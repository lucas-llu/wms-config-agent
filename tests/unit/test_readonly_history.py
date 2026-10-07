from unittest.mock import Mock

import pytest

from multiuser.access import AccessDenied, UserContext
from multiuser.application import OwnedApplication


def test_legacy_history_blocks_model_before_checkpoint_or_runtime(tmp_path):
    agent = Mock()
    application = OwnedApplication(Mock(), export_root=tmp_path, agent=agent)
    repository = Mock()
    repository.get_session.return_value = Mock(current_revision=1, legacy_readonly=True)
    application.repository = Mock(return_value=repository)
    with pytest.raises(AccessDenied, match="cannot resume"):
        application.continue_session(
            UserContext("A", "issuer", "subject", "sid", 1, 2), "session:a", "resume", 1
        )
    agent.continue_session.assert_not_called()
    repository.get_session.assert_called_once()
