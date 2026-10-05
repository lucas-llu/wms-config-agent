"""Current OIDC session state, not just a previously valid JWT signature."""

import httpx

from multiuser.identity import IdentityUnavailable, InvalidIdentity


class TokenIntrospector:
    def __init__(self, issuer, client_id, client_secret, *, client=None):
        if not client_id or not client_secret:
            raise ValueError("Confidential API client configuration required")
        self.issuer, self.client_id, self.client_secret = (
            issuer.rstrip("/"),
            client_id,
            client_secret,
        )
        self.client = client or httpx.Client(timeout=5, follow_redirects=False)

    def close(self):
        self.client.close()

    def check(self, token, principal):
        try:
            response = self.client.post(
                self.issuer + "/protocol/openid-connect/token/introspect",
                auth=(self.client_id, self.client_secret),
                data={"token": token},
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("Invalid identity response")
        except (httpx.HTTPError, ValueError) as exc:
            raise IdentityUnavailable("Identity service unavailable") from exc
        if value.get("active") is not True or value.get("sub") != principal.subject:
            raise InvalidIdentity("User session is no longer active")
