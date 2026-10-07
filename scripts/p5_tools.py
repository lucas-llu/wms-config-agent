"""Private offline P5 tools. Read-only/dry-run defaults; no automatic live cutover."""

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

import psycopg

from core.settings import load_settings
from libs.llm.openai_compatible_llm import OpenAICompatibleLLM
from multiuser.migration import import_snapshot, load_snapshot, snapshot
from multiuser.pressure import ProbeBudget, probe_supplier
from multiuser.release import prepare_phase, preserve_post_cutover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("snapshot")
    backup.add_argument("--source", required=True)
    backup.add_argument("--destination", required=True)
    backup.add_argument("--checkpoints")
    backup.add_argument("--confirm-source-stopped", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    migration = sub.add_parser("import")
    migration.add_argument("--bundle", required=True)
    migration.add_argument("--ownership-plan", required=True)
    migration.add_argument("--operator", required=True)
    migration.add_argument("--apply", action="store_true")
    phase = sub.add_parser("phase")
    phase.add_argument(
        "--target", choices=("draining", "frozen", "rollback_readonly"), required=True
    )
    phase.add_argument("--revision", type=int, required=True)
    phase.add_argument("--operator", required=True)
    phase.add_argument("--reason", required=True)
    phase.add_argument("--apply", action="store_true")
    preserve = sub.add_parser("preserve")
    preserve.add_argument("--destination", required=True)
    preserve.add_argument("--confirm-workers-stopped", action="store_true")
    supplier = sub.add_parser("supplier")
    supplier.add_argument("--approval", required=True)
    supplier.add_argument("--settings", default="config/settings.yaml")
    supplier.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = snapshot(
                args.source,
                args.destination,
                writers_stopped=args.confirm_source_stopped,
                checkpoints=args.checkpoints,
            )
        elif args.command == "verify":
            result = load_snapshot(args.bundle)[0]
        elif args.command == "supplier":
            if os.getenv("WMS_P5_SUPPLIER_LIVE") != "1":
                raise PermissionError("Real supplier calls require explicit opt-in")
            budget = ProbeBudget(**json.loads(Path(args.approval).read_text(encoding="utf-8")))
            settings = replace(
                load_settings(args.settings).llm, max_retries=0, max_tokens=budget.max_output_tokens
            )
            if settings.provider != "openai_compatible":
                raise ValueError("Explicit compatible supplier configuration required")
            report = Path(args.report)
            if report.exists():
                raise ValueError("New private report path required")
            report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Consume this report slot before any request. Crash/timeout is not a free retry.
            with report.open("x", encoding="utf-8") as stream:
                json.dump({"status": "incomplete", "calls_reserved": budget.max_calls}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            result = probe_supplier(OpenAICompatibleLLM(settings), budget)
            report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        elif args.command == "preserve":
            result = preserve_post_cutover(
                os.environ["WMS_MIGRATION_DSN"],
                args.destination,
                workers_stopped=args.confirm_workers_stopped,
            )
        else:
            with psycopg.connect(os.environ["WMS_MIGRATION_DSN"]) as connection:
                if args.command == "import":
                    result = import_snapshot(
                        connection,
                        args.bundle,
                        json.loads(Path(args.ownership_plan).read_text(encoding="utf-8")),
                        args.operator,
                        apply=args.apply,
                    )
                else:
                    result = prepare_phase(
                        connection,
                        args.target,
                        args.revision,
                        args.operator,
                        args.reason,
                        apply=args.apply,
                    )
        print(json.dumps(result, default=str))
    except Exception as exc:
        parser.exit(
            1,
            "P5 operation refused/failed: "
            + type(exc).__name__
            + ". Inspect protected operator inputs; no raw credentials logged.\n",
        )


if __name__ == "__main__":
    main()
