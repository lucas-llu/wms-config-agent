import time
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agents.repositories import SessionNotFoundError
from api.users import create_app
from multiuser.access import AccessDenied, UserContext
from multiuser.account import AccountClient
from multiuser.application import OwnedApplication
from multiuser.identity import IdentityUnavailable, InvalidIdentity


@pytest.fixture
def context():
    return UserContext(
        "user:a",
        "https://id.example.invalid/realms/test",
        "subject:a",
        "sid-a",
        1,
        int(time.time()) + 300,
    )


def client(data, status=200):
    return AccountClient(
        "https://id.example.invalid/realms/test",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=data))
        ),
    )


def test_verified_self_service_profile_and_device_projection(context):
    account = client({"sub": context.subject, "email": "a@example.invalid", "email_verified": True})
    assert account.profile(context, "synthetic")["email_verified"] is True
    account.close()
    account = client(
        [
            {"id": "sid-a", "ipAddress": "127.0.0.1", "started": 1, "lastAccess": 2},
            {"id": "sid-other", "browser": "Synthetic"},
        ]
    )
    devices = account.devices(context, "synthetic")
    assert devices[0]["current"] and not devices[1]["current"]
    account.logout_device(context, "synthetic", "sid-other")
    account.logout_others(context, "synthetic")
    with pytest.raises(SessionNotFoundError):
        account.logout_device(context, "synthetic", "sid-victim")
    with pytest.raises(ValueError):
        account.logout_device(context, "synthetic", "../victim")
    account.close()


@pytest.mark.parametrize(
    "status,error", [(401, InvalidIdentity), (403, AccessDenied), (500, IdentityUnavailable)]
)
def test_account_errors_never_include_tokens_or_private_bodies(context, status, error):
    account = client({"PRIVATE": "synthetic"}, status)
    with pytest.raises(error):
        account.devices(context, "synthetic-token")
    account.close()


@pytest.mark.parametrize("value", [None, [], {"sub": "other"}])
def test_profile_subject_and_response_shape_are_verified(context, value):
    account = client(value)
    with pytest.raises(IdentityUnavailable):
        account.profile(context, "synthetic")
    account.close()


def test_invalid_device_response_fails_closed(context):
    account = client({})
    with pytest.raises(IdentityUnavailable):
        account.devices(context, "synthetic")
    account.close()


def test_web_endpoints_reuse_verified_identity_and_config_has_no_secret(context):
    verifier, intro, application, account = Mock(), Mock(), Mock(), Mock()
    application.store.resolve.return_value = context
    application.store.profile.return_value = {"nickname": "A", "workspaces": []}
    account.profile.return_value = {"email": "a@example.invalid", "email_verified": True}
    account.devices.return_value = []
    headers = {"Authorization": "Bearer synthetic"}
    with TestClient(
        create_app(
            verifier,
            intro,
            application,
            account=account,
            web_config={"issuer": context.issuer, "client_id": "wms-workbench"},
        )
    ) as api:
        assert set(api.get("/v1/web-config").json()) == {"issuer", "client_id"}
        assert api.get("/v1/me/devices").status_code == 401
        assert api.get("/v1/me", headers=headers).json()["email_verified"]
        assert api.get("/v1/me/devices", headers=headers).json() == []
        assert api.post("/v1/me/logout-others", headers=headers).status_code == 200
        assert api.delete("/v1/me/devices/sid-other", headers=headers).status_code == 200
        account.logout_device.assert_called_with(context, "synthetic", "sid-other")


def test_workbench_images_follow_owned_turns_without_paths(context, tmp_path):
    @dataclass
    class Record:
        session_id: str = "session:a"

    @dataclass
    class Turn:
        turn_id: str = "turn:a"
        role: str = "assistant"
        revision: int = 2
        message: str = "Answer\nSupporting evidence\n[1] Old source"
        metadata: dict = None

    source = {
        "source": "synthetic.pdf",
        "excerpt": "Evidence [IMAGE: x]",
        "full_excerpt": "Full evidence [IMAGE: x]",
    }
    turn = Turn(metadata={"citations": [source]})
    repository = Mock()
    repository.get_session.return_value = Record()
    repository.get_revision.return_value = SimpleNamespace(revision=2, state={})
    repository.list_turns.return_value = [turn]
    repository.list_approvals.return_value = []
    path = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "white").save(path)
    images = Mock()
    images.resolve.return_value = ([{"path": path}], False)
    application = OwnedApplication(Mock(), export_root=tmp_path, images=images)
    application.repository = Mock(return_value=repository)
    result = application.workbench(context, "session:a")
    assert result["turns"][0]["citations"][0]["excerpt"] == "Full evidence"
    assert "path" not in result["turns"][0]["citations"][0]
    assert application.evidence_image(context, "session:a", "turn:a", 0, 0) == (path, "image/png")
    with pytest.raises(SessionNotFoundError):
        application.evidence_image(context, "session:a", "turn:victim", 0, 0)
    with pytest.raises(SessionNotFoundError):
        application.evidence_image(context, "session:a", "turn:a", 0, 9)
    path.write_text("<script/>")
    with pytest.raises(AccessDenied):
        application.evidence_image(context, "session:a", "turn:a", 0, 0)
    assert asdict(turn)["turn_id"] == "turn:a"
