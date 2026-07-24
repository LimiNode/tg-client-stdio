from __future__ import annotations

import argparse
import sys

from .protocol import DEFAULT_MAX_JSONL_RECORD_BYTES, MIN_MAX_JSONL_RECORD_BYTES
from .server import JsonlWorkerServer, MockTelegramBackend, ServerConfig


def jsonl_byte_limit(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < MIN_MAX_JSONL_RECORD_BYTES:
        raise argparse.ArgumentTypeError(
            f"must be at least {MIN_MAX_JSONL_RECORD_BYTES}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Telegram user-client JSONL stdio worker",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run deterministic mock backend instead of a real Telegram client.",
    )
    parser.add_argument(
        "--max-jsonl-bytes",
        type=jsonl_byte_limit,
        default=DEFAULT_MAX_JSONL_RECORD_BYTES,
        help="Maximum inbound/outbound JSONL record size.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.mock:
        print(
            "Only --mock backend is implemented in this scaffold.",
            file=sys.stderr,
        )
        return 2

    server = JsonlWorkerServer(
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
        error_stream=sys.stderr,
        backend=MockTelegramBackend(),
        config=ServerConfig(max_jsonl_bytes=args.max_jsonl_bytes),
    )
    return server.run()
