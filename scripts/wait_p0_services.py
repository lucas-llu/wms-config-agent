"""Bounded disposable-service startup checks; never print credentials."""

import os
import time

import httpx
import psycopg
from redis import Redis
from redis.exceptions import RedisError

deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    try:
        with psycopg.connect(os.environ["P0_POSTGRES_DSN"], connect_timeout=2) as db:
            db.execute("SELECT 1")
        client = Redis.from_url(
            os.environ["P0_REDIS_URL"], socket_connect_timeout=2, socket_timeout=2
        )
        try:
            assert client.ping()
        finally:
            client.close()
        response = httpx.get(
            os.environ["P0_OIDC_ISSUER"] + "/.well-known/openid-configuration", timeout=3
        )
        response.raise_for_status()
        print("P0 disposable services ready")
        break
    except (psycopg.Error, RedisError, OSError, httpx.HTTPError, AssertionError):
        time.sleep(2)
else:
    raise SystemExit("P0 service readiness timed out; no compatibility gate has passed")
