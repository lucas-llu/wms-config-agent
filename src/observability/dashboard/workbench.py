"""User-facing workbench; mutations happen only on explicit form submission."""

from __future__ import annotations

import streamlit as st

from agents.repositories.feedback_repository import FEEDBACK_KINDS, REGENERATION_REASONS
from observability.dashboard.services.workbench_service import WorkbenchService


def _submit(operation, *, refresh=True):
    try:
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
        st.session_state["workbench_notice"] = "操作完成，请选择最新版本查看结果。"
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


def render_workbench(service: WorkbenchService) -> None:
    st.title("Agent Sessions")
    workspace = service.repository.workspace
    st.caption(f"配置工作台 · {workspace.name} · {workspace.workspace_id}")
    st.caption("Workspace 由主机配置选择；此页面不会执行真实 WMS 配置写入。")
    if notice := st.session_state.pop("workbench_notice", None):
        st.success(notice)
    if not service.enabled:
        st.info("Agent 未启用：仅可查看已保存会话。")
    with st.form("new_session"):
        goal = st.text_area("配置目标", placeholder="描述模块、站点、版本和期望结果")
        if st.form_submit_button("新建会话", disabled=not service.enabled):
            _submit(lambda: service.start(goal))
    rows = service.list_rows()
    if not rows:
        st.info("暂无会话。启用 Agent 后，输入配置目标开始。")
        return
    names = {row["Session"]: row["Goal"] for row in rows}
    session_key = f"workbench_session:{workspace.workspace_id}"
    target = st.session_state.pop("workbench_target", None)
    if target and target[0] in names:
        st.session_state[session_key] = target[0]
        st.session_state[f"revision:{workspace.workspace_id}:{target[0]}"] = target[1]
    session_id = st.selectbox(
        "会话", list(names), format_func=lambda key: names[key], key=session_key
    )
    revisions = service.repository.list_revisions(session_id)
    revision = st.selectbox(
        "查看版本",
        [item.revision for item in reversed(revisions)],
        key=f"revision:{workspace.workspace_id}:{session_id}",
    )
    view = service.view(session_id, revision)
    st.subheader(f"版本 {revision} · {view['status']}")
    st.info(view["next_step"])
    historical = revision != view["current_revision"]
    if historical:
        st.warning("正在查看历史版本：对话、验证、审批与导出已禁用。")
    disabled = not service.enabled or historical
    chat, draft, evidence, review, feedback = st.tabs(
        ["对话", "配置草稿与依赖", "引用证据", "审查与导出", "反馈"]
    )
    with chat:
        for turn in view["turns"]:
            with st.chat_message(turn["role"]):
                st.text(turn["message"])
        for question in view["questions"]:
            st.warning(str(question.get("text", "待补充需求")))
        with st.form(f"continue:{session_id}:{revision}"):
            message = st.text_area("补充需求或回答问题")
            if st.form_submit_button("继续对话", disabled=disabled or view["status"] != "paused"):
                _submit(lambda: service.act("continue", session_id, revision, message=message))
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
