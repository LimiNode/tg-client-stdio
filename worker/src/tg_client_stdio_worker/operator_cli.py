#!/usr/bin/env python3
"""Small operator CLI for dialogs listing and streaming message export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, TextIO

from tg_client_stdio_worker.client import WorkerClientError
from tg_client_stdio_worker.process import WorkerProcess, WorkerProcessConfig


def _configure_utf8_stdio() -> None:
    """Make human-facing CLI output independent of the Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


def _load_local_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


def _positive_int(value: str) -> int:
    parsed = int(value, 10)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the deterministic mock worker instead of Telegram.",
    )
    parser.add_argument(
        "--api-id",
        type=int,
        default=_env_int("TG_CLIENT_STDIO_API_ID"),
        help="Telegram API ID; defaults to TG_CLIENT_STDIO_API_ID.",
    )
    parser.add_argument(
        "--api-hash",
        default=os.environ.get("TG_CLIENT_STDIO_API_HASH", ""),
        help="Telegram API hash; defaults to TG_CLIENT_STDIO_API_HASH.",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("TG_CLIENT_STDIO_SESSION", ""),
        help="Telethon session path; defaults to TG_CLIENT_STDIO_SESSION.",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("TG_CLIENT_STDIO_PROXY", ""),
        help="Proxy URL; defaults to TG_CLIENT_STDIO_PROXY.",
    )
    parser.add_argument(
        "--max-jsonl-bytes",
        type=_positive_int,
        default=1024 * 1024,
        help="Maximum worker JSONL record size.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Telegram dialogs and stream raw message history.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dialogs = subparsers.add_parser(
        "dialogs",
        help="List dialogs visible to the current Telegram session.",
    )
    _add_connection_options(dialogs)
    dialogs.add_argument(
        "--search",
        default="",
        help="Case-insensitive substring filter for id, title, username, or kind.",
    )
    dialogs.add_argument(
        "--json",
        action="store_true",
        help="Write the filtered dialogs as one JSON array.",
    )

    export = subparsers.add_parser(
        "export",
        help="Stream raw message history as JSONL.",
    )
    _add_connection_options(export)
    export.add_argument("--chat", required=True, help="Chat id, username, or exact title.")
    export.add_argument("--topic-id", default="0")
    export.add_argument(
        "--from",
        dest="from_date",
        help="Inclusive start: UTC milliseconds or ISO-8601 date/time.",
    )
    export.add_argument(
        "--to",
        dest="to_date",
        help="Inclusive end: UTC milliseconds or ISO-8601 date/time.",
    )
    export.add_argument("--limit", type=_positive_int)
    export.add_argument(
        "--order",
        choices=("oldest_first", "newest_first"),
        default="oldest_first",
    )
    export.add_argument(
        "--output",
        default="-",
        help="Output JSONL path, or '-' for stdout.",
    )
    return parser


def _worker_args(args: argparse.Namespace) -> list[str]:
    if args.mock:
        return ["--mock"]
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
        raise ValueError("missing required Telethon options: " + ", ".join(missing))
    return ["--backend", "telethon"]


def _worker_environment(args: argparse.Namespace) -> dict[str, str] | None:
    if args.mock:
        return None
    environment = os.environ.copy()
    environment.update({
        "TG_CLIENT_STDIO_API_ID": str(args.api_id),
        "TG_CLIENT_STDIO_API_HASH": args.api_hash,
        "TG_CLIENT_STDIO_SESSION": args.session,
    })
    if args.proxy.strip():
        environment["TG_CLIENT_STDIO_PROXY"] = args.proxy
    else:
        environment.pop("TG_CLIENT_STDIO_PROXY", None)
    return environment


def _start_worker(args: argparse.Namespace) -> tuple[WorkerProcess, Any]:
    process = WorkerProcess(
        WorkerProcessConfig(
            args=_worker_args(args),
            environment=_worker_environment(args),
            max_jsonl_bytes=args.max_jsonl_bytes,
        )
    )
    try:
        return process, process.start(stderr=sys.stderr)
    except BaseException:
        process.close()
        raise


def _matches_dialog(dialog: dict[str, Any], search: str) -> bool:
    if not search:
        return True
    needle = search.casefold()
    haystack = " ".join(
        str(dialog.get(field, ""))
        for field in ("chat_id", "title", "username", "kind")
    ).casefold()
    return needle in haystack


def _print_dialogs(dialogs: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        json.dump(dialogs, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
        return
    if not dialogs:
        print("No dialogs matched.")
        return
    fields = ("chat_id", "kind", "title", "username")
    widths = {
        field: max(len(field), *(len(str(dialog.get(field, ""))) for dialog in dialogs))
        for field in fields
    }
    print("  ".join(field.upper().ljust(widths[field]) for field in fields))
    print("  ".join("-" * widths[field] for field in fields))
    for dialog in dialogs:
        print("  ".join(str(dialog.get(field, "")).ljust(widths[field]) for field in fields))


def run_dialogs(args: argparse.Namespace) -> int:
    process, client = _start_worker(args)
    try:
        dialogs = [dialog for dialog in client.dialogs() if _matches_dialog(dialog, args.search)]
        _print_dialogs(dialogs, args.json)
        return 0
    finally:
        process.close()


def _parse_date_bound(value: str, *, upper: bool) -> int:
    raw = value.strip()
    if not raw:
        raise ValueError("date bound must not be empty")
    if raw.isdecimal():
        result = int(raw, 10)
        if result < 0:
            raise ValueError("date bound must not be negative")
        return result

    try:
        if "T" not in raw and " " not in raw:
            parsed_date = date.fromisoformat(raw)
            parsed = datetime.combine(
                parsed_date,
                time.max if upper else time.min,
                tzinfo=timezone.utc,
            )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"invalid date '{value}'; use milliseconds or ISO-8601"
        ) from exc
    return int(parsed.timestamp() * 1000)


def _export_query(args: argparse.Namespace) -> dict[str, Any]:
    query: dict[str, Any] = {
        "chat": args.chat,
        "topic_id": args.topic_id,
        "order": args.order,
    }
    if args.from_date is not None:
        query["from_date_ms"] = _parse_date_bound(args.from_date, upper=False)
    if args.to_date is not None:
        query["to_date_ms"] = _parse_date_bound(args.to_date, upper=True)
    if args.limit is not None:
        query["limit"] = args.limit
    if (
        "from_date_ms" in query
        and "to_date_ms" in query
        and query["from_date_ms"] > query["to_date_ms"]
    ):
        raise ValueError("--from must not be later than --to")
    return query


def _open_output(path: str) -> tuple[TextIO, bool, Any]:
    if path == "-":
        return sys.stdout, False, lambda: None
    final = Path(path).expanduser()
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    output = part.open("w", encoding="utf-8", newline="\n")

    def commit() -> None:
        output.flush()
        os.fsync(output.fileno())
        output.close()
        os.replace(part, final)

    return output, True, commit


def run_export(args: argparse.Namespace) -> int:
    query = _export_query(args)
    output, close_output, commit_output = _open_output(args.output)
    process = None
    committed = False
    count = 0
    try:
        process, client = _start_worker(args)

        def write_message(message: dict[str, Any]) -> None:
            nonlocal count
            output.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")
            output.flush()
            count += 1

        summary = client.stream_messages(query, write_message)
        print(
            f"exported={count} worker_messages={summary.messages} "
            f"truncated={str(summary.truncated).lower()}",
            file=sys.stderr,
        )
        if close_output:
            commit_output()
            committed = True
        return 0
    finally:
        if process is not None:
            process.close()
        if close_output and not committed:
            output.close()


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    _load_local_env()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "dialogs":
            return run_dialogs(args)
        return run_export(args)
    except (OSError, RuntimeError, ValueError, WorkerClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
