from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
