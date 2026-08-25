"""Newline-delimited JSON-RPC stdio transport for the WMS MCP server."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from mcp_server.app import create_protocol_handler
from mcp_server.protocol_handler import ProtocolHandler
from observability.logger import get_logger

LOGGER = get_logger(__name__)


def serve_stdio(
    handler: ProtocolHandler,
    input_stream: Iterable[str],
    output_stream: TextIO,
) -> None:
    """Serve one JSON-RPC message per input line until EOF."""
    for line in input_stream:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = handler.parse_error()
        else:
            response = handler.handle(message)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        output_stream.write("\n")
        output_stream.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--bm25-path", type=Path, default=Path("data/db/bm25"))
    parser.add_argument("--chunks", type=Path, default=Path("data/corpus/processed/chunks"))
    parser.add_argument("--image-root", action="append", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        handler = create_protocol_handler(
            settings_path=args.settings,
            bm25_path=args.bm25_path,
            chunks_path=args.chunks,
            image_roots=args.image_root,
        )
    except Exception as exc:
        LOGGER.error("MCP server failed to start: %s", exc)
        raise SystemExit(1) from exc
    serve_stdio(handler, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
