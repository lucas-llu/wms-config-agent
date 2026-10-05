"""Evaluate synthetic evidence decisions; default is a no-network preflight, not accuracy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

from agents.contracts import Evidence
from agents.services.evidence_decision import EvidenceClaim, EvidenceDecisionService
from core.settings import load_settings
from libs.llm import BudgetedLLM, ChatResponse, LLMFactory


class NoNetworkLLM:
    def chat(self, messages) -> ChatResponse:
        del messages
        raise RuntimeError("Semantic model intentionally absent in preflight mode")


def make_case(case: dict) -> tuple[tuple[EvidenceClaim, ...], tuple[Evidence, ...]]:
    evidence = Evidence(
        evidence_id=f"e:{case['id']}",
        chunk_id=f"c:{case['id']}",
        source="synthetic-evidence-decision.pdf",
        excerpt=case["evidence"],
        score=1.0,
        product_version=case.get("evidence_version", "v1"),
    )
    return (
        (
            EvidenceClaim(
                f"claim:{case['id']}", case["claim"], (evidence.evidence_id,), case["quote"]
            ),
        ),
        (evidence,),
    )


def evaluate_cases(service: EvidenceDecisionService, cases: list[dict], *, live: bool) -> dict:
    rows = []
    for case in cases:
        claims, evidence = make_case(case)
        report = service.evaluate(
            case["question"], claims, evidence, confirmed_context={"product_version": "v1"}
        )
        decision = report.decisions[0]
        candidate = decision.suggested_action == "candidate_supported"
        rows.append(
            {
                "case_id": case["id"],
                "expected_candidate": case["expected_candidate"],
                "candidate": candidate,
                "quote_presence_only_candidate": report.error_code
                not in {
                    "invalid_input",
                    "invalid_evidence_reference",
                    "quote_not_in_evidence",
                    "input_limit_exceeded",
                },
                "labels_match": (
                    decision.relation == case["expected_relation"]
                    and decision.scope == case["expected_scope"]
                    and decision.conditions == case["expected_conditions"]
                ),
                "preflight_expected": case.get("expected_preflight_error"),
                "preflight_guard_correct": (
                    report.error_code == case.get("expected_preflight_error")
                    if case.get("expected_preflight_error")
                    else report.error_code == "judge_unavailable_or_invalid"
                    if not live
                    else True
                ),
                "report": report.to_dict(),
            }
        )
    positives = [row for row in rows if row["expected_candidate"]]
    negatives = [row for row in rows if not row["expected_candidate"]]
    false_pass = sum(row["candidate"] for row in negatives)
    false_block = sum(not row["candidate"] for row in positives)
    completed = sum(row["report"]["status"] == "completed" for row in rows)
    return {
        "mode": "live_synthetic" if live else "offline_preflight",
        "measures_semantic_quality": live and completed > 0,
        "dataset_kind": "author_labelled_synthetic_not_real_corpus",
        "sample_count": len(rows),
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "completed_count": completed,
        "false_pass_count": false_pass if live else None,
        "false_pass_rate": false_pass / len(negatives) if live and negatives else None,
        "false_block_count": false_block if live else None,
        "false_block_rate": false_block / len(positives) if live and positives else None,
        "label_accuracy": (
            sum(row["labels_match"] for row in rows) / len(rows) if live and completed else None
        ),
        "quote_presence_only_baseline": {
            "not_a_full_agent_baseline": True,
            "false_pass_count": sum(row["quote_presence_only_candidate"] for row in negatives),
            "false_block_count": sum(not row["quote_presence_only_candidate"] for row in positives),
        },
        "preflight_guards_passed": all(row["preflight_guard_correct"] for row in rows),
        "tokens_used": sum(row["report"]["tokens_used"] for row in rows),
        "token_accounting": "provider_total_when_available_else_character_estimate",
        "latency_seconds": sum(row["report"]["latency_seconds"] for row in rows),
        "network_enabled": live,
        "production_ready": False,
        "promotion_reason": "Requires model comparisons, calibration and real-corpus review",
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("tests/fixtures/evidence_decision_cases.json")
    )
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/evaluation/evidence-decision-preflight.json")
    )
    parser.add_argument(
        "--live", action="store_true", help="Send synthetic cases to configured API"
    )
    parser.add_argument("--model", help="Optional decision model override, using the same endpoint")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    args = parser.parse_args()
    if min(args.limit, args.max_calls, args.max_output_tokens) < 1:
        parser.error("limits must be positive")
    raw = args.dataset.read_bytes()
    cases = json.loads(raw)["cases"][: args.limit]
    if not cases:
        parser.error("dataset must contain cases")
    llm = NoNetworkLLM()
    if args.live:
        settings = load_settings(args.settings).llm
        if settings.provider == "disabled":
            parser.error("live evaluation requires a configured provider")
        if settings.api_key_env and not os.getenv(settings.api_key_env):
            parser.error(f"Set {settings.api_key_env} in the process environment; never in chat")
        provider = replace(
            settings,
            model=args.model or settings.model,
            max_tokens=args.max_output_tokens,
            max_retries=0,
        )
        llm = BudgetedLLM(LLMFactory.create(provider), max_calls=args.max_calls)
    service = EvidenceDecisionService(llm)
    report = evaluate_cases(service, cases, live=args.live)
    report["dataset_sha256"] = hashlib.sha256(raw).hexdigest()
    report["logical_model_calls"] = llm.calls_made if args.live else 0
    report["model"] = getattr(llm, "model", None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "cases"}
    print(json.dumps(summary, ensure_ascii=False))
    if not args.live:
        return 0 if report["preflight_guards_passed"] else 1
    return 0 if report["false_pass_count"] == 0 and report["false_block_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
