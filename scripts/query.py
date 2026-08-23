"""Query the local WMS corpus through hybrid retrieval and cited evidence output."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.response import ResponseBuilder
from core.settings import load_settings
from ingestion.storage import BM25Indexer
from libs.embedding import EmbeddingFactory
from libs.reranker import RerankerFactory
from libs.vector_store import VectorStoreFactory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--bm25-path", type=Path, default=Path("data/db/bm25"))
    parser.add_argument("--collection")
    parser.add_argument("--domain")
    parser.add_argument("--document-type")
    parser.add_argument("--process-code")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-rerank", action="store_true")
    return parser.parse_args()


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "collection": args.collection,
            "domain": args.domain,
            "document_type": args.document_type,
            "process_code": args.process_code,
        }.items()
        if value is not None
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    settings = load_settings(args.settings)
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
    outcome = hybrid_search.search_with_details(
        args.query,
        top_k=args.top_k,
        filters=_filters(args),
    )
    rerank_failure = None
    if not args.no_rerank:
        reranked = SafeReranker(RerankerFactory.create(settings)).rerank(
            outcome.processed_query.retrieval_query,
            list(outcome.results),
        )
        outcome = replace(outcome, results=reranked.results)
        rerank_failure = reranked.failure

    response = ResponseBuilder().build(outcome)
    payload = response.to_dict()
    if rerank_failure:
        payload["diagnostics"]["rerank_failure"] = rerank_failure
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(response.message)
    if response.markdown != response.message:
        print()
        print(response.markdown)
    if args.verbose:
        print()
        print("Diagnostics:")
        print(json.dumps(payload["diagnostics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
