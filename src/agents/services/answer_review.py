"""Opt-in, bounded answer review; never enabled by the default answering strategy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents.contracts import Evidence
from agents.language import localized, response_language
from agents.llm_json import StructuredLLMError
from agents.nodes.grounded_answer import GroundedAnswer, answer_question
from agents.services.evidence_decision import EvidenceDecisionReport, EvidenceDecisionService
from core.evidence_text import clean_evidence_text
from libs.llm import BaseLLM

EvidenceRetriever = Callable[[str, dict[str, Any], tuple[str, ...]], tuple[Evidence, ...]]


@dataclass(frozen=True, slots=True)
class AnswerReviewResult:
    baseline: GroundedAnswer
    final: GroundedAnswer
    outcome: str
    reports: tuple[EvidenceDecisionReport, ...]
    revisions: int
    retrievals: int
    tokens_used: int
    error_code: str | None = None
    evidence: tuple[Evidence, ...] = ()
    retries: int = 0


def review_answer(
    llm: BaseLLM,
    judge: EvidenceDecisionService,
    question: str,
    evidence: tuple[Evidence, ...],
    *,
    baseline: GroundedAnswer,
    confirmed_context: dict[str, Any] | None = None,
    language: str = "",
    retrieve: EvidenceRetriever | None = None,
    max_revisions: int = 1,
) -> AnswerReviewResult:
    """Reuse one baseline, review it, optionally retrieve/revise once, then re-review.

    This function runs only when explicitly selected. The caller must supply a
    Workspace-scoped read-only retriever. A positive judge result never grants action approval.
    """
    if (
        isinstance(max_revisions, bool)
        or not isinstance(max_revisions, int)
        or max_revisions not in (0, 1)
    ):
        raise ValueError("The experiment permits zero or one revision")
    language = language or response_language(question)
    context = dict(confirmed_context or {})
    current = baseline
    sources = evidence
    reports: list[EvidenceDecisionReport] = []
    tokens = baseline.tokens_used
    retries = baseline.retries
    revisions = retrievals = 0

    def finish(outcome: str, *, error_code: str | None = None) -> AnswerReviewResult:
        return AnswerReviewResult(
            baseline,
            current,
            outcome,
            tuple(reports),
            revisions,
            retrievals,
            tokens,
            error_code,
            sources,
            retries,
        )

    def render_verified(report: EvidenceDecisionReport) -> None:
        """Only audited claims and program-authored guidance reach the trial answer."""
        nonlocal current
        numbers = {item.evidence_id: index for index, item in enumerate(sources[:10], 1)}
        heading = localized(language, "结论", "Conclusion")
        if current.status == "insufficient_evidence":
            heading = localized(
                language,
                "结论：现有证据不足以确定所需修改。",
                "Conclusion: the evidence does not establish the required change.",
            )
        lines = [heading]
        for claim in current.claims:
            citations = ",".join(str(numbers[identifier]) for identifier in claim.evidence_ids)
            lines.append(f"- {claim.text} [{citations}]")
        if current.status == "insufficient_evidence":
            lines.extend(
                [
                    "",
                    localized(
                        language,
                        "需要补充能够明确支持该问题结论的适用文档或业务信息。",
                        "Provide applicable documents or business context that explicitly support "
                        "a conclusion for this question.",
                    ),
                ]
            )
        lines.extend(
            [
                "",
                localized(
                    language,
                    "以上基于文档，尚未核验你的实际环境。",
                    "Based on documents; your actual environment is not verified.",
                ),
            ]
        )
        current = GroundedAnswer(
            "\n".join(lines),
            tokens,
            current.retries,
            current.cited_source_ids,
            current.status,
            current.supporting_quotes,
            report,
            current.claims,
        )

    def abstain(error_code: str) -> AnswerReviewResult:
        nonlocal current
        current = GroundedAnswer(
            localized(
                language,
                "现有证据未能通过结论核验，暂不能给出可靠的配置结论。"
                "请补充适用范围明确、包含前提和例外的文档，或由业务人员复核。",
                "The conclusion could not be verified against the available evidence. "
                "Provide documentation with explicit scope, prerequisites and exceptions, "
                "or request a domain review.",
            ),
            tokens_used=tokens,
            retries=0,
            status="insufficient_evidence",
        )
        return finish("abstained", error_code=error_code)

    for attempt in range(max_revisions + 1):
        if attempt == 0 and current.decision_report is not None:
            report = current.decision_report  # Its tokens are already included in the baseline.
        else:
            report = judge.evaluate(question, current.claims, sources, confirmed_context=context)
            tokens += report.tokens_used
        reports.append(report)
        registry = {item.evidence_id: item for item in sources}
        recoverable_context_gap = (
            report.error_code == "quote_not_in_evidence"
            and any(item.full_excerpt for item in sources)
            and bool(current.claims)
            and all(
                len(clean_evidence_text(claim.quote)) >= 8
                and any(
                    identifier in registry
                    and clean_evidence_text(claim.quote)
                    in clean_evidence_text(registry[identifier].excerpt)
                    for identifier in claim.evidence_ids
                )
                for claim in current.claims
            )
        )
        if report.status == "unresolved" and not recoverable_context_gap:
            return abstain(report.error_code or "review_unresolved")
        verified = (
            bool(current.claims)
            and report.status == "completed"
            and len(report.decisions) == len(current.claims)
            and all(d.suggested_action == "candidate_supported" for d in report.decisions)
        )
        if current.status == "answered" and verified:
            render_verified(report)
            return finish("accepted" if not revisions else "revised")
        if (
            current.status == "insufficient_evidence"
            and retrieve is None
            and (not current.claims or verified)
        ):
            if verified:
                render_verified(report)
            return finish("insufficient_evidence")
        if attempt == max_revisions:
            return abstain("review_not_passed")

        reasons = tuple(sorted({d.reason_code for d in report.decisions}))
        should_retrieve = current.status == "insufficient_evidence" or any(
            d.suggested_action in {"retrieve_more", "clarify_scope"} for d in report.decisions
        )
        if retrieve is not None and should_retrieve:
            retrievals += 1
            try:
                additional = retrieve(question, context, reasons)
                if not isinstance(additional, tuple) or any(
                    not isinstance(item, Evidence) for item in additional
                ):
                    return abstain("retrieval_invalid")
                registry = {item.evidence_id: item for item in sources}
                for item in additional:
                    old = registry.get(item.evidence_id)
                    if old is not None and old != item:
                        return abstain("evidence_changed")
                # Keep the original sources as counterevidence while admitting one new batch.
                merged = {item.evidence_id: item for item in (*additional, *sources)}
                sources = tuple(merged.values())[:10]
            except Exception:
                return abstain("retrieval_unavailable")
        feedback = {
            "previous_claims": [
                {"claim_id": claim.claim_id, "text": claim.text} for claim in current.claims
            ],
            "findings": [
                {
                    "claim_id": d.claim_id,
                    "relation": d.relation,
                    "scope": d.scope,
                    "conditions": d.conditions,
                    "reason_code": d.reason_code,
                }
                for d in report.decisions
            ],
            "previous_status": current.status,
        }
        revisions += 1
        try:
            current = answer_question(
                llm,
                question,
                sources,
                language=language,
                confirmed_context=context,
                review_feedback=feedback,
                use_full_excerpt=True,
                max_sources=10,
            )
            tokens += current.tokens_used
            retries += current.retries
        except StructuredLLMError as error:
            tokens += error.tokens_used
            retries += error.retries
            return abstain("revision_unavailable")
    raise AssertionError("Unreachable review state")
