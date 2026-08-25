"""Run a diagnostic dense-vector query against the local WMS corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.settings import load_settings
from libs.embedding import EmbeddingFactory
from libs.vector_store import VectorStoreFactory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--domain")
    parser.add_argument("--document-type")
    parser.add_argument("--process-code")
    return parser.parse_args()


def _filters(args: argparse.Namespace) -> dict[str, Any] | None:
    values = {
        "domain": args.domain,
        "document_type": args.document_type,
        "process_code": args.process_code,
    }
    active = {key: value for key, value in values.items() if value}
    if len(active) <= 1:
        return active or None
    return {"$and": [{key: value} for key, value in active.items()]}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    settings = load_settings(args.settings)
    embedding = EmbeddingFactory.create(settings)
    store = VectorStoreFactory.create(settings)
    results = store.query(
        embedding.embed_query(args.query),
        top_k=args.top_k,
        filters=_filters(args),
    )
    output = {
        "query": args.query,
        "count": len(results),
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
