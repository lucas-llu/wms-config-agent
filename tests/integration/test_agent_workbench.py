from __future__ import annotations

from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.workspace import Workspace, WorkspaceService
from observability.dashboard.services.workbench_service import WorkbenchService, safe_source


def render(service):
    from observability.dashboard.workbench import render_workbench

    render_workbench(service)


def fixture(tmp_path, *, status="paused", enabled=True):
    repo = SessionRepository(tmp_path / "sessions.db")
    repo.create_session(
        session_id="session:a",
        goal="Configure receiving",
        initial_state={
            "configuration_tasks": [
                {
                    "task_id": "task:a",
                    "title": "Receiving",
                    "steps": ["Follow cited procedure"],
                    "evidence_ids": ["e:a"],
                }
            ],
            "evidence_registry": [
                {
                    "evidence_id": "e:a",
                    "source": "manual/receiving.pdf",
                    "excerpt": "Source evidence",
                    "page_start": 2,
                }
            ],
            "open_questions": [{"text": "Which site?"}],
        },
    )
    repo.append_turn(session_id="session:a", expected_revision=1, role="user", message="Goal")
    repo.update_revision(
        session_id="session:a",
        expected_revision=1,
        state_update={"status": status},
        actor="system",
        reason="fixture",
    )
    call = Mock(return_value={"isError": False, "structuredContent": {"signals": []}})
    return WorkbenchService(repo, call, enabled=enabled), call


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_workbench_read_only_render_and_feedback(tmp_path):
    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert len(app.tabs) == 5
    assert not app.json
    assert len(app.chat_message) == 1
    call.assert_not_called()
    button(app, "记录反馈").click().run()
    call.assert_called_once_with(
        "record_configuration_feedback",
        {"session_id": "session:a", "revision": 2, "kind": "thumbs_up", "reason": ""},
    )


def test_historical_view_disables_mutations_but_allows_feedback(tmp_path):
    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    next(item for item in app.selectbox if item.label == "查看版本").select(1).run()
    for label in ("继续对话", "验证草稿", "提交审查", "导出已批准方案"):
        assert button(app, label).disabled
    assert not button(app, "记录反馈").disabled
    call.assert_not_called()


def test_review_requires_confirmation_and_comment(tmp_path):
    service, call = fixture(tmp_path, status="review_required")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "提交审查").click().run()
    call.assert_not_called()
    next(item for item in app.text_area if item.label == "审查意见（必填）").input("Checked")
    app.checkbox[0].check()
    next(item for item in app.selectbox if item.label == "审查决定").select("approve")
    button(app, "提交审查").click().run()
    call.assert_called_once_with(
        "review_configuration_draft",
        {
            "session_id": "session:a",
            "expected_revision": 2,
            "decision": "approve",
            "comment": "Checked",
        },
    )


def test_disabled_agent_and_empty_state(tmp_path):
    repo = SessionRepository(tmp_path / "empty.db")
    call = Mock()
    service = WorkbenchService(repo, call, enabled=False)
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert button(app, "新建会话").disabled
    assert any("暂无会话" in item.value for item in app.info)
    with pytest.raises(ValueError):
        service.start("goal")
    call.assert_not_called()


def test_service_binds_revision_and_rejects_scope_and_overrides(tmp_path):
    service, call = fixture(tmp_path)
    with pytest.raises(ValueError):
        service.act("validate", "session:a", 1)
    with pytest.raises(ValueError):
        service.act("validate", "session:a", 2, expected_revision=99)
    with pytest.raises(ValueError):
        service.act("apply", "session:a", 2)
    WorkspaceService(service.repository.database_path).create(
        Workspace("workspace:b", "B", ("c",), ("m",), ("s",), ("e",))
    )
    other = WorkbenchService(
        SessionRepository(service.repository.database_path, workspace_id="workspace:b"),
        call,
        enabled=True,
    )
    with pytest.raises(SessionNotFoundError):
        other.view("session:a", 2)
    with pytest.raises(SessionNotFoundError):
        other.act("feedback", "session:a", 2, kind="thumbs_up")
    call.assert_not_called()


@pytest.mark.parametrize("source", ["C:/private/file.pdf", "/private/file.pdf", "../file.pdf"])
def test_source_paths_hidden(source):
    assert safe_source(source) == "本地来源路径已隐藏"


def test_tool_error_is_generic_and_not_retried(tmp_path):
    service, call = fixture(tmp_path)
    call.side_effect = RuntimeError("private-token")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "验证草稿").click().run()
    assert not app.exception
    assert len(app.error) == 1
    assert "private-token" not in app.error[0].value
    assert call.call_count == 1
