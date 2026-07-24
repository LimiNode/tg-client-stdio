from __future__ import annotations

import subprocess
import sys
import unittest

from tg_client_stdio_worker.protocol import MIN_MAX_JSONL_RECORD_BYTES
from tg_client_stdio_worker.process import WorkerProcess, WorkerProcessConfig


class WorkerProcessTest(unittest.TestCase):
    def test_starts_mock_worker_and_streams_export(self) -> None:
        worker = WorkerProcess(
            WorkerProcessConfig(
                args=["--mock"],
                executable=sys.executable,
                startup_hello=False,
            )
        )
        try:
            client = worker.start(stderr=subprocess.DEVNULL)
            hello = client.hello({"client_name": "unit-test"})
            dialogs = client.dialogs()
            messages: list[dict] = []
            summary = client.stream_messages({"chat": "-10042"}, messages.append)

            self.assertEqual(hello["worker_name"], "tg-client-stdio-worker")
            self.assertEqual(dialogs[0]["chat_id"], "-1001234567890")
            self.assertEqual(summary.messages, 2)
            self.assertEqual(messages[0]["message_identity"], "telegram:-10042:0:1234")
        finally:
            worker.close()

    def test_context_manager_closes_worker(self) -> None:
        worker = WorkerProcess(
            WorkerProcessConfig(
                args=["--mock"],
                executable=sys.executable,
            )
        )

        with worker as client:
            self.assertEqual(client.dialogs()[0]["kind"], "channel")

        self.assertIsNone(worker.client)

    def test_start_cleans_up_when_startup_hello_times_out(self) -> None:
        worker = WorkerProcess(
            WorkerProcessConfig(
                command=[sys.executable, "-c", "import time; time.sleep(60)"],
                startup_timeout_seconds=0.1,
                shutdown_timeout_seconds=0.1,
            )
        )

        with self.assertRaises(TimeoutError):
            worker.start(stderr=subprocess.DEVNULL)

        self.assertIsNone(worker.client)

    def test_invalid_jsonl_config_is_rejected_before_process_start(self) -> None:
        with self.assertRaises(ValueError):
            WorkerProcess(
                WorkerProcessConfig(
                    command=[sys.executable, "-c", "import time; time.sleep(60)"],
                    max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES - 1,
                )
            )

    def test_close_does_not_wait_forever_for_shutdown_response(self) -> None:
        script = (
            "import sys, time\n"
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write("
            "b'{\"protocol_version\":1,\"message_type\":\"response\",\"request_id\":1,"
            "\"operation\":\"hello\",\"payload\":{\"capabilities\":{\"max_jsonl_record_bytes\":1048576}}}\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(60)\n"
        )
        worker = WorkerProcess(
            WorkerProcessConfig(
                command=[sys.executable, "-c", script],
                startup_timeout_seconds=1.0,
                shutdown_timeout_seconds=0.1,
            )
        )

        client = worker.start(stderr=subprocess.DEVNULL)
        self.assertIsNotNone(client)
        worker.close()

        self.assertIsNone(worker.client)

    def test_invalid_close_timeout_preserves_process_ownership(self) -> None:
        worker = WorkerProcess(
            WorkerProcessConfig(
                args=["--mock"],
                executable=sys.executable,
                startup_hello=False,
                shutdown_timeout_seconds=0.1,
            )
        )

        try:
            client = worker.start(stderr=subprocess.DEVNULL)
            self.assertIsNotNone(client)

            with self.assertRaises(ValueError):
                worker.close(timeout_seconds=0)
            self.assertIsNotNone(worker.client)
            self.assertIsNotNone(worker._process)
            assert worker._process is not None
            self.assertIsNone(worker._process.poll())

            with self.assertRaises(ValueError):
                worker.close(timeout_seconds=-1)
            self.assertIsNotNone(worker.client)
            self.assertIsNotNone(worker._process)
            assert worker._process is not None
            self.assertIsNone(worker._process.poll())
        finally:
            worker.close(timeout_seconds=0.1)

        self.assertIsNone(worker.client)
        self.assertIsNone(worker._process)


if __name__ == "__main__":
    unittest.main()
