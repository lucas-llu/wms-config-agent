"""Trusted OIDC identity from a configured issuer, never request-body user IDs."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt


class InvalidIdentity(ValueError):
    pass


class IdentityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str

    @property
    def identity_key(self) -> str:
        return hashlib.sha256(f"{self.issuer}\0{self.subject}".encode()).hexdigest()


class OIDCVerifier:
    def __init__(
        self, issuer, audience, *, client=None, allow_local_http=False, clock=time.monotonic
    ):
        url = urlsplit(issuer)
        if (
            (
                url.scheme != "https"
                and not (
                    allow_local_http
                    and url.scheme == "http"
                    and url.hostname in {"localhost", "127.0.0.1"}
                )
            )
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("OIDC issuer requires HTTPS (explicit loopback HTTP for P0 only)")
        if not audience:
            raise ValueError("OIDC API audience is required")
        self.issuer, self.audience = issuer.rstrip("/"), audience
        self.client = client or httpx.Client(timeout=5, follow_redirects=False)
        self.clock, self.lock = clock, threading.Lock()
        self.keys, self.loaded_at = {}, float("-inf")

    def close(self):
        self.client.close()

    def _load(self):
        try:
            response = self.client.get(self.issuer + "/.well-known/openid-configuration")
            response.raise_for_status()
            discovery = response.json()
            uri = discovery["jwks_uri"]
            if discovery["issuer"] != self.issuer or (
                urlsplit(uri).scheme,
                urlsplit(uri).netloc,
            ) != (urlsplit(self.issuer).scheme, urlsplit(self.issuer).netloc):
                raise ValueError("Unexpected issuer or JWKS origin")
            response = self.client.get(uri)
            response.raise_for_status()
            keys = {
                key["kid"]: jwt.PyJWK.from_dict(key, algorithm="RS256").key
                for key in response.json()["keys"]
                if key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
            }
            if not keys:
                raise ValueError("No trusted signing keys")
        except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError) as exc:
            raise IdentityUnavailable("Identity verification is unavailable") from exc
        self.keys, self.loaded_at = keys, self.clock()

    def verify(self, token: str) -> Principal:
        if not isinstance(token, str) or len(token) > 16_384:
            raise InvalidIdentity("Invalid access token")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or len(kid) > 128:
                raise InvalidIdentity("Invalid access token")
            with self.lock:
                # Bound refreshes caused by arbitrary unknown kids; also refresh
                # existing keys after five minutes to allow issuer key rotation.
                age = self.clock() - self.loaded_at
                if age >= 300 or (kid not in self.keys and age >= 2):
                    self._load()
                key = self.keys.get(kid)
            if key is None:
                raise InvalidIdentity("Invalid access token")
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
                leeway=5,
            )
            if (
                claims.get("typ") != "Bearer"
                or not isinstance(claims["sub"], str)
                or not claims["sub"]
            ):
                raise InvalidIdentity("Expected a user access token")
            return Principal(self.issuer, claims["sub"])
        except jwt.PyJWTError as exc:
            raise InvalidIdentity("Invalid access token") from exc
