"""Build a private JSONL manifest for an authorized local PDF corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ingestion import CorpusManifestBuilder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("../14_system_training"),
        help="Authorized PDF corpus directory (default: ../14_system_training)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/corpus/manifest.jsonl"),
        help="Private manifest output path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    builder = CorpusManifestBuilder()
    entries = builder.scan(args.source)
    output_path = builder.write(entries, args.output)
    result = {
        "manifest_path": output_path.as_posix(),
        "source_root": args.source.resolve().as_posix(),
        **builder.summarize(entries).to_dict(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
