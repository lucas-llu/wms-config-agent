import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.p0 import create_app
from multiuser.identity import OIDCVerifier, Principal

ISSUER = "https://identity.example.invalid/realms/test"


@pytest.fixture
def fixture():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key.update(kid="test-key", use="sig", alg="RS256")
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/certs"})
        return httpx.Response(200, json={"keys": [key]})

    verifier = OIDCVerifier(
        ISSUER, "wms-api", client=httpx.Client(transport=httpx.MockTransport(transport))
    )

    def token(_kid="test-key", **changes):
        claims = {
            "iss": ISSUER,
            "sub": "user-a",
            "aud": "wms-api",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "typ": "Bearer",
        }
        claims.update(changes)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": _kid})

    return verifier, token, calls


def test_signed_identity_is_stable_private_and_body_ids_do_not_authenticate(fixture):
    verifier, token, calls = fixture
    with TestClient(create_app(verifier)) as client:
        assert client.get("/v1/me?owner_user_id=user-a").status_code == 401
        a = client.get("/v1/me", headers={"Authorization": "Bearer " + token()}).json()
        b = client.get("/v1/me", headers={"Authorization": "Bearer " + token(sub="user-b")}).json()
        assert a["subject"] == "user-a" and b["subject"] == "user-b"
        assert a["identity_key"] != b["identity_key"]
        assert a["knowledge_access"] == []
        assert len(calls) == 2  # Shared discovery/JWKS cache, not a fetch per request.
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "other-api"},
        {"exp": 1},
        {"iat": int(time.time()) + 1000},
        {"sub": ""},
        {"typ": "ID"},
    ],
)
def test_invalid_claims_are_rejected(fixture, changes):
    verifier, token, _ = fixture
    with TestClient(create_app(verifier)) as client:
        assert (
            client.get(
                "/v1/me", headers={"Authorization": "Bearer " + token(**changes)}
            ).status_code
            == 401
        )


def test_unsigned_and_forged_tokens_are_rejected(fixture):
    verifier, token, _ = fixture
    unsigned = jwt.encode({"sub": "administrator"}, key="", algorithm="none")
    forged = jwt.encode({"sub": "administrator"}, "synthetic-test-key" * 3, algorithm="HS256")
    broken = token()[:-20] + "xxxxxxxxxxxxxxxxxxxx"
    with TestClient(create_app(verifier)) as client:
        for value in (unsigned, forged, broken, "not-a-token", "x" * 17000):
            assert (
                client.get("/v1/me", headers={"Authorization": "Bearer " + value}).status_code
                == 401
            )


def test_unknown_kids_do_not_refetch_without_limit(fixture):
    verifier, token, calls = fixture
    verifier.verify(token())
    for index in range(20):
        with pytest.raises(ValueError):
            verifier.verify(token(_kid=f"unknown-{index}"))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "issuer",
    ["http://external.invalid", "https://user:pass@host.invalid", "https://host.invalid?a=1"],
)
def test_issuer_configuration_fails_closed(issuer):
    with pytest.raises(ValueError):
        OIDCVerifier(issuer, "wms-api", allow_local_http=True)


@pytest.mark.parametrize("mode", ["unavailable", "redirect", "issuer", "jwks-origin"])
def test_unavailable_or_redirected_jwks_returns_service_error(fixture, mode):
    verifier, token, _ = fixture
    verifier.client.close()

    def broken(request):
        if mode == "unavailable":
            return httpx.Response(503)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.invalid"})
        return httpx.Response(
            200,
            json={
                "issuer": ISSUER if mode == "jwks-origin" else "https://attacker.invalid",
                "jwks_uri": "https://attacker.invalid/certs"
                if mode == "jwks-origin"
                else ISSUER + "/certs",
            },
        )

    verifier.client = httpx.Client(transport=httpx.MockTransport(broken))
    with TestClient(create_app(verifier)) as client:
        assert (
            client.get("/v1/me", headers={"Authorization": "Bearer " + token()}).status_code == 503
        )


def test_principal_issuer_is_part_of_identity():
    assert (
        Principal("issuer-a", "same-subject").identity_key
        != Principal("issuer-b", "same-subject").identity_key
    )
