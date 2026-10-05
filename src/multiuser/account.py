"""Pinned Keycloak self-service API: uses the caller token, never admin credentials."""

import re

import httpx

from multiuser.access import AccessDenied
from multiuser.identity import IdentityUnavailable, InvalidIdentity


class AccountClient:
    def __init__(self, issuer, *, client=None):
        self.issuer = issuer.rstrip("/")
        self.client = client or httpx.Client(timeout=5, follow_redirects=False)

    def close(self):
        self.client.close()

    def request(self, method, path, token):
        return self._request(method, self.issuer + "/account/" + path, token)

    def _request(self, method, url, token):
        try:
            response = self.client.request(
                method,
                url,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
            if response.status_code == 401:
                raise InvalidIdentity("Session ended")
            if response.status_code == 403:
                raise AccessDenied("Self-service permission required")
            response.raise_for_status()
            return response.json() if response.content else None
        except (InvalidIdentity, AccessDenied):
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise IdentityUnavailable("Account service unavailable") from exc

    def profile(self, context, token):
        data = self._request("GET", self.issuer + "/protocol/openid-connect/userinfo", token)
        if not isinstance(data, dict) or data.get("sub") != context.subject:
            raise IdentityUnavailable("Unexpected account profile")
        return {
            "email": data.get("email", ""),
            "email_verified": data.get("email_verified") is True,
        }

    def devices(self, context, token):
        data = self.request("GET", "sessions", token)
        if not isinstance(data, list):
            raise IdentityUnavailable("Unexpected device response")
        return [
            {
                "id": s["id"],
                "current": s["id"] == context.sid,
                "browser": s.get("browser") or "未知浏览器",
                "ip_address": s.get("ipAddress", ""),
                "started": s.get("started", 0),
                "last_access": s.get("lastAccess", 0),
            }
            for s in data
            if isinstance(s, dict) and isinstance(s.get("id"), str)
        ]

    def logout_device(self, context, token, session_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ValueError("Invalid device identifier")
        if not any(d["id"] == session_id for d in self.devices(context, token)):
            from agents.repositories import SessionNotFoundError

            raise SessionNotFoundError("Device not found")
        self.request("DELETE", "sessions/" + session_id, token)

    def logout_others(self, context, token):
        self.request("DELETE", "sessions?current=false", token)
