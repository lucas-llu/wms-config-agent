"""Task-local, opaque provider conversation identity."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SESSION: ContextVar[str | None] = ContextVar("wms_llm_session", default=None)


@contextmanager
def llm_conversation(identity: str) -> Iterator[None]:
    token = _SESSION.set(hashlib.sha256(identity.encode("utf-8")).hexdigest())
    try:
        yield
    finally:
        _SESSION.reset(token)


def provider_session_id() -> str:
    return _SESSION.get() or uuid.uuid4().hex
