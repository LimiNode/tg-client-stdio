from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tg_client_stdio_worker import operator_cli

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "tg_client_cli.py"


class OperatorCliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dialogs_search_json(self) -> None:
        result = self.run_cli("dialogs", "--mock", "--search", "signal", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{
                "chat_id": "-1001234567890",
                "title": "Signals",
                "username": "signals",
                "kind": "channel",
            }],
        )

    def test_export_is_streamed_as_jsonl(self) -> None:
        result = self.run_cli(
            "export",
            "--mock",
            "--chat",
            "-10042",
            "--from",
            "2026-07-23",
            "--to",
            "2026-07-23",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([record["message_id"] for record in records], [1234, 1235])
        self.assertIn("exported=2", result.stderr)
        self.assertIn("truncated=false", result.stderr)

    def test_export_rejects_reversed_date_range(self) -> None:
        result = self.run_cli(
            "export",
            "--mock",
            "--chat",
            "-10042",
            "--from",
            "1784830300000",
            "--to",
            "1784830000000",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--from must not be later than --to", result.stderr)

    def test_export_output_preserves_existing_file_on_worker_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "history.jsonl"
            output.write_text("old archive\n", encoding="utf-8")

            failed = self.run_cli(
                "export",
                "--chat",
                "-10042",
                "--output",
                str(output),
            )

            self.assertEqual(failed.returncode, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "old archive\n")

            succeeded = self.run_cli(
                "export",
                "--mock",
                "--chat",
                "-10042",
                "--output",
                str(output),
            )

            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["message_id"] for record in records], [1234, 1235])

    def test_unicode_output_is_utf8_when_console_uses_legacy_encoding(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")
        with mock.patch.object(operator_cli.sys, "stdout", stream):
            operator_cli._configure_utf8_stdio()
            operator_cli._print_dialogs([{
                "chat_id": "-10042",
                "title": "Сигналы MONEY BOT",
                "username": "",
                "kind": "channel",
            }], as_json=True)
            stream.flush()

        decoded = buffer.getvalue().decode("utf-8")
        self.assertEqual(
            json.loads(decoded),
            [{
                "chat_id": "-10042",
                "title": "Сигналы MONEY BOT",
                "username": "",
                "kind": "channel",
            }],
        )


if __name__ == "__main__":
    unittest.main()
