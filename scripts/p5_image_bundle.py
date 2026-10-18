"""Plan or stage explicitly scoped evidence images in a new private directory."""

import argparse
import json

from multiuser.image_bundle import plan_image_bundle, stage_image_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-index", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--collection", action="append", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-source-writers-stopped", action="store_true")
    parser.add_argument("--confirm-private-destination", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.destination:
                raise ValueError("New private destination required for staging")
            report = stage_image_bundle(
                args.source_index,
                args.source_root,
                args.destination,
                tuple(args.collection),
                writers_stopped=args.confirm_source_writers_stopped,
                private_destination_ready=args.confirm_private_destination,
            )
        else:
            report = plan_image_bundle(args.source_index, args.source_root, tuple(args.collection))
            report = {key: value for key, value in report.items() if key != "records"}
            report["status"] = "read_only_plan"
        print(json.dumps(report))
    except Exception as error:
        parser.exit(1, f"Image bundle refused/failed: {type(error).__name__}.\n")


if __name__ == "__main__":
    main()
