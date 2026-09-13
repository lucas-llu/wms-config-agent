"""Run deterministic product release gates; failures return a nonzero exit code."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from observability.evaluation.product_release import run_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/evaluation/product-release.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = run_release(root)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False
    )
    report["git_revision"] = revision.stdout.strip() if revision.returncode == 0 else None
    report["dirty_worktree"] = bool(dirty.stdout) if dirty.returncode == 0 else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
