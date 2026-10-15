"""Explicit P5 schema preflight/installation; never activates a release."""

import argparse
import json
import os
from pathlib import Path

import psycopg

from multiuser.schema_install import inspect_schema, install_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-db", required=True)
    parser.add_argument("--migrations", default="migrations")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        with psycopg.connect(os.environ["WMS_SCHEMA_ADMIN_DSN"], autocommit=True) as connection:
            directory = Path(args.migrations)
            if args.apply:
                result = install_schema(
                    connection,
                    directory,
                    args.expected_db,
                    os.environ["WMS_P1_RUNTIME_PASSWORD"],
                    os.environ["WMS_P3_CONTROL_PASSWORD"],
                )
            else:
                result = inspect_schema(connection, directory, args.expected_db)
        print(json.dumps(result))
    except Exception as exc:
        parser.exit(1, f"Schema installation refused/failed: {type(exc).__name__}.\n")


if __name__ == "__main__":
    main()
