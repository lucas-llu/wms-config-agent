"""Read-only current Keycloak SID authority, with no persisted user bearer."""

import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from multiuser.access import AccessDenied
from multiuser.identity import IdentityUnavailable


class SessionAuthority:
    def __init__(self, issuer, client_id, client_secret, *, client=None, allow_local_http=False):
        parts = urlsplit(issuer)
        if (
            (
                parts.scheme != "https"
                and not (
                    allow_local_http and parts.scheme == "http" and parts.hostname == "127.0.0.1"
                )
            )
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Fixed trusted HTTPS Keycloak issuer required")
        prefix, separator, realm = parts.path.rstrip("/").rpartition("/realms/")
        if not separator or not realm or "/" in realm or not client_id or not client_secret:
            raise ValueError("Dedicated session-read service account required")
        self.issuer = issuer.rstrip("/")
        self.admin = f"{parts.scheme}://{parts.netloc}{prefix}/admin/realms/{quote(realm, safe='')}"
        self.client_id, self.secret = client_id, client_secret
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(5, connect=2),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        self.lock, self.token, self.until = threading.Lock(), "", 0.0

    def close(self):
        self.client.close()

    def _token(self):
        with self.lock:
            if self.until <= time.monotonic():
                response = self.client.post(
                    self.issuer + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.secret,
                    },
                )
                if response.status_code != 200:
                    raise IdentityUnavailable("Session authority unavailable")
                body = response.json()
                token, lifespan = body.get("access_token"), body.get("expires_in")
                if (
                    not isinstance(token, str)
                    or not token
                    or type(lifespan) is not int
                    or lifespan < 5
                ):
                    raise IdentityUnavailable("Session authority unavailable")
                self.token, self.until = token, time.monotonic() + min(lifespan - 2, 60)
            return self.token

    def check(self, context):
        if context.issuer != self.issuer or not context.sid:
            raise AccessDenied("Session authority mismatch")
        try:
            headers = {"Authorization": "Bearer " + self._token()}
            root = self.admin + "/users/" + quote(context.subject, safe="")
            user = self.client.get(root, headers=headers)
            sessions = self.client.get(root + "/sessions", headers=headers)
            if user.status_code == 404:
                raise AccessDenied("User unavailable")
            if user.status_code != 200 or sessions.status_code != 200:
                raise IdentityUnavailable("Session authority unavailable")
            profile, rows = user.json(), sessions.json()
            if not isinstance(profile, dict) or not isinstance(rows, list):
                raise IdentityUnavailable("Session authority unavailable")
            before = profile.get("notBefore", 0)
            if (
                profile.get("enabled") is not True
                or type(before) is not int
                or context.issued_at <= before
                or not any(isinstance(r, dict) and r.get("id") == context.sid for r in rows)
            ):
                raise AccessDenied("Device session has ended")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise IdentityUnavailable("Session authority unavailable") from exc
