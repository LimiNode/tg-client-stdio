#!/usr/bin/env python3
"""Create or authorize one local Telethon session for tg-client-stdio."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from tg_client_stdio_worker.backend import BackendError
from tg_client_stdio_worker.cli import parse_proxy
from tg_client_stdio_worker.telethon_backend import TelethonBackend, TelethonBackendConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authorize one Telegram user session for tg-client-stdio.",
    )
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", required=True, help="Telethon session path/name.")
    parser.add_argument("--phone", help="Phone number in international format.")
    parser.add_argument(
        "--proxy",
        help="Proxy URL: http://, socks5://, or socks5h://.",
    )
    return parser


def authorize(args: argparse.Namespace) -> int:
    Path(args.session).expanduser().parent.mkdir(parents=True, exist_ok=True)
    backend = TelethonBackend(
        TelethonBackendConfig(
            api_id=args.api_id,
            api_hash=args.api_hash,
            session=args.session,
            phone=args.phone or "",
            proxy=parse_proxy(args.proxy) if args.proxy else None,
        )
    )
    try:
        status = backend.auth_status()
        if not status["authorized"]:
            phone = (args.phone or input("Telegram phone number: ")).strip()
            if not phone:
                raise ValueError("phone number must not be empty")

            backend.auth_send_code(phone)
            code = input("Telegram login code: ").strip()
            status = backend.auth_submit_code(code)
            if status["password_required"]:
                password = getpass.getpass("Telegram 2FA password: ")
                status = backend.auth_submit_password(password)

        if not status["authorized"]:
            print("Telegram session is not authorized.", file=sys.stderr)
            return 1

        print(json.dumps({
            "authorized": True,
            "session": args.session,
            "proxy_configured": bool(args.proxy),
        }, ensure_ascii=True, sort_keys=True))
        return 0
    finally:
        backend.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return authorize(args)
    except (BackendError, ValueError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
