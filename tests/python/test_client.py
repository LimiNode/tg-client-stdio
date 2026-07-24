from __future__ import annotations

import io
import json
import unittest

from tg_client_stdio_worker.client import JsonlWorkerClient, WorkerClientError
from tg_client_stdio_worker.protocol import Envelope, encode_envelope


def record(message_type: str, request_id: int, operation: str, payload: dict) -> bytes:
    return encode_envelope(
        Envelope(
            message_type=message_type,  # type: ignore[arg-type]
            request_id=request_id,
            operation=operation,
            payload=payload,
        ),
        max_jsonl_bytes=1024 * 1024,
    )


class JsonlWorkerClientTest(unittest.TestCase):
    def test_sends_request_and_reads_response(self) -> None:
        input_stream = io.BytesIO(record("response", 1, "hello", {"ok": True}))
        output_stream = io.BytesIO()
        client = JsonlWorkerClient(input_stream, output_stream)

        self.assertEqual(client.hello(), {"ok": True})

        sent = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(sent["message_type"], "request")
        self.assertEqual(sent["request_id"], 1)
        self.assertEqual(sent["operation"], "hello")

    def test_streams_export_messages_until_terminal_response(self) -> None:
        input_stream = io.BytesIO(
            record("event", 1, "export.started", {}) +
            record("event", 1, "export.message", {"message": {"message_id": 10}}) +
            record("event", 1, "export.message", {"message": {"message_id": 11}}) +
            record("response", 1, "messages.export", {"messages": 2, "truncated": False})
        )
        output_stream = io.BytesIO()
        client = JsonlWorkerClient(input_stream, output_stream)
        messages: list[dict] = []

        summary = client.stream_messages({"chat": "-100"}, messages.append)

        self.assertEqual([item["message_id"] for item in messages], [10, 11])
        self.assertEqual(summary.messages, 2)
        self.assertFalse(summary.truncated)

    def test_worker_error_raises_typed_exception(self) -> None:
        input_stream = io.BytesIO(
            record(
                "error",
                1,
                "dialogs.list",
                {"code": "auth_required", "message": "login first", "fatal": False},
            )
        )
        client = JsonlWorkerClient(input_stream, io.BytesIO())

        with self.assertRaises(WorkerClientError) as caught:
            client.dialogs()

        self.assertEqual(caught.exception.code, "auth_required")
        self.assertFalse(caught.exception.fatal)

    def test_rejects_oversized_inbound_record(self) -> None:
        client = JsonlWorkerClient(
            io.BytesIO(record("response", 1, "hello", {"padding": "x" * 512})),
            io.BytesIO(),
            max_jsonl_bytes=256,
        )

        with self.assertRaises(WorkerClientError) as caught:
            client.hello()

        self.assertEqual(caught.exception.code, "jsonl_record_too_large")
        self.assertTrue(caught.exception.fatal)


if __name__ == "__main__":
    unittest.main()
