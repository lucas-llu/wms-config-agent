"""Paired synthetic final-answer experiment, with a shared initial draft and context controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from agents.contracts import Evidence
from agents.llm_json import StructuredLLMError
from agents.nodes.grounded_answer import GroundedAnswer, answer_question
from agents.services.answer_review import review_answer
from agents.services.evidence_decision import EvidenceClaim, EvidenceDecisionService
from core.evidence_text import clean_evidence_text
from core.settings import load_settings
from libs.llm import BudgetedLLM, LLMFactory


class CaseMeter:
    """Distinguish transport failures from decisions without recording request bodies."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.events = []

    def chat(self, messages, trace=None):
        started = time.perf_counter()
        event = {"stage": "judge" if messages[0]["role"] == "system" else "generation"}
        try:
            response = self.delegate.chat(messages, trace=trace)
            event["normalized_response_received"] = True
            event["tokens"] = response.metadata.get("usage", {}).get("total_tokens")
            return response
        except Exception as error:
            event["normalized_response_received"] = False
            event["error_type"] = type(error).__name__
            event["http_status"] = getattr(error, "status_code", None)
            raise
        finally:
            event["latency_seconds"] = time.perf_counter() - started
            self.events.append(event)


def make_sources(case: dict, field: str = "sources") -> tuple[Evidence, ...]:
    return tuple(
        Evidence(
            evidence_id=f"e:{case['id']}:{item['id']}",
            chunk_id=f"c:{case['id']}:{item['id']}",
            source="synthetic-holdout.pdf",
            excerpt=item["text"],
            full_excerpt=item.get("full_text"),
            score=1.0,
            product_version=item.get("version", "v1"),
        )
        for item in case.get(field, [])
    )


def answer_record(answer: GroundedAnswer, case: dict) -> dict:
    text = "\n".join(claim.text for claim in answer.claims).casefold()
    status_match = answer.status == case["expected_status"]
    missing = [term for term in case.get("required_terms", []) if term.casefold() not in text]
    return {
        "status": answer.status,
        "text": answer.text,
        "claims": [
            {"text": c.text, "evidence_ids": c.evidence_ids, "quote": c.quote}
            for c in answer.claims
        ],
        "tokens_used": answer.tokens_used,
        "status_matches": status_match,
        "missing_terms": missing,
        "proxy_pass": status_match and not missing,
    }


def restore_baseline(case: dict, record: dict) -> GroundedAnswer:
    """Restore the same initial draft, checking citation identity against the pinned dataset."""
    sources = make_sources(case)
    registry = {item.evidence_id: (index, item) for index, item in enumerate(sources, 1)}
    claims = []
    quotes = []
    for index, value in enumerate(record["claims"], 1):
        ids = tuple(value["evidence_ids"])
        quote = clean_evidence_text(value["quote"])
        if (
            not ids
            or len(quote) < 8
            or any(identifier not in registry for identifier in ids)
            or not any(quote in clean_evidence_text(registry[i][1].excerpt) for i in ids)
        ):
            raise ValueError("Cached baseline has invalid citation identity")
        claims.append(EvidenceClaim(f"claim:{index}", value["text"], ids, quote))
        quotes.extend((registry[identifier][0], quote) for identifier in ids)
    if record["status"] not in {"answered", "insufficient_evidence"}:
        raise ValueError("Invalid cached answer status")
    return GroundedAnswer(
        record["text"],
        record["tokens_used"],
        0,
        tuple(sorted({i for i, _ in quotes})),
        record["status"],
        tuple(dict.fromkeys(quotes)),
        claims=tuple(claims),
    )


def run_case(case: dict, llm: BudgetedLLM) -> dict:
    started = time.perf_counter()
    llm = CaseMeter(llm)
    sources = make_sources(case)
    additional = make_sources(case, "additional_sources")
    context = case.get("context", {"product_version": "v1"})
    judge = EvidenceDecisionService(llm, include_counter_evidence=True)
    row = {"case_id": case["id"], "expected_status": case["expected_status"]}
    try:
        baseline = (
            restore_baseline(case, case["cached"]["baseline"])
            if "cached" in case
            else answer_question(llm, case["question"], sources, confirmed_context=context)
        )
        row["baseline"] = answer_record(baseline, case)
        row["baseline_reused"] = "cached" in case

        def retrieve(question, confirmed, reasons):
            del question, confirmed, reasons
            return additional

        trial = review_answer(
            llm,
            judge,
            case["question"],
            sources,
            baseline=baseline,
            confirmed_context=context,
            retrieve=retrieve if additional else None,
        )
        row["reviewed"] = answer_record(trial.final, case)
        row["review"] = {
            "outcome": trial.outcome,
            "revisions": trial.revisions,
            "retrievals": trial.retrievals,
            "tokens_used": trial.tokens_used,
            "error_code": trial.error_code,
            "reports": [report.to_dict() for report in trial.reports],
        }
        if case.get("context_control"):
            if "cached" in case and "context_only_control" in case["cached"]:
                row["context_only_control"] = case["cached"]["context_only_control"]
            else:
                registry = {item.evidence_id: item for item in (*additional, *sources)}
                control = answer_question(
                    llm,
                    case["question"],
                    tuple(registry.values())[:5],
                    confirmed_context=context,
                    use_full_excerpt=True,
                )
                row["context_only_control"] = answer_record(control, case)
    except StructuredLLMError as error:
        row["error_code"] = "generation_unavailable"
        row["error_tokens"] = error.tokens_used
    row["latency_seconds"] = time.perf_counter() - started
    row["provider_events"] = llm.events
    print(
        json.dumps(
            {
                "case_id": case["id"],
                "baseline_proxy": row.get("baseline", {}).get("proxy_pass"),
                "reviewed_proxy": row.get("reviewed", {}).get("proxy_pass"),
                "review_outcome": row.get("review", {}).get("outcome"),
                "error": row.get("error_code"),
            }
        ),
        flush=True,
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("tests/fixtures/answer_review_holdout.json")
    )
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/evaluation/answer-review-holdout-1004.json")
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case-ids", nargs="+", help="Optional named diagnostic subset")
    parser.add_argument(
        "--baseline-cache", type=Path, help="Reuse pinned initial drafts and controls"
    )
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--max-calls", type=int, default=160)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if min(args.limit, args.max_calls, args.workers) < 1 or args.workers > 3:
        parser.error("positive limits and at most three workers required")
    raw = args.dataset.read_bytes()
    cases = json.loads(raw)["cases"][: args.limit]
    if args.case_ids:
        wanted = set(args.case_ids)
        if wanted - {case["id"] for case in cases}:
            parser.error("unknown diagnostic case ID")
        cases = [case for case in cases if case["id"] in wanted]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        parser.error("non-empty dataset with unique IDs required")
    if args.baseline_cache:
        cached = json.loads(args.baseline_cache.read_text("utf-8"))
        if cached["dataset_sha256"] != hashlib.sha256(raw).hexdigest():
            parser.error("baseline cache must use the exact same dataset")
        mapping = {row["case_id"]: row for row in cached["cases"]}
        for case in cases:
            if case["id"] not in mapping or "baseline" not in mapping[case["id"]]:
                parser.error("baseline cache is missing a complete case")
            case["cached"] = mapping[case["id"]]
            restore_baseline(case, mapping[case["id"]]["baseline"])
    if not args.live:
        print(
            json.dumps(
                {
                    "cases": len(cases),
                    "network_enabled": False,
                    "measures_accuracy": False,
                    "production_ready": False,
                }
            )
        )
        return 0
    settings = load_settings(args.settings).llm
    if settings.provider == "disabled" or (
        settings.api_key_env and not os.getenv(settings.api_key_env)
    ):
        parser.error("configured provider credentials required in process environment")
    llm = BudgetedLLM(
        LLMFactory.create(replace(settings, max_tokens=8192, max_retries=0)), args.max_calls
    )
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_case, case, llm) for case in cases]
        for future in as_completed(futures):
            rows.append(future.result())
    order = {case["id"]: index for index, case in enumerate(cases)}
    rows.sort(key=lambda row: order[row["case_id"]])
    report = {
        "mode": "paired_final_answer_trial",
        "model": settings.model,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "sample_count": len(rows),
        "logical_model_calls": llm.calls_made,
        "baseline_cache_used": args.baseline_cache is not None,
        "production_ready": False,
        "scoring": "status_and_keywords_proxy; factual correctness requires source review",
        "baseline_proxy_passes": sum(r.get("baseline", {}).get("proxy_pass", False) for r in rows),
        "reviewed_proxy_passes": sum(r.get("reviewed", {}).get("proxy_pass", False) for r in rows),
        "proxy_improvements": sum(
            not r.get("baseline", {}).get("proxy_pass", False)
            and r.get("reviewed", {}).get("proxy_pass", False)
            for r in rows
        ),
        "proxy_regressions": sum(
            r.get("baseline", {}).get("proxy_pass", False)
            and not r.get("reviewed", {}).get("proxy_pass", False)
            for r in rows
        ),
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)
    return 1 if any("error_code" in row for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
