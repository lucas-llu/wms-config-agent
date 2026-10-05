"""Enable registration/mail/session policy only in this job's synthetic realm."""

import json
from pathlib import Path


def prepare(root=Path("data/p0-fixture")):
    path = root / "realm.json"
    realm = json.loads(path.read_text())
    if realm["realm"] != "wms-p0":
        raise ValueError("Only the disposable fixture may be changed")
    realm.update(
        registrationAllowed=True,
        requiredCredentials=["password"],
        verifyEmail=True,
        resetPasswordAllowed=True,
        loginTheme="wms",
        internationalizationEnabled=True,
        supportedLocales=["en", "zh-CN"],
        defaultLocale="zh-CN",
        eventsListeners=["jboss-logging", "wms-password-revocation"],
        bruteForceProtected=True,
        actionTokenGeneratedByUserLifespan=30,
        smtpServer={
            "host": "mailpit",
            "port": "1025",
            "from": "noreply@example.invalid",
            "ssl": "false",
            "starttls": "false",
        },
    )
    audience = realm["clients"][0]["protocolMappers"]
    realm["clients"].append(
        {
            "clientId": "wms-workbench",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "implicitFlowEnabled": False,
            "redirectUris": ["http://127.0.0.1:5173/"],
            "webOrigins": ["http://127.0.0.1:5173"],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": audience,
        }
    )
    path.write_text(json.dumps(realm), encoding="utf-8")


if __name__ == "__main__":
    prepare()
