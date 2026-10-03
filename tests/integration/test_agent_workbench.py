from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.workspace import Workspace, WorkspaceService
from observability.dashboard.services.workbench_service import WorkbenchService, safe_source


def render(service):
    from observability.dashboard.workbench import render_workbench

    render_workbench(service)


@pytest.fixture(autouse=True)
def local_workbench_host(monkeypatch):
    import streamlit as st

    original = st.get_option
    monkeypatch.setattr(
        st, "get_option", lambda name: "127.0.0.1" if name == "server.address" else original(name)
    )


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
    assert len(app.tabs) == 6
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
    next(item for item in app.selectbox if item.label == "对话轮次").select(1).run()
    assert app.chat_input[0].disabled
    for label in ("验证草稿", "提交审查", "导出已批准方案"):
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
    assert button(app, "＋ 新对话").disabled
    assert app.chat_input[0].disabled
    assert button(app, "清理应用缓存").disabled
    assert any("暂无会话" in item.value for item in app.caption)
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


def test_new_chat_and_suggestion_do_not_send_until_submission(tmp_path):
    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "＋ 新对话").click().run()
    assert not app.exception
    assert not app.chat_message
    button(app, "查一个配置").click().run()
    assert not app.exception
    call.assert_not_called()
    app.chat_input[0].set_value("如何配置收货？").run()
    call.assert_called_once_with("start_configuration_session", {"goal": "如何配置收货？"})


def test_chat_composer_preserves_selected_revision(tmp_path):
    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    app.chat_input[0].set_value("继续查询 ASN").run()
    assert not app.exception
    call.assert_called_once_with(
        "continue_configuration_session",
        {"session_id": "session:a", "expected_revision": 2, "message": "继续查询 ASN"},
    )


def test_history_search_and_safe_markdown(tmp_path):
    from observability.dashboard.workbench import _safe_markdown

    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    app.text_input[0].set_value("does-not-exist").run()
    assert not app.exception
    assert any("没有匹配" in item.value for item in app.caption)
    call.assert_not_called()
    rendered = _safe_markdown('![tracking](https://example.invalid/pixel) <img src="remote">')
    assert "![" not in rendered and "<img" not in rendered


def test_assistant_message_renders_with_supported_avatar(tmp_path):
    service, call = fixture(tmp_path)
    service.repository.append_turn(
        session_id="session:a",
        expected_revision=2,
        role="assistant",
        message="### 结论\n请确认测试环境版本。\n\n- 引用依据 [1]",
    )
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert len(app.chat_message) == 2
    assert app.chat_input
    assert any("请确认测试环境版本" in item.value for item in app.markdown)
    call.assert_not_called()


def test_style_keeps_sidebar_expand_control_visible(tmp_path):
    service, _ = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    styles = next(item.value for item in app.markdown if "<style>" in item.value)
    assert '[data-testid="stExpandSidebarButton"] { visibility: visible; }' in styles


def test_cache_shortcut_is_separate_from_copy_and_requires_confirmation(tmp_path, monkeypatch):
    import streamlit as st

    service, call = fixture(tmp_path)
    data_clear, resource_clear = Mock(), Mock()
    monkeypatch.setattr(st.cache_data, "clear", data_clear)
    monkeypatch.setattr(st.cache_resource, "clear", resource_clear)
    original = service.repository.get_revision("session:a")
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert st.get_option("client.toolbarMode") == "viewer"
    cache = button(app, "清理应用缓存")
    assert cache.proto.shortcut == "ctrl+alt+shift+k"
    cache.click().run()
    assert not app.exception
    assert button(app, "确认清理缓存")
    data_clear.assert_not_called()
    resource_clear.assert_not_called()
    button(app, "取消").click().run()
    assert not app.exception
    data_clear.assert_not_called()
    resource_clear.assert_not_called()
    button(app, "清理应用缓存").click().run()
    button(app, "确认清理缓存").click().run()
    assert not app.exception
    data_clear.assert_called_once_with()
    resource_clear.assert_called_once_with()
    assert service.repository.get_revision("session:a") == original
    assert len(service.repository.list_turns("session:a")) == 1
    call.assert_not_called()


def test_cache_mode_configured_before_the_first_browser_session():
    configuration = Path(__file__).resolve().parents[2] / ".streamlit" / "config.toml"
    with configuration.open("rb") as handle:
        assert tomllib.load(handle)["client"]["toolbarMode"] == "viewer"


def test_cache_maintenance_disabled_during_processing_and_other_dialogs(tmp_path):
    service, _ = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    app.session_state["workbench_busy"] = True
    app.run()
    assert button(app, "清理应用缓存").disabled
    app.session_state["workbench_busy"] = False
    app.run()
    button(app, "重命名").click().run()
    assert not app.exception
    assert button(app, "清理应用缓存").disabled


@pytest.mark.parametrize("address", [None, "0.0.0.0", "::", "192.168.1.10"])
def test_cache_maintenance_is_not_exposed_on_remote_capable_hosts(tmp_path, monkeypatch, address):
    import streamlit as st

    original = st.get_option
    monkeypatch.setattr(
        st, "get_option", lambda name: address if name == "server.address" else original(name)
    )
    data_clear, resource_clear = Mock(), Mock()
    monkeypatch.setattr(st.cache_data, "clear", data_clear)
    monkeypatch.setattr(st.cache_resource, "clear", resource_clear)
    service, call = fixture(tmp_path)
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert button(app, "清理应用缓存").disabled
    app.session_state["workbench_cache_dialog"] = True
    app.run()
    assert not any(b.label == "确认清理缓存" for b in app.button)
    data_clear.assert_not_called()
    resource_clear.assert_not_called()
    call.assert_not_called()


def test_cache_maintenance_failure_is_generic_and_retry_is_explicit(tmp_path, monkeypatch):
    import streamlit as st

    service, call = fixture(tmp_path)
    data_clear = Mock(side_effect=RuntimeError("private-details"))
    resource_clear = Mock()
    monkeypatch.setattr(st.cache_data, "clear", data_clear)
    monkeypatch.setattr(st.cache_resource, "clear", resource_clear)
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "清理应用缓存").click().run()
    button(app, "确认清理缓存").click().run()
    assert not app.exception and app.error
    assert "private-details" not in app.error[0].value
    data_clear.assert_called_once_with()
    resource_clear.assert_not_called()
    app.run()
    assert data_clear.call_count == 1
    assert len(service.repository.list_sessions()) == 1
    call.assert_not_called()


def test_qa_citations_visible_without_configuration_draft(tmp_path):
    service, _ = fixture(tmp_path)
    service.repository.update_revision(
        session_id="session:a",
        expected_revision=2,
        actor="test",
        reason="qa",
        state_update={
            "intent": "atomic_query",
            "configuration_tasks": [],
            "evidence_registry": [],
            "answer_status": "answered",
            "answer_evidence": [
                {
                    "evidence_id": "q:1",
                    "source": "qa.pdf",
                    "page_start": 6,
                    "excerpt": "A verified question citation",
                    "citation_index": 1,
                }
            ],
        },
    )
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert any(item.label == "[1] qa.pdf" for item in app.expander)
    assert any("知识问答" in item.value for item in app.info)
    assert button(app, "验证草稿").disabled
    assert button(app, "提交审查").disabled


def test_failed_chat_remains_visible_and_can_be_retried_manually(tmp_path):
    service, call = fixture(tmp_path)
    call.side_effect = RuntimeError("private-token")
    app = AppTest.from_function(render, args=(service,)).run()
    app.chat_input[0].set_value("新问题").run()
    assert not app.exception
    assert any(item.value == "新问题" for item in app.text)
    assert app.error and "private-token" not in app.error[0].value
    assert not app.chat_input[0].disabled
    app.run()
    assert call.call_count == 1


def test_sidebar_delete_and_restore_preserve_conversation(tmp_path):
    service, call = fixture(tmp_path)
    before = service.repository.get_revision("session:a")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "删除对话").click().run()
    assert not app.exception
    assert len(service.repository.list_sessions()) == 1
    button(app, "确认删除").click().run()
    assert not app.exception
    assert service.repository.list_sessions() == ()
    assert len(service.deleted_rows()) == 1
    assert not app.chat_message
    button(app, "恢复").click().run()
    assert not app.exception
    assert service.repository.get_revision("session:a") == before
    assert len(app.chat_message) == 1
    assert not service.deleted_rows()
    call.assert_not_called()


def test_row_actions_and_search_preserve_other_selected_conversation(tmp_path):
    service, call = fixture(tmp_path)
    service.repository.create_session(session_id="session:b", goal="Other conversation")
    app = AppTest.from_function(render, args=(service,)).run()
    selection = "workbench_session:workspace:legacy"
    next(b for b in app.button if b.key == f"select:{selection}:session:a").click().run()
    next(b for b in app.selectbox if b.label == "对话轮次").select(1).run()
    next(b for b in app.button if b.key == f"delete:{selection}:session:b").click().run()
    button(app, "确认删除").click().run()
    assert not app.exception
    assert app.session_state[selection] == "session:a"
    assert next(b for b in app.selectbox if b.label == "对话轮次").value == 1
    button(app, "恢复").click().run()
    assert app.session_state[selection] == "session:a"
    app.text_input[0].set_value("no match").run()
    assert app.session_state[selection] == "session:a"
    assert len(app.chat_message) == 1
    call.assert_not_called()


def test_delete_confirmation_cancel_and_rename_cancel_do_not_mutate(tmp_path):
    service, call = fixture(tmp_path)
    before = service.repository.get_session("session:a")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "删除对话").click().run()
    assert button(app, "确认删除")
    button(app, "取消").click().run()
    assert not app.exception
    assert service.repository.get_session("session:a") == before
    button(app, "重命名").click().run()
    next(t for t in app.text_input if t.label == "对话名称").set_value("不应保存")
    button(app, "取消").click().run()
    assert not app.exception
    assert service.repository.get_session("session:a") == before
    call.assert_not_called()


def test_clarification_completion_selects_latest_and_removes_prompt(tmp_path):
    service, call = fixture(tmp_path)

    def complete(_tool, fields):
        assert fields["expected_revision"] == 2
        latest = service.repository.update_revision(
            session_id="session:a",
            expected_revision=2,
            actor="test",
            reason="complete",
            state_update={"status": "review_required", "open_questions": [], "pause_reason": ""},
        )
        service.repository.append_turn(
            session_id="session:a",
            expected_revision=latest.revision,
            role="assistant",
            message="需求已补齐，方案等待审查。",
        )
        return {"structuredContent": {"session_id": "session:a", "revision": latest.revision}}

    call.side_effect = complete
    app = AppTest.from_function(render, args=(service,)).run()
    assert any(e.label == "需要补充的信息" for e in app.expander)
    app.chat_input[0].set_value("站点是 DC01").run()
    assert not app.exception
    assert next(b for b in app.selectbox if b.label == "对话轮次").value == 3
    assert not any(e.label == "需要补充的信息" for e in app.expander)
    assert any("需求已补齐" in m.value for m in app.markdown)
    assert call.call_count == 1


def test_answered_fields_and_nonclarification_pauses_hide_stale_questions(tmp_path):
    service, _ = fixture(tmp_path)
    repo = service.repository
    repo.update_revision(
        session_id="session:a",
        expected_revision=2,
        actor="test",
        reason="partial",
        state_update={
            "pause_reason": "requirements_missing",
            "confirmed_context": {"site": "DC01"},
            "open_questions": [
                {"text": "Which site?", "reason": "required_context_missing:site"},
                {"text": "Which environment?", "reason": "required_context_missing:environment"},
            ],
        },
    )
    assert [q["text"] for q in service.view("session:a", 3)["questions"]] == ["Which environment?"]
    repo.update_revision(
        session_id="session:a",
        expected_revision=3,
        actor="test",
        reason="timeout",
        state_update={"pause_reason": "turn_timeout"},
    )
    assert service.view("session:a", 4)["questions"] == []
    assert len(service.view("session:a", 3)["questions"]) == 1


def test_rename_menu_targets_its_own_conversation(tmp_path):
    service, call = fixture(tmp_path)
    service.repository.create_session(session_id="session:b", goal="Original goal")
    original = service.repository.get_revision("session:b")
    app = AppTest.from_function(render, args=(service,)).run()
    selection = "workbench_session:workspace:legacy"
    next(b for b in app.button if b.key == f"select:{selection}:session:a").click().run()
    next(b for b in app.button if b.key == f"rename-action:{selection}:session:b").click().run()
    assert not app.exception
    next(t for t in app.text_input if t.label == "对话名称").set_value("改名后的对话")
    button(app, "保存名称").click().run()
    assert not app.exception
    assert service.repository.get_session("session:b").display_title == "改名后的对话"
    assert service.repository.get_revision("session:b") == original
    assert app.session_state[selection] == "session:a"
    call.assert_not_called()


def _trash_rows(service, *ids):
    for sid in ids:
        service.repository.create_session(session_id=sid, goal=f"Archived {sid}")
        service.repository.delete_session(sid)


def test_multi_select_purge_requires_confirmation_and_preserves_active_view(tmp_path):
    service, call = fixture(tmp_path)
    _trash_rows(service, "b", "c", "d")
    active = service.repository.get_revision("session:a")
    app = AppTest.from_function(render, args=(service,)).run()
    assert button(app, "删除所选").disabled
    for sid in ("b", "c"):
        next(c for c in app.checkbox if c.key == f"trash-selected:workspace:legacy:{sid}").check()
    app.run()
    button(app, "删除所选").click().run()
    assert not app.exception
    assert len(service.deleted_rows()) == 3
    button(app, "取消").click().run()
    assert len(service.deleted_rows()) == 3
    button(app, "删除所选").click().run()
    button(app, "确认永久删除").click().run()
    assert not app.exception
    assert [r["session_id"] for r in service.deleted_rows()] == ["d"]
    assert service.repository.get_revision("session:a") == active
    assert app.session_state["workbench_session:workspace:legacy"] == "session:a"
    assert len(app.chat_message) == 1
    call.assert_not_called()


def test_empty_bin_deletes_confirmed_snapshot_only_and_shows_empty_state(tmp_path):
    service, call = fixture(tmp_path)
    _trash_rows(service, "b", "c")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "清空回收站").click().run()
    _trash_rows(service, "arrived-later")
    button(app, "确认永久删除").click().run()
    assert not app.exception
    assert [r["session_id"] for r in service.deleted_rows()] == ["arrived-later"]
    button(app, "清空回收站").click().run()
    button(app, "确认永久删除").click().run()
    assert not app.exception
    assert service.deleted_rows() == []
    assert any(e.label == "回收站（0）" for e in app.expander)
    assert any(c.value == "回收站为空。" for c in app.caption)
    assert service.repository.get_session("session:a")
    call.assert_not_called()


def test_restored_target_rejects_whole_selected_purge(tmp_path):
    service, _ = fixture(tmp_path)
    _trash_rows(service, "b", "c")
    app = AppTest.from_function(render, args=(service,)).run()
    button(app, "全选").click().run()
    assert button(app, "取消全选")
    button(app, "删除所选").click().run()
    service.repository.restore_session("b")
    button(app, "确认永久删除").click().run()
    assert not app.exception and app.error
    assert [r["session_id"] for r in service.deleted_rows()] == ["c"]
    assert service.repository.get_session("b")


def test_bin_controls_are_disabled_while_a_reply_is_running(tmp_path):
    service, _ = fixture(tmp_path)
    _trash_rows(service, "b")
    app = AppTest.from_function(render, args=(service,)).run()
    app.session_state["workbench_busy"] = True
    app.run()
    assert not app.exception
    for label in ("全选", "恢复", "删除所选", "清空回收站"):
        assert button(app, label).disabled
    assert next(c for c in app.checkbox if c.key == "trash-selected:workspace:legacy:b").disabled


def test_each_answer_uses_its_own_evidence_and_old_bibliography_is_folded(tmp_path):
    service, _ = fixture(tmp_path)
    repo = service.repository
    first = {
        "evidence_id": "e:first",
        "source": "first.pdf",
        "citation_index": 1,
        "page_start": 1,
        "excerpt": "First original source. [IMAGE: short_1_1]",
    }
    second = {
        "evidence_id": "e:second",
        "source": "second.pdf",
        "citation_index": 1,
        "page_start": 2,
        "excerpt": "Second original source.",
    }
    repo.update_revision(
        session_id="session:a",
        expected_revision=2,
        actor="test",
        reason="first",
        state_update={"pause_reason": "question_answered", "answer_evidence": [first]},
    )
    repo.append_turn(
        session_id="session:a",
        expected_revision=3,
        role="assistant",
        message="First answer [1]\n\n引用依据\n[1] first.pdf\n原文：First original source.",
    )
    repo.update_revision(
        session_id="session:a",
        expected_revision=3,
        actor="test",
        reason="second",
        state_update={"answer_evidence": [second]},
    )
    repo.append_turn(
        session_id="session:a",
        expected_revision=4,
        role="assistant",
        message="Second answer [1]",
        metadata={"citations": [second]},
    )
    turns = [t for t in service.view("session:a", 4)["turns"] if t["role"] == "assistant"]
    assert turns[0]["citations"][0]["source"] == "first.pdf"
    assert turns[1]["citations"][0]["source"] == "second.pdf"
    assert "first.pdf" not in turns[0]["message"]
    app = AppTest.from_function(render, args=(service,)).run()
    assert not app.exception
    assert not any("original source." in t.value for t in app.text)
    assert not any("引用依据" in m.value for m in app.markdown)
    key = f"answer-evidence:workspace:legacy:session:a:{turns[0]['turn_id']}"
    assert not app.session_state[key]
    app.session_state[key] = True
    app.run()
    assert not app.exception
    assert any(t.value == "First original source." for t in app.text)
    assert not any(t.value == "Second original source." for t in app.text)
    assert repo.list_turns("session:a")[1].message.endswith("原文：First original source.")


def test_expander_defers_image_resolution_until_opened(tmp_path):
    from unittest.mock import patch

    service, _ = fixture(tmp_path)
    repo = service.repository
    citations = [
        {
            "evidence_id": "e:1",
            "source": "manual.pdf",
            "excerpt": "Source evidence",
            "page_start": 1,
            "citation_index": 1,
        }
    ]
    turn = repo.append_turn(
        session_id="session:a",
        expected_revision=2,
        role="assistant",
        message="Answer [1]",
        metadata={"citations": citations},
    )
    with patch.object(service, "present_evidence", wraps=service.present_evidence) as present:
        app = AppTest.from_function(render, args=(service,)).run()
        assert not app.exception
        present.assert_not_called()
        app.session_state[f"answer-evidence:workspace:legacy:session:a:{turn.turn_id}"] = True
        app.run()
        assert not app.exception
        present.assert_called_once()
