"""Verify LangGraph persistence, interrupt/resume, and asynchronous streaming."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from agents.runtime import run_runtime_probe


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="Optional SQLite checkpoint path. A temporary file is used by default.",
    )
    return parser.parse_args()


async def _run(checkpoint: Path | None) -> dict[str, object]:
    if checkpoint is not None:
        return await run_runtime_probe(checkpoint)
    with tempfile.TemporaryDirectory(prefix="wms-agent-runtime-") as temporary_directory:
        return await run_runtime_probe(Path(temporary_directory) / "probe.db")


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_run(args.checkpoint))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["completed"] and report["final_values"]["result"] == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
