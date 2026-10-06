"""Generate synthetic realm and credentials in ignored data/, never stdout."""

import json
import os
import secrets
from pathlib import Path


def prepare(root):
    root = root.resolve()
    if (root / ".env").exists() or (root / "realm.json").exists():
        raise ValueError("Fixture already exists; reuse it rather than replacing credentials")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {
        "P0_DB_PASSWORD": secrets.token_urlsafe(24),
        "P0_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "P0_A_PASSWORD": secrets.token_urlsafe(24),
        "P0_B_PASSWORD": secrets.token_urlsafe(24),
        "P1_DB_PASSWORD": secrets.token_urlsafe(24),
        "P1_API_CLIENT_SECRET": secrets.token_urlsafe(24),
        "P3_DB_PASSWORD": secrets.token_urlsafe(24),
        "P3_SESSION_CLIENT_SECRET": secrets.token_urlsafe(24),
        "P3_EXECUTION_TOKEN": secrets.token_urlsafe(32),
        "P0_FIXTURE_DIR": root.as_posix(),
        "P0_REDIS_URL": "redis://127.0.0.1:26379/0",
        "P0_OIDC_ISSUER": "http://127.0.0.1:28081/realms/wms-p0",
    }
    values["P0_POSTGRES_DSN"] = (
        "postgresql://p0_admin:" + values["P0_DB_PASSWORD"] + "@127.0.0.1:25432/wms_p0"
    )
    values["P1_POSTGRES_DSN"] = (
        "postgresql://p1_runtime:" + values["P1_DB_PASSWORD"] + "@127.0.0.1:25432/wms_p0"
    )
    values["P3_CONTROL_DSN"] = (
        "postgresql://p3_control:" + values["P3_DB_PASSWORD"] + "@127.0.0.1:25432/wms_p0"
    )
    realm = {
        "realm": "wms-p0",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "clients": [
            {
                "clientId": "wms-p0-cli",
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": ["http://127.0.0.1:18080/callback"],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": [
                    {
                        "name": "wms-api-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": "wms-api",
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                        },
                    }
                ],
            },
            {
                "clientId": "wms-api",
                "publicClient": False,
                "secret": values["P1_API_CLIENT_SECRET"],
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
            },
            {
                "clientId": "wms-session-reader",
                "publicClient": False,
                "secret": values["P3_SESSION_CLIENT_SECRET"],
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
            },
        ],
        "users": [
            {
                "username": name,
                "email": f"{name}@example.invalid",
                "enabled": True,
                "emailVerified": True,
                "firstName": name,
                "lastName": "P0",
                "credentials": [{"type": "password", "value": values[key], "temporary": False}],
            }
            for name, key in (("user-a", "P0_A_PASSWORD"), ("user-b", "P0_B_PASSWORD"))
        ],
    }
    realm["users"].append(
        {
            "username": "service-account-wms-session-reader",
            "enabled": True,
            "serviceAccountClientId": "wms-session-reader",
            "clientRoles": {"realm-management": ["view-users"]},
        }
    )
    (root / "realm.json").write_text(json.dumps(realm), encoding="utf-8")
    (root / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    (root / ".env").chmod(0o600)
    # Bind-mounted synthetic realm must be readable by the Keycloak container UID.
    # The host directory remains mode 0700; no production credentials are used.
    (root / "realm.json").chmod(0o644)
    if target := os.getenv("GITHUB_ENV"):
        for key, value in values.items():
            if any(s in key for s in ("PASSWORD", "SECRET", "DSN", "TOKEN")):
                print("::add-mask::" + value)
        with Path(target).open("a", encoding="utf-8") as output:
            output.write("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return values


if __name__ == "__main__":
    prepare(Path("data/p0-fixture"))
