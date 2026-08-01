from __future__ import annotations

import contextlib
import io
import json
import unittest
from collections.abc import Iterable
from typing import Any

from tg_client_stdio_worker.backend import (
    ExportQuery,
    LiveQuery,
    MockTelegramBackend,
    RawMessage,
)
from tg_client_stdio_worker.cli import build_parser, main
from tg_client_stdio_worker.protocol import (
    MIN_MAX_JSONL_RECORD_BYTES,
    Envelope,
    ProtocolError,
    decode_envelope,
    encode_envelope,
)
from tg_client_stdio_worker.server import JsonlWorkerServer, ServerConfig


def request(request_id: int, operation: str, payload: dict | None = None) -> bytes:
    return encode_envelope(
        Envelope(
            message_type="request",
            request_id=request_id,
            operation=operation,
            payload=payload or {},
        ),
        max_jsonl_bytes=1024 * 1024,
    )


class ProtocolTest(unittest.TestCase):
    def test_live_listener_emits_worker_originated_event_and_stops(self) -> None:
        backend = MockTelegramBackend()
        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=backend,
        )

        server._handle_request(Envelope(
            message_type="request",
            request_id=10,
            operation="messages.listen",
            payload={"chats": ["-10042"]},
        ))
        backend.emit_live_message(RawMessage(
            chat_id="-10042",
            chat_title="Signals",
            topic_id="0",
            message_id=9,
            date_ms=1784830000000,
            text="EURUSD BUY 5m",
        ))
        server._handle_request(Envelope(
            message_type="request",
            request_id=11,
            operation="messages.stop",
            payload={},
        ))

        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        self.assertEqual(records[0]["operation"], "messages.listen")
        self.assertEqual(records[0]["message_type"], "response")
        self.assertEqual(records[1]["request_id"], 0)
        self.assertEqual(records[1]["operation"], "message.received")
        self.assertEqual(records[1]["payload"]["message"]["message_id"], 9)
        self.assertEqual(records[2]["operation"], "messages.stop")

    def test_live_query_requires_unique_non_empty_chats(self) -> None:
        with self.assertRaises(ValueError):
            LiveQuery.from_payload({"chats": []})
        with self.assertRaises(ValueError):
            LiveQuery.from_payload({"chats": ["-10042", "-10042"]})

    def test_round_trips_envelope(self) -> None:
        line = request(7, "hello", {"client_name": "unit-test"})

        envelope = decode_envelope(line)

        self.assertEqual(envelope.protocol_version, 1)
        self.assertEqual(envelope.message_type, "request")
        self.assertEqual(envelope.request_id, 7)
        self.assertEqual(envelope.operation, "hello")
        self.assertEqual(envelope.payload["client_name"], "unit-test")

    def test_mock_export_streams_messages(self) -> None:
        input_stream = io.BytesIO(
            request(1, "hello") +
            request(2, "dialogs.list") +
            request(3, "messages.export", {"chat": "-10042"}) +
            request(4, "shutdown")
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)

        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        self.assertEqual(records[0]["payload"]["capabilities"]["max_jsonl_record_bytes"], 1024 * 1024)
        self.assertEqual(records[0]["message_type"], "response")
        self.assertEqual(records[0]["operation"], "hello")
        self.assertEqual(records[1]["operation"], "dialogs.list")

        export_records = [item for item in records if item["request_id"] == 3]
        self.assertEqual(
            [item["operation"] for item in export_records],
            ["export.started", "export.message", "export.message", "messages.export"],
        )
        self.assertEqual(export_records[-1]["message_type"], "response")
        self.assertEqual(export_records[-1]["payload"]["messages"], 2)
        self.assertEqual(
            export_records[1]["payload"]["message"]["message_identity"],
            "telegram:-10042:0:1234",
        )
        self.assertEqual(
            export_records[1]["payload"]["message"]["revision_identity"],
            "telegram:-10042:0:1234:0",
        )

    def test_rejects_invalid_export_query(self) -> None:
        input_stream = io.BytesIO(request(3, "messages.export", {"limit": 0}))
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["operation"], "messages.export")
        self.assertEqual(record["payload"]["code"], "invalid_export_query")

    def test_export_query_filters_by_date_limit_and_order(self) -> None:
        input_stream = io.BytesIO(
            request(
                3,
                "messages.export",
                {
                    "chat": "-10042",
                    "from_date_ms": 1784830000000,
                    "order": "newest_first",
                    "limit": 1,
                },
            )
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        message_records = [item for item in records if item["operation"] == "export.message"]
        self.assertEqual(len(message_records), 1)
        self.assertEqual(message_records[0]["payload"]["message"]["message_id"], 1235)
        self.assertEqual(records[-1]["operation"], "messages.export")
        self.assertEqual(records[-1]["payload"]["messages"], 1)
        self.assertTrue(records[-1]["payload"]["truncated"])

    def test_export_query_reports_not_truncated_when_limit_exhausts_results(self) -> None:
        input_stream = io.BytesIO(
            request(
                3,
                "messages.export",
                {
                    "chat": "-10042",
                    "from_date_ms": 1784830200000,
                    "limit": 1,
                },
            )
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        self.assertEqual(records[-1]["payload"]["messages"], 1)
        self.assertFalse(records[-1]["payload"]["truncated"])

    def test_export_query_rejects_invalid_date_range(self) -> None:
        input_stream = io.BytesIO(
            request(
                3,
                "messages.export",
                {
                    "chat": "-10042",
                    "from_date_ms": 1784830300000,
                    "to_date_ms": 1784830200000,
                },
            )
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 3)
        self.assertEqual(record["operation"], "messages.export")
        self.assertEqual(record["payload"]["code"], "invalid_export_query")

    def test_rejects_oversized_inbound_record(self) -> None:
        input_stream = io.BytesIO(request(1, "hello", {"padding": "x" * 512}))
        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
            config=ServerConfig(max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["payload"]["code"], "jsonl_record_too_large")

    def test_hello_fits_minimum_jsonl_limit(self) -> None:
        input_stream = io.BytesIO(request(1, "hello"))
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
            config=ServerConfig(max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES),
        )

        self.assertEqual(server.run(), 0)
        self.assertLessEqual(
            len(output_stream.getvalue()),
            MIN_MAX_JSONL_RECORD_BYTES,
        )
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["operation"], "hello")

    def test_payload_validation_error_keeps_request_terminal(self) -> None:
        raw = (
            b'{"protocol_version":1,"message_type":"request","request_id":42,'
            b'"operation":"messages.export","payload":[]}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 42)
        self.assertEqual(record["operation"], "messages.export")
        self.assertEqual(record["payload"]["code"], "invalid_payload")

    def test_unsupported_version_keeps_request_terminal_when_identity_is_known(self) -> None:
        raw = (
            b'{"protocol_version":999,"message_type":"request","request_id":43,'
            b'"operation":"hello","payload":{}}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 43)
        self.assertEqual(record["operation"], "hello")
        self.assertEqual(record["payload"]["code"], "unsupported_protocol_version")

    def test_malformed_uncorrelated_json_is_session_fatal(self) -> None:
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(b'{"protocol_version":1,\n'),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 0)
        self.assertTrue(record["payload"]["fatal"])

    def test_unterminated_jsonl_record_is_session_fatal(self) -> None:
        raw = b'{"protocol_version":1,"message_type":"request","request_id":1,"operation":"hello","payload":{}}'
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["payload"]["code"], "unterminated_jsonl_record")
        self.assertTrue(record["payload"]["fatal"])

    def test_rejects_boolean_protocol_version_and_request_id(self) -> None:
        raw = (
            b'{"protocol_version":true,"message_type":"request","request_id":true,'
            b'"operation":"hello","payload":{}}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 0)
        self.assertEqual(record["payload"]["code"], "unsupported_protocol_version")
        self.assertTrue(record["payload"]["fatal"])

    def test_rejects_boolean_request_id(self) -> None:
        raw = (
            b'{"protocol_version":1,"message_type":"request","request_id":true,'
            b'"operation":"hello","payload":{}}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 0)
        self.assertEqual(record["payload"]["code"], "invalid_request_id")
        self.assertTrue(record["payload"]["fatal"])

    def test_rejects_request_id_larger_than_uint64(self) -> None:
        raw = (
            b'{"protocol_version":1,"message_type":"request",'
            b'"request_id":18446744073709551616,"operation":"hello","payload":{}}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 0)
        self.assertEqual(record["payload"]["code"], "invalid_request_id")
        self.assertTrue(record["payload"]["fatal"])

    def test_server_config_rejects_too_small_jsonl_limit(self) -> None:
        with self.assertRaises(ValueError):
            ServerConfig(max_jsonl_bytes=0)
        with self.assertRaises(ValueError):
            ServerConfig(max_jsonl_bytes=-2)
        with self.assertRaises(ValueError):
            ServerConfig(max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES - 1)

    def test_cli_rejects_too_small_jsonl_limit(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--mock", "--max-jsonl-bytes", "-2"])

    def test_cli_requires_explicit_backend(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([]), 2)
        self.assertIn("one of --mock or --backend must be specified", stderr.getvalue())

    def test_rejects_nan_and_infinity_on_input(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            raw = (
                b'{"protocol_version":1,"message_type":"request","request_id":47,'
                b'"operation":"hello","payload":{"value":' + constant + b"}}\n"
            )
            output_stream = io.BytesIO()

            server = JsonlWorkerServer(
                input_stream=io.BytesIO(raw),
                output_stream=output_stream,
                error_stream=io.StringIO(),
                backend=MockTelegramBackend(),
            )

            self.assertEqual(server.run(), 1)
            record = json.loads(output_stream.getvalue().decode("utf-8"))
            self.assertEqual(record["message_type"], "error")
            self.assertEqual(record["request_id"], 0)
            self.assertEqual(record["payload"]["code"], "invalid_json")
            self.assertTrue(record["payload"]["fatal"])

    def test_rejects_nan_on_output(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_envelope(
                Envelope(
                    message_type="response",
                    request_id=1,
                    operation="hello",
                    payload={"value": float("nan")},
                ),
                max_jsonl_bytes=1024 * 1024,
            )

    def test_oversized_export_event_returns_terminal_error(self) -> None:
        class LargeMessageBackend(MockTelegramBackend):
            def iter_export_messages(self, query: ExportQuery) -> Iterable[RawMessage]:
                yield RawMessage(
                    chat_id="-100",
                    chat_title="Signals",
                    topic_id="0",
                    message_id=1,
                    date_ms=1,
                    text="x" * 512,
                )

        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(request(44, "messages.export", {"chat": "-100"})),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=LargeMessageBackend(),
            config=ServerConfig(max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES),
        )

        self.assertEqual(server.run(), 0)
        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        self.assertEqual(records[0]["operation"], "export.started")
        self.assertEqual(records[1]["message_type"], "error")
        self.assertEqual(records[1]["request_id"], 44)
        self.assertEqual(records[1]["operation"], "messages.export")
        self.assertEqual(records[1]["payload"]["code"], "jsonl_record_too_large")

    def test_unserializable_terminal_error_fails_session(self) -> None:
        huge_operation = "x" * 360
        raw = (
            b'{"protocol_version":1,"message_type":"request","request_id":45,'
            b'"operation":"' + huge_operation.encode("ascii") + b'","payload":[]}\n'
        )
        output_stream = io.BytesIO()
        errors = io.StringIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=errors,
            backend=MockTelegramBackend(),
            config=ServerConfig(max_jsonl_bytes=MIN_MAX_JSONL_RECORD_BYTES),
        )

        self.assertEqual(server.run(), 1)
        self.assertEqual(output_stream.getvalue(), b"")
        self.assertIn("failed to serialize", errors.getvalue())

    def test_lone_surrogate_operation_does_not_crash_worker(self) -> None:
        raw = (
            b'{"protocol_version":1,"message_type":"request","request_id":46,'
            b'"operation":"\\ud800","payload":{}}\n'
        )
        output_stream = io.BytesIO()

        server = JsonlWorkerServer(
            input_stream=io.BytesIO(raw),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=MockTelegramBackend(),
        )

        self.assertEqual(server.run(), 0)
        self.assertIn(b"\\ud800", output_stream.getvalue())
        record = json.loads(output_stream.getvalue().decode("ascii"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["request_id"], 46)
        self.assertEqual(record["payload"]["code"], "unknown_operation")


if __name__ == "__main__":
    unittest.main()
