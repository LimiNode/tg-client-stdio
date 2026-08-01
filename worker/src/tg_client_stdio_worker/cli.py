from __future__ import annotations

import argparse
import sys

from .backend import BackendError
from .protocol import DEFAULT_MAX_JSONL_RECORD_BYTES, MIN_MAX_JSONL_RECORD_BYTES
from .backend import MockTelegramBackend
from .server import JsonlWorkerServer, ServerConfig
from .telethon_backend import TelethonBackend, TelethonBackendConfig


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
        action="store_const",
        const="mock",
        dest="backend",
        help="Run deterministic mock backend instead of a real Telegram client.",
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "telethon"],
        default=None,
        help="Backend implementation to run.",
    )
    parser.add_argument("--api-id", type=int, help="Telegram API ID for Telethon backend.")
    parser.add_argument("--api-hash", help="Telegram API hash for Telethon backend.")
    parser.add_argument("--session", help="Telethon session path/name.")
    parser.add_argument(
        "--max-jsonl-bytes",
        type=jsonl_byte_limit,
        default=DEFAULT_MAX_JSONL_RECORD_BYTES,
        help="Maximum inbound/outbound JSONL record size.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backend = build_backend(args)
    except (BackendError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    server = JsonlWorkerServer(
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
        error_stream=sys.stderr,
        backend=backend,
        config=ServerConfig(max_jsonl_bytes=args.max_jsonl_bytes),
        backend_name=args.backend,
    )
    return server.run()


def build_backend(args: argparse.Namespace) -> object:
    if args.backend is None:
        raise ValueError("one of --mock or --backend must be specified")
    if args.backend == "mock":
        return MockTelegramBackend()

    missing = [
        name
        for name, value in {
            "--api-id": args.api_id,
            "--api-hash": args.api_hash,
            "--session": args.session,
        }.items()
        if value in (None, "")
    ]
    if missing:
        raise ValueError(f"missing required Telethon options: {', '.join(missing)}")

    return TelethonBackend(
        TelethonBackendConfig(
            api_id=args.api_id,
            api_hash=args.api_hash,
            session=args.session,
        )
    )
