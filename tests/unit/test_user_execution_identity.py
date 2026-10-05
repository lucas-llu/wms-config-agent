from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from multiuser.access import UserContext
from multiuser.agent import UserAgent
from multiuser.identity import InvalidIdentity


def test_model_guard_rechecks_identity_provider_before_and_after_delegate():
    store = MagicMock()
    repository = Mock()
    check = Mock(side_effect=InvalidIdentity("revoked"))
    from core.settings import load_settings

    agent = UserAgent(store, "synthetic", Mock(), load_settings().agent)
    runner = agent.runner(UserContext("A", "issuer", "sub", "sid", 1, 2), repository, check)
    with pytest.raises(InvalidIdentity):
        runner.supervisor.classifier.llm.chat([])
    agent.llm.chat.assert_not_called()
    check.side_effect = [None, InvalidIdentity("revoked")]
    with pytest.raises(InvalidIdentity):
        runner.supervisor.classifier.llm.chat([])
    assert agent.llm.chat.call_count == 1


def test_finished_result_not_delivered_after_remote_session_revocation():
    agent = UserAgent(Mock(), "synthetic", Mock(), Mock())

    @asynccontextmanager
    async def saver(context):
        yield Mock()

    agent.saver = saver
    result = SimpleNamespace()

    async def start(*args, **kwargs):
        return result

    agent.runner = Mock(return_value=SimpleNamespace(start=start))
    agent.result = Mock()
    check = Mock(side_effect=InvalidIdentity("revoked"))
    with pytest.raises(InvalidIdentity):
        agent.start(
            UserContext("A", "issuer", "sub", "sid", 1, 2),
            Mock(),
            "synthetic",
            identity_check=check,
        )
    agent.result.assert_not_called()
