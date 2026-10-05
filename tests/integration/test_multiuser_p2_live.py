import json
import os

import pytest
from test_multiuser_p1_live import system as _system
from test_multiuser_p1_live import tokens as _tokens

# Reuse the real account/database fixtures without importing the parent's tests.
system = _system
tokens = _tokens

pytestmark = pytest.mark.skipif(os.getenv("WMS_P2_LIVE") != "1", reason="P2 requires disposable PG")


def test_private_round_evidence_and_workbench_image_endpoint(system):
    client, headers, contexts, workspace, store, application, admin = system
    result = client.post(
        "/v1/conversations",
        headers=headers[0],
        json={"workspace_id": workspace.workspace_id, "goal": "Synthetic"},
    )
    session_id = result.json()["session_id"]
    repository = application.repository(contexts[0], session_id)
    for index in range(2):
        repository.append_turn(
            session_id=session_id,
            expected_revision=index + 1,
            role="assistant",
            message=f"Answer {index + 1}",
            metadata={
                "citations": [
                    {
                        "source": f"synthetic-{index}.pdf",
                        "excerpt": "Evidence [IMAGE: marker]",
                        "full_excerpt": f"PRIVATE Evidence round {index + 1}",
                    }
                ]
            },
        )
        if index == 0:
            repository.update_revision(
                session_id=session_id,
                expected_revision=1,
                state_update={"status": "paused"},
                actor="system",
                reason="fixture",
            )
    root = "/v1/conversations/" + session_id
    response = client.get(root + "/workbench", headers=headers[0])
    assert response.status_code == 200
    assert len(response.json()["turns"]) == 2
    assert (
        "PRIVATE Evidence round 1" in response.text and "PRIVATE Evidence round 2" in response.text
    )
    assert "[IMAGE:" not in json.dumps([t["citations"] for t in response.json()["turns"]])
    historical = client.get(root + "/workbench?revision=1", headers=headers[0]).json()
    assert len(historical["turns"]) == 1
    assert client.get(root + "/workbench", headers=headers[1]).status_code == 404
    turn_id = response.json()["turns"][0]["turn_id"]
    assert (
        client.get(root + f"/turns/{turn_id}/evidence/0/images/0", headers=headers[1]).status_code
        == 404
    )


def test_invalid_review_strategy_does_not_start_owned_execution(system):
    client, headers, contexts, workspace, store, application, admin = system
    result = client.post(
        "/v1/conversations",
        headers=headers[0],
        json={
            "workspace_id": workspace.workspace_id,
            "goal": "Synthetic",
            "answer_strategy": "automatic",
        },
    )
    assert result.status_code == 422


def test_empty_trash_handles_more_than_one_page_and_preserves_other_owner(system):
    from agents.services import SessionService

    client, headers, contexts, workspace, store, application, admin = system
    from multiuser.session_repository import PostgresSessionRepository

    repos = [PostgresSessionRepository(store, c, workspace.workspace_id) for c in contexts]
    for index in range(101):
        row = SessionService(repos[0]).create_session("Synthetic trash")
        repos[0].delete_session(row.session_id)
    other = SessionService(repos[1]).create_session("Other owner")
    repos[1].delete_session(other.session_id)
    params = {"workspace_id": workspace.workspace_id}
    snapshot = client.get("/v1/trash/snapshot", headers=headers[0], params=params).json()
    assert snapshot["count"] == 101
    assert (
        client.post(
            "/v1/trash/empty",
            headers=headers[1],
            params=params,
            json={"fingerprint": snapshot["fingerprint"]},
        ).status_code
        == 409
    )
    stale = client.post(
        "/v1/trash/empty", headers=headers[0], params=params, json={"fingerprint": "0" * 64}
    )
    assert stale.status_code == 409
    result = client.post(
        "/v1/trash/empty",
        headers=headers[0],
        params=params,
        json={"fingerprint": snapshot["fingerprint"]},
    )
    assert result.json() == {"purged": 101}
    assert client.get("/v1/trash/snapshot", headers=headers[0], params=params).json()["count"] == 0
    assert client.get("/v1/trash/snapshot", headers=headers[1], params=params).json()["count"] == 1
