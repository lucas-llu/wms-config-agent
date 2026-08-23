"""Run a validated retrieval benchmark and persist a privacy-safe baseline report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.settings import load_settings
from ingestion.storage import BM25Indexer
from libs.embedding import EmbeddingFactory
from libs.evaluator import EvaluatorFactory
from libs.reranker import RerankerFactory
from libs.vector_store import VectorStoreFactory
from observability.evaluation import (
    BaselineComparator,
    BenchmarkDataset,
    RetrievalBenchmarkRunner,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--bm25-path", type=Path, default=Path("data/db/bm25"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/evaluation/latest_report.json")
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--enforce-thresholds", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    settings = load_settings(args.settings)
    dataset_path = args.dataset or settings.evaluation.golden_test_set
    dataset = BenchmarkDataset.load(dataset_path)
    embedding = EmbeddingFactory.create(settings)
    vector_store = VectorStoreFactory.create(settings)
    bm25_indexer = BM25Indexer(args.bm25_path)
    if vector_store.count() == 0 or bm25_indexer.count() == 0:
        raise SystemExit("No retrieval index found; run scripts/ingest.py first")
    hybrid_search = HybridSearch(
        settings,
        QueryProcessor(),
        DenseRetriever(embedding, vector_store),
        SparseRetriever(bm25_indexer, vector_store),
        ReciprocalRankFusion(settings.retrieval.rrf_k),
    )
    report = RetrievalBenchmarkRunner(
        hybrid_search,
        SafeReranker(RerankerFactory.create(settings)),
        top_k=args.top_k,
        evaluator=EvaluatorFactory.create(settings),
    ).run(dataset)
    payload = report.to_dict()
    comparison = None
    if args.baseline:
        comparison = BaselineComparator().compare(
            BaselineComparator.load(args.baseline), report
        )
        payload["comparison"] = comparison.to_dict()
    payload["run_metadata"] = {
        "git_revision": _git_revision(),
        "embedding_provider": settings.embedding.provider,
        "embedding_model": settings.embedding.model,
        "vector_store": settings.vector_store.backend,
        "sparse_backend": settings.retrieval.sparse_backend,
        "fusion_algorithm": settings.retrieval.fusion_algorithm,
        "reranker": settings.rerank.backend,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "dataset": report.dataset_name,
        "fingerprint": report.dataset_fingerprint,
        "case_count": report.case_count,
        "metrics": report.metrics,
        "threshold_results": report.threshold_results,
        "passed": report.passed,
        "failed_cases": [case.case_id for case in report.cases if not case.passed],
        "comparison": comparison.to_dict() if comparison else None,
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.enforce_thresholds and not report.passed:
        raise SystemExit(1)
    if args.fail_on_regression and (comparison is None or not comparison.passed):
        raise SystemExit(1)


def _git_revision() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


if __name__ == "__main__":
    main()
