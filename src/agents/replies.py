"""Truthful, deterministic replies for configuration workflow outcomes."""

from __future__ import annotations

from typing import Any

from agents.language import localized, response_language


def workflow_reply(state: dict[str, Any]) -> tuple[str, str]:
    language = state.get("response_language") or response_language(state.get("user_goal", ""))
    reason = state.get("pause_reason", "")
    if reason in {"requirements_missing", "intent_clarification"}:
        questions = [str(q.get("text", "")).strip() for q in state.get("open_questions", [])]
        if any(questions):
            intro = localized(
                language, "为了继续，请补充以下信息：", "To continue, please confirm:"
            )
            return intro + "\n" + "\n".join(f"- {q}" for q in questions if q), "clarification"
    tasks = state.get("configuration_tasks", [])
    if reason == "validation_blocked":
        unsupported = sum(
            item.get("evidence_status") != "supported"
            for item in state.get("task_evidence_bindings", [])
        )
        blocking = sum(
            item.get("severity") == "blocking" for item in state.get("validation_findings", [])
        )
        return localized(
            language,
            f"已根据当前需求形成 {len(tasks)} 项配置任务，但验证尚未通过。"
            f"其中 {unsupported} 项任务的证据未完全支持，"
            f"共有 {blocking} 项阻断性验证问题及 {len(state.get('conflicts', []))} 项冲突。\n\n"
            "请在工作区查看具体证据缺口与验证项，再补充或修正需求、版本或范围。"
            "补充后会重新规划并验证；不会跳过验证或自动批准。",
            f"Prepared {len(tasks)} configuration tasks, but validation is blocked. "
            f"{unsupported} tasks lack complete evidence support and "
            f"{blocking} blocking findings and "
            f"{len(state.get('conflicts', []))} conflicts remain.\n\n"
            "Check evidence gaps and findings in Workspace, then clarify or correct the "
            "requirements, version or scope. Your reply will trigger replanning and validation; "
            "it cannot bypass validation or grant approval.",
        ), "validation_result"
    if state.get("status") == "review_required":
        lines = [
            localized(
                language,
                f"需求已补齐，已生成 {len(tasks)} 项配置任务，当前验证未发现阻断项。",
                f"Requirements are complete. Prepared {len(tasks)} configuration tasks with "
                "no blocking validation findings.",
            )
        ]
        lines.extend(f"- {item.get('title', '')}" for item in tasks)
        lines.append(
            localized(
                language,
                "\n请在工作区检查配置步骤、引用证据和风险，然后明确提交审查。"
                "方案尚未批准，也没有写入 WMS。",
                "\nReview the steps, citations and risks in Workspace, then submit an explicit "
                "review decision. The draft is not approved and nothing has been written to WMS.",
            )
        )
        return "\n".join(lines), "configuration_result"
    if state.get("status") == "retrieving":
        return localized(
            language,
            f"需求已补齐，已规划 {len(tasks)} 项配置任务。当前尚待检索和验证证据，"
            "不能将此草稿当作可执行或已批准的方案。请在工作区查看规划并检查知识库服务。",
            f"Requirements are complete and {len(tasks)} tasks are planned. Evidence retrieval "
            "and validation are still pending. This is not an executable or approved solution. "
            "Check the plan in Workspace and the knowledge service.",
        ), "configuration_result"
    # Never replay a previous clarification or claim success after a budget/provider failure.
    return localized(
        language,
        "本轮处理已暂停，尚未形成通过验证的新结果。请检查服务、预算和工作区范围后重试；"
        "已有草稿不代表获准执行。",
        "This turn paused without a newly validated result. Check service availability, "
        "turn budgets and workspace scope before retrying. Existing drafts are not approved.",
    ), "workflow_paused"
