"""User-facing workbench; mutations happen only on explicit form submission."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

import streamlit as st

from agents.repositories.feedback_repository import FEEDBACK_KINDS, REGENERATION_REASONS
from observability.dashboard.services.workbench_service import WorkbenchService

_BUSY = "workbench_busy"
_PENDING = "workbench_pending_message"
_FAILED = "workbench_failed_message"


def _queue_message(key, workspace_id, session_id, revision):
    message = st.session_state.get(key)
    if isinstance(message, str) and message.strip() and not st.session_state.get(_BUSY):
        st.session_state[_PENDING] = {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "revision": revision,
            "message": message,
        }
        st.session_state[_BUSY] = True
        st.session_state.pop(_FAILED, None)
        st.session_state[f"workbench_view:{workspace_id}:{session_id}"] = "对话"


def _prefill_message(key, message):
    st.session_state[key] = message


def _render_pending(entry):
    with st.chat_message("user"):
        st.text(entry["message"])
    with st.chat_message("assistant", avatar=":material/auto_awesome:"):
        indicator = st.empty()
        indicator.markdown(
            '<div class="chat-thinking" role="status" aria-live="polite">'
            '<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>'
            "<span>正在处理你的问题，整理证据与回答…</span></div>",
            unsafe_allow_html=True,
        )
    return indicator


def _process_message(service, entry, indicator):
    error = None
    result = None
    try:
        if entry["session_id"]:
            result = service.act(
                "continue", entry["session_id"], entry["revision"], message=entry["message"]
            )
        else:
            result = service.start(entry["message"])
        if result.get("isError"):
            error = "请求未完成。请检查当前版本和输入后再重试。"
    except Exception:
        error = "暂时未能完成回答。请检查历史是否已保存，再重试；不会自动重复发送。"
    finally:
        st.session_state[_BUSY] = False
        indicator.empty()
    if error:
        st.session_state[_FAILED] = {**entry, "error": error}
    else:
        _select_result(result)
    st.rerun()


def _select_result(result):
    payload = result.get("structuredContent", {})
    if payload.get("session_id") and isinstance(payload.get("revision"), int):
        st.session_state["workbench_target"] = (payload["session_id"], payload["revision"])
    st.session_state["workbench_notice"] = "操作完成，已显示返回的会话版本。"


def _render_failed(workspace_id, session_id, turns):
    failed = st.session_state.get(_FAILED)
    if failed and (failed["workspace_id"], failed["session_id"]) == (workspace_id, session_id):
        if not turns or turns[-1]["role"] != "user" or turns[-1]["message"] != failed["message"]:
            with st.chat_message("user"):
                st.text(failed["message"])
        with st.chat_message("assistant", avatar=":material/auto_awesome:"):
            st.error(failed["error"])


def _delete_conversation(service, session_id):
    try:
        service.delete_conversation(session_id)
    except Exception:
        st.error("未能删除对话，请刷新后重试。")
        return
    failed = st.session_state.get(_FAILED)
    if failed and failed["session_id"] == session_id:
        st.session_state.pop(_FAILED, None)
    selection = f"workbench_session:{service.repository.workspace_id}"
    if st.session_state.get(selection) == session_id:
        st.session_state[selection] = ""
    st.session_state["workbench_notice"] = "对话已移至回收站，可以恢复。"
    st.rerun()


def _new_conversation(workspace_id):
    st.session_state[f"workbench_session:{workspace_id}"] = ""
    st.session_state[f"new_message:{workspace_id}"] = ""
    st.session_state["chat_history_search"] = ""


def _select_conversation(key, session_id):
    st.session_state[key] = session_id


def _remember_revision(widget_key, selection_key):
    st.session_state[selection_key] = st.session_state[widget_key]


def _close_rename():
    st.session_state.pop("workbench_rename_target", None)


@st.dialog("重命名对话", on_dismiss=_close_rename)
def _rename_conversation(service, session_id, current_title):
    with st.form(f"rename:{service.repository.workspace_id}:{session_id}"):
        title = st.text_input("对话名称", value=current_title[:120], max_chars=120)
        if st.form_submit_button("保存名称", type="primary"):
            try:
                service.rename_conversation(session_id, title)
            except ValueError:
                st.error("名称不能为空，且需为不超过 120 个字符的单行文本。")
                return
            except Exception:
                st.error("未能重命名对话，请刷新后重试。")
                return
            st.session_state["workbench_notice"] = "对话名称已更新。"
            _close_rename()
            st.rerun()
    if st.button("取消"):
        _close_rename()
        st.rerun()


def _history_title(text):
    title = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"([\\`*_\[\]()<>!])", r"\\\1", title[:48]) + ("…" if len(title) > 48 else "")


def _render_history(service, names, visible, session_key, busy):
    current = st.session_state[session_key]
    for session_id in visible:
        identity = hashlib.sha256(session_id.encode()).hexdigest()[:12]
        with st.container(key=f"history-row-{identity}"):
            title, actions = st.columns([5, 1], gap="small", vertical_alignment="center")
            title.button(
                _history_title(names[session_id]),
                key=f"select:{session_key}:{session_id}",
                help=_safe_markdown(names[session_id]),
                use_container_width=True,
                wrap=False,
                type="primary" if session_id == current else "secondary",
                disabled=busy,
                on_click=_select_conversation,
                args=(session_key, session_id),
            )
            with actions.popover(
                "⋯",
                help="对话操作",
                disabled=busy,
                use_container_width=True,
                key=f"menu:{session_key}:{session_id}",
            ):
                st.caption(_safe_markdown(names[session_id]))
                if st.button(
                    "重命名", key=f"rename-action:{session_key}:{session_id}", disabled=busy
                ):
                    st.session_state["workbench_rename_target"] = session_id
                if st.button(
                    "删除对话",
                    key=f"delete:{session_key}:{session_id}",
                    icon=":material/delete:",
                    disabled=busy,
                    help="移到回收站，可恢复。",
                ):
                    _delete_conversation(service, session_id)


def _restore_conversation(service, session_id):
    try:
        target = service.restore_conversation(session_id)
    except Exception:
        st.error("未能恢复对话，请刷新后重试。")
        return
    selection = f"workbench_session:{service.repository.workspace_id}"
    if not st.session_state.get(selection):
        st.session_state["workbench_target"] = target
    st.session_state["workbench_notice"] = "对话已恢复。"
    st.rerun()


def _submit(operation, *, refresh=True):
    if st.session_state.get(_BUSY):
        st.info("请等待当前回答完成。")
        return
    try:
        with st.spinner("正在检索资料并整理回答，请稍候…"):
            result = operation()
    except Exception:
        st.error("操作失败：请刷新版本并检查权限、状态或服务配置。未自动重试。")
        return
    if result.get("isError"):
        st.error("请求被服务端拒绝。请刷新并检查版本、审批条件及输入。")
        return
    if refresh:
        _select_result(result)
        st.rerun()
    else:
        st.dataframe(result["structuredContent"].get("signals", []), hide_index=True)


def _text_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            st.text(str(key))
            _text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _text_values(item)
    else:
        st.text(str(value) if value is not None else "尚未提供")


def _safe_markdown(text: str) -> str:
    # Display generated Markdown without remote images or raw HTML.
    return html.escape(text, quote=False).replace("![", "[")


def render_workbench(service: WorkbenchService) -> None:
    st.markdown(
        "<style>"
        + Path(__file__).with_name("workbench.css").read_text(encoding="utf-8")
        + "</style>",
        unsafe_allow_html=True,
    )
    workspace = service.repository.workspace
    busy = bool(st.session_state.get(_BUSY))
    rows = service.list_rows()
    names = {row["Session"]: row.get("Title") or row["Goal"] for row in rows}
    session_key = f"workbench_session:{workspace.workspace_id}"
    if session_key not in st.session_state:
        st.session_state[session_key] = next(iter(names), "")
    target = st.session_state.pop("workbench_target", None)
    if target and target[0] in names:
        st.session_state[session_key] = target[0]
        st.session_state[f"revision:{workspace.workspace_id}:{target[0]}"] = target[1]
        st.session_state[f"selected_revision:{workspace.workspace_id}:{target[0]}"] = target[1]
    with st.sidebar:
        st.markdown("### ◈ WMS Assistant")
        st.caption("知识问答 · 配置协作")
        st.button(
            "＋ 新对话",
            use_container_width=True,
            type="primary",
            disabled=not service.enabled or busy,
            on_click=_new_conversation,
            args=(workspace.workspace_id,),
        )
        search = st.text_input(
            "搜索会话", placeholder="搜索历史对话", key="chat_history_search", disabled=busy
        )
        visible = [key for key in names if search.casefold() in names[key].casefold()]
        if st.session_state[session_key] not in names:
            st.session_state[session_key] = ""
        st.caption("最近对话")
        _render_history(service, names, visible, session_key, busy)
        session_id = st.session_state[session_key]
        if not rows:
            st.caption("暂无会话，发送第一条消息开始。")
        elif not visible:
            st.caption("没有匹配的历史会话。")
        deleted = service.deleted_rows()
        if deleted:
            with st.expander(f"回收站（{len(deleted)}）"):
                st.caption("恢复会保留原有对话、版本和审批记录；已导出文件不会被删除。")
                for item in deleted:
                    title, action = st.columns([3, 1])
                    title.text(re.sub(r"\s+", " ", item["goal"])[:44])
                    if action.button(
                        "恢复",
                        key=f"restore:{workspace.workspace_id}:{item['session_id']}",
                        disabled=busy,
                    ):
                        _restore_conversation(service, item["session_id"])
        st.divider()
        st.caption("当前工作空间")
        st.text(workspace.name)
        with st.expander("测试范围与隐私"):
            st.caption(workspace.workspace_id)
            st.caption("主机配置决定 Workspace；这里不是权限切换入口。")
            st.caption("问题及相关片段可能发送给配置的模型。请勿输入密钥或敏感信息。")
            st.caption("只生成配置建议，不执行真实 WMS 写入。审批与导出仍需显式操作。")
    if rename_target := st.session_state.get("workbench_rename_target"):
        if rename_target in names:
            _rename_conversation(service, rename_target, names[rename_target])
        else:
            _close_rename()
    with st.container(key="workbench_header"):
        st.title("WMS Workspace")
        st.caption("对话、证据和配置方案，在同一工作区协作。")
    if notice := st.session_state.pop("workbench_notice", None):
        st.toast(notice)
    if not service.enabled:
        st.info("Agent 未启用：仅可查看已保存会话。")
    if not session_id:
        composer_key = f"new_message:{workspace.workspace_id}"
        st.chat_input(
            "询问 WMS 问题，或描述你的配置目标…",
            key=composer_key,
            disabled=not service.enabled or busy,
            on_submit=_queue_message,
            args=(composer_key, workspace.workspace_id, "", None),
        )
        entry = st.session_state.pop(_PENDING, None)
        if entry:
            indicator = _render_pending(entry)
            _process_message(service, entry, indicator)
            return
        _render_failed(workspace.workspace_id, "", [])
        st.markdown(
            '<div class="welcome"><div class="welcome-symbol">✦</div>'
            "<h2>今天想解决什么 WMS 问题？</h2>"
            "<p>查询配置、核对证据，或一起规划一个完整方案。</p></div>",
            unsafe_allow_html=True,
        )
        with st.container(key="suggestions"):
            prompts = [
                ("查一个配置", "如何配置 trolley picking？请给出依据。"),
                ("排查一个问题", "RF 操作不可见时，应该先检查哪些配置？"),
                ("规划一个流程", "帮我规划一个入库收货流程，请先确认所需条件。"),
            ]
            for column, (label, prompt) in zip(st.columns(3), prompts, strict=True):
                column.button(
                    label,
                    use_container_width=True,
                    disabled=not service.enabled or busy,
                    on_click=_prefill_message,
                    args=(composer_key, prompt),
                )
        st.markdown(
            '<div class="chat-footnote">回答基于文档证据，重要配置请人工核验。</div>',
            unsafe_allow_html=True,
        )
        return
    revisions = service.repository.list_revisions(session_id)
    versions = [item.revision for item in reversed(revisions)]
    revision_key = f"revision:{workspace.workspace_id}:{session_id}"
    selected_revision_key = f"selected_revision:{workspace.workspace_id}:{session_id}"
    selected_revision = st.session_state.get(selected_revision_key)
    if selected_revision in versions:
        st.session_state[revision_key] = selected_revision
    with st.sidebar:
        revision = st.selectbox(
            "查看版本",
            versions,
            key=revision_key,
            disabled=busy,
            on_change=_remember_revision,
            args=(revision_key, selected_revision_key),
        )
    st.session_state[selected_revision_key] = revision
    view = service.view(session_id, revision)
    st.subheader(_safe_markdown(names[session_id]))
    st.caption(f"版本 {revision} · {view['next_step']}")
    historical = revision != view["current_revision"]
    if historical:
        st.warning("正在查看历史版本：对话、验证、审批与导出已禁用。")
    disabled = not service.enabled or historical or busy
    composer_key = f"message:{workspace.workspace_id}:{session_id}:{revision}"
    st.chat_input(
        "继续提问，或补充配置需求…",
        key=composer_key,
        disabled=disabled or view["status"] != "paused",
        on_submit=_queue_message,
        args=(composer_key, workspace.workspace_id, session_id, revision),
    )
    entry = st.session_state.pop(_PENDING, None)
    chat, workspace_panel = st.tabs(
        ["对话", "工作区"],
        key=f"workbench_view:{workspace.workspace_id}:{session_id}",
        default="对话",
    )
    with chat:
        for turn in view["turns"]:
            with st.chat_message(
                turn["role"],
                avatar=":material/auto_awesome:" if turn["role"] == "assistant" else None,
            ):
                if turn["role"] == "user":
                    st.text(turn["message"])
                else:
                    st.markdown(_safe_markdown(turn["message"]))
        if not view["turns"]:
            st.info("这个版本还没有对话记录。")
        _render_failed(workspace.workspace_id, session_id, view["turns"])
        if entry:
            indicator = _render_pending(entry)
        if view["questions"]:
            with st.expander("需要补充的信息", expanded=True):
                for question in view["questions"]:
                    st.text(str(question.get("text", "待补充需求")))
    with workspace_panel:
        st.caption("当前工作区内容绑定这条对话和所选版本。")
        tasks, sources, review = st.columns(3)
        tasks.metric("配置任务", len(view["tasks"]))
        sources.metric("证据片段", len(view["answer_evidence"]) + len(view["evidence"]))
        review.metric(
            "审查状态",
            {
                "approved": "已批准",
                "review_required": "待审查",
                "rejected": "已拒绝",
                "cancelled": "已取消",
            }.get(view["status"], "待验证" if view["tasks"] else "尚未生成方案"),
        )
        _render_details(service, session_id, revision, view, disabled)
    if entry:
        _process_message(service, entry, indicator)
        return
    st.markdown(
        '<div class="chat-footnote">AI 回答可能存在错误，请结合引用核验。不会自动写入 WMS。</div>',
        unsafe_allow_html=True,
    )


def _render_details(service, session_id, revision, view, disabled):
    draft, evidence, review, feedback = st.tabs(
        ["配置草稿与依赖", "引用证据", "审查与导出", "反馈"]
    )
    with draft:
        st.subheader("已确认上下文")
        _text_values(view["context"])
        if not view["tasks"]:
            st.info(
                "这轮是知识问答，尚未生成配置草稿。描述完整配置目标后，可进入方案规划。"
                if view["is_question"]
                else "需求尚未形成配置草稿。请继续补充配置条件。"
            )
        else:
            st.graphviz_chart(view["dag"])
        for task in view["tasks"]:
            with st.expander(str(task.get("title", "配置任务")), expanded=True):
                st.text(f"状态：{task.get('status', 'draft')}")
                st.text(f"证据：{task.get('evidence_status', 'unsupported')}")
                for field, label in (
                    ("goal", "目标"),
                    ("preconditions", "前置条件"),
                    ("parameters", "参数"),
                    ("steps", "配置步骤"),
                    ("validation_steps", "验证步骤"),
                    ("rollback_steps", "回退步骤"),
                ):
                    st.text(label)
                    _text_values(task.get(field) or "尚未提供")
    with evidence:
        if view["answer_evidence"]:
            st.subheader("本次回答的引用")
        elif view["legacy_answer_evidence"]:
            st.info("这条历史回答尚未保存结构化引用。重新提问后可在这里查看对应证据。")
        elif view["is_question"]:
            st.info("本轮尚未形成通过校验的引用，回答中的证据缺口仍需补充。")
        if view["evidence"]:
            st.subheader("配置任务的证据")
        elif not view["is_question"]:
            st.info("尚无配置任务证据；请先完成需求与规划，再检查证据覆盖。")
        for item in [*view["answer_evidence"], *view["evidence"]]:
            title = (
                f"[{item['citation_index']}] {item['source']}"
                if item.get("citation_index")
                else str(item["evidence_id"])
            )
            with st.expander(title, expanded=True):
                st.text(f"来源：{item['source']} · 页码：{item['page_start'] or '未知'}")
                st.text(f"文档版本：{item['product_version'] or '未知'}")
                st.text(item["excerpt"])
        for binding in view["bindings"]:
            st.text(f"{binding.get('task_id')}: {binding.get('evidence_status')}")
            st.text("引用：" + ", ".join(binding.get("evidence_ids", [])))
            for gap in binding.get("gap_reasons", []):
                st.warning(str(gap))
    with review:
        if not view["tasks"]:
            st.info(
                "当前没有配置草稿可审查。普通问答无需审批；生成草稿并通过验证后才能批准和导出。"
            )
        for finding in [*view["conflicts"], *view["findings"]]:
            _text_values(finding)
        st.dataframe(view["approvals"], hide_index=True)
        terminal = view["status"] in {"approved", "rejected", "cancelled"}
        if st.button("验证草稿", disabled=disabled or terminal or not view["tasks"]):
            _submit(lambda: service.act("validate", session_id, revision))
        with st.form(f"review:{session_id}:{revision}"):
            decision = st.selectbox("审查决定", ["revise", "reject", "approve"])
            comment = st.text_area("审查意见（必填）")
            confirmed = st.checkbox("我已检查当前版本草稿、引用与风险")
            ready = view["status"] == "review_required"
            if st.form_submit_button("提交审查", disabled=disabled or not ready):
                if confirmed and comment.strip():
                    _submit(
                        lambda: service.act(
                            "review", session_id, revision, decision=decision, comment=comment
                        )
                    )
                else:
                    st.warning("请填写审查意见并明确确认。")
        if st.button("导出已批准方案", disabled=disabled or view["status"] != "approved"):
            _submit(lambda: service.act("export", session_id, revision, format="markdown"))
    with feedback:
        st.caption("记录所选版本的问题信号，不会自动重新生成；请勿输入敏感信息。")
        with st.form(f"feedback:{session_id}:{revision}"):
            kind = st.selectbox("反馈类别", FEEDBACK_KINDS)
            reason = st.selectbox("重新生成原因（仅 regeneration 使用）", REGENERATION_REASONS)
            if st.form_submit_button("记录反馈", disabled=not service.enabled):
                _submit(
                    lambda: service.act(
                        "feedback",
                        session_id,
                        revision,
                        kind=kind,
                        reason=reason if kind == "regeneration" else "",
                    )
                )
        if st.button("查看反馈汇总", disabled=not service.enabled):
            _submit(lambda: service.act("summary", session_id, revision), refresh=False)
