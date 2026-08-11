from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

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


def _env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


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
    parser.add_argument(
        "--api-id",
        type=int,
        default=_env_int("TG_CLIENT_STDIO_API_ID"),
        help="Telegram API ID; defaults to TG_CLIENT_STDIO_API_ID.",
    )
    parser.add_argument(
        "--api-hash",
        default=os.environ.get("TG_CLIENT_STDIO_API_HASH"),
        help="Telegram API hash; defaults to TG_CLIENT_STDIO_API_HASH.",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("TG_CLIENT_STDIO_SESSION"),
        help="Telethon session path/name; defaults to TG_CLIENT_STDIO_SESSION.",
    )
    parser.add_argument(
        "--phone",
        default=os.environ.get("TG_CLIENT_STDIO_PHONE"),
        help="Default phone number for auth.send_code.",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("TG_CLIENT_STDIO_PROXY"),
        help="Proxy URL; defaults to TG_CLIENT_STDIO_PROXY.",
    )
    parser.add_argument(
        "--live-poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds for the Telethon live listener.",
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
            phone=args.phone or "",
            proxy=parse_proxy(args.proxy) if args.proxy else None,
            live_poll_interval_seconds=args.live_poll_interval,
        )
    )


def parse_proxy(value: str) -> tuple[object, str, int, bool, str | None, str | None]:
    """Convert a proxy URL into the tuple accepted by Telethon/PySocks."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "socks5", "socks5h"}:
        raise ValueError("proxy scheme must be http, socks5, or socks5h")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("proxy must include host and port")
    try:
        import socks  # type: ignore
    except ImportError as exc:
        raise ValueError("install tg-client-stdio-worker[telegram] for proxy support") from exc

    proxy_type = socks.HTTP if parsed.scheme.lower() == "http" else socks.SOCKS5
    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None
    return (
        proxy_type,
        parsed.hostname,
        parsed.port,
        parsed.scheme.lower() != "socks5",
        username,
        password,
    )
