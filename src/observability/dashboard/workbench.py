"""User-facing workbench; mutations happen only on explicit form submission."""

from __future__ import annotations

import html
import re
from pathlib import Path

import streamlit as st

from agents.repositories.feedback_repository import FEEDBACK_KINDS, REGENERATION_REASONS
from observability.dashboard.services.workbench_service import WorkbenchService


def _submit(operation, *, refresh=True):
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
        payload = result.get("structuredContent", {})
        if payload.get("session_id") and isinstance(payload.get("revision"), int):
            st.session_state["workbench_target"] = (payload["session_id"], payload["revision"])
        st.session_state["workbench_notice"] = "操作完成，已显示返回的会话版本。"
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
    rows = service.list_rows()
    names = {row["Session"]: row["Goal"] for row in rows}
    session_key = f"workbench_session:{workspace.workspace_id}"
    if session_key not in st.session_state:
        st.session_state[session_key] = next(iter(names), "")
    target = st.session_state.pop("workbench_target", None)
    if target and target[0] in names:
        st.session_state[session_key] = target[0]
        st.session_state[f"revision:{workspace.workspace_id}:{target[0]}"] = target[1]
    with st.sidebar:
        st.markdown("### ◈ WMS Assistant")
        st.caption("知识问答 · 配置协作")
        if st.button(
            "＋ 新对话", use_container_width=True, type="primary", disabled=not service.enabled
        ):
            st.session_state[session_key] = ""
        search = st.text_input("搜索会话", placeholder="搜索历史对话", key="chat_history_search")
        visible = [key for key in names if search.casefold() in names[key].casefold()]
        if st.session_state[session_key] not in visible:
            st.session_state[session_key] = ""
        st.caption("最近对话")
        session_id = st.radio(
            "会话",
            ["", *visible],
            key=session_key,
            format_func=lambda key: "新对话" if not key else (re.sub(r"\s+", " ", names[key])[:52]),
            label_visibility="collapsed",
        )
        if not rows:
            st.caption("暂无会话，发送第一条消息开始。")
        elif not visible:
            st.caption("没有匹配的历史会话。")
        st.divider()
        st.caption("当前工作空间")
        st.text(workspace.name)
        with st.expander("测试范围与隐私"):
            st.caption(workspace.workspace_id)
            st.caption("主机配置决定 Workspace；这里不是权限切换入口。")
            st.caption("问题及相关片段可能发送给配置的模型。请勿输入密钥或敏感信息。")
            st.caption("只生成配置建议，不执行真实 WMS 写入。审批与导出仍需显式操作。")
    with st.container(key="workbench_header"):
        st.title("WMS Assistant")
        st.caption("有据可查的回答，逐步完成的配置。")
    if notice := st.session_state.pop("workbench_notice", None):
        st.toast(notice)
    if not service.enabled:
        st.info("Agent 未启用：仅可查看已保存会话。")
    if not session_id:
        st.markdown(
            '<div class="welcome"><div class="welcome-symbol">✦</div>'
            "<h2>今天想解决什么 WMS 问题？</h2>"
            "<p>查询配置、核对证据，或一起规划一个完整方案。</p></div>",
            unsafe_allow_html=True,
        )
        composer_key = f"new_message:{workspace.workspace_id}"
        with st.container(key="suggestions"):
            prompts = [
                ("查一个配置", "如何配置 trolley picking？请给出依据。"),
                ("排查一个问题", "RF 操作不可见时，应该先检查哪些配置？"),
                ("规划一个流程", "帮我规划一个入库收货流程，请先确认所需条件。"),
            ]
            for column, (label, prompt) in zip(st.columns(3), prompts, strict=True):
                if column.button(label, use_container_width=True, disabled=not service.enabled):
                    st.session_state[composer_key] = prompt
        st.markdown(
            '<div class="chat-footnote">回答基于文档证据，重要配置请人工核验。</div>',
            unsafe_allow_html=True,
        )
        if message := st.chat_input(
            "询问 WMS 问题，或描述你的配置目标…", key=composer_key, disabled=not service.enabled
        ):
            _submit(lambda: service.start(message))
        return
    revisions = service.repository.list_revisions(session_id)
    with st.sidebar:
        revision = st.selectbox(
            "查看版本",
            [item.revision for item in reversed(revisions)],
            key=f"revision:{workspace.workspace_id}:{session_id}",
        )
    view = service.view(session_id, revision)
    st.caption(f"版本 {revision} · {view['status']} · {view['next_step']}")
    historical = revision != view["current_revision"]
    if historical:
        st.warning("正在查看历史版本：对话、验证、审批与导出已禁用。")
    disabled = not service.enabled or historical
    for turn in view["turns"]:
        with st.chat_message(
            turn["role"], avatar=":material/auto_awesome:" if turn["role"] == "assistant" else None
        ):
            if turn["role"] == "user":
                st.text(turn["message"])
            else:
                st.markdown(_safe_markdown(turn["message"]))
    if not view["turns"]:
        st.info("这个版本还没有对话记录。")
    if view["questions"]:
        with st.expander("需要补充的信息", expanded=True):
            for question in view["questions"]:
                st.text(str(question.get("text", "待补充需求")))
    with st.expander("草稿、证据与审批", expanded=False):
        _render_details(service, session_id, revision, view, disabled)
    st.markdown(
        '<div class="chat-footnote">AI 回答可能存在错误，请结合引用核验。不会自动写入 WMS。</div>',
        unsafe_allow_html=True,
    )
    if message := st.chat_input(
        "继续提问，或补充配置需求…",
        key=f"message:{workspace.workspace_id}:{session_id}:{revision}",
        disabled=disabled or view["status"] != "paused",
    ):
        _submit(lambda: service.act("continue", session_id, revision, message=message))


def _render_details(service, session_id, revision, view, disabled):
    draft, evidence, review, feedback = st.tabs(
        ["配置草稿与依赖", "引用证据", "审查与导出", "反馈"]
    )
    with draft:
        st.subheader("已确认上下文")
        _text_values(view["context"])
        if not view["tasks"]:
            st.info("需求尚未形成配置草稿。")
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
        if not view["evidence"]:
            st.warning("暂无可核验引用；不能将草稿视为已验证配置。")
        for item in view["evidence"]:
            with st.expander(str(item["evidence_id"]), expanded=True):
                st.text(f"来源：{item['source']} · 页码：{item['page_start'] or '未知'}")
                st.text(f"文档版本：{item['product_version'] or '未知'}")
                st.text(item["excerpt"])
        for binding in view["bindings"]:
            st.text(f"{binding.get('task_id')}: {binding.get('evidence_status')}")
            st.text("引用：" + ", ".join(binding.get("evidence_ids", [])))
            for gap in binding.get("gap_reasons", []):
                st.warning(str(gap))
    with review:
        for finding in [*view["conflicts"], *view["findings"]]:
            _text_values(finding)
        st.dataframe(view["approvals"], hide_index=True)
        terminal = view["status"] in {"approved", "rejected", "cancelled"}
        if st.button("验证草稿", disabled=disabled or terminal):
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
