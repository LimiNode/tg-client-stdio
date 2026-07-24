from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from .protocol import (
    DEFAULT_MAX_JSONL_RECORD_BYTES,
    Envelope,
    ProtocolError,
    decode_envelope,
    encode_envelope,
)


class WorkerClientError(RuntimeError):
    """Raised when the worker returns an error record or violates the protocol."""

    def __init__(self, code: str, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal


@dataclass
class ExportSummary:
    messages: int
    truncated: bool


class JsonlWorkerClient:
    """Small host-side helper for talking to one tg-client-stdio worker session."""

    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        max_jsonl_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES,
    ) -> None:
        if max_jsonl_bytes <= 0:
            raise ValueError("max_jsonl_bytes must be positive")
        self._input = input_stream
        self._output = output_stream
        self._max_jsonl_bytes = max_jsonl_bytes
        self._next_request_id = 1

    def request(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._send_request(operation, payload or {})
        while True:
            record = self._read_record()
            if record.request_id != request_id:
                raise WorkerClientError(
                    "unexpected_record",
                    f"unexpected request_id {record.request_id}",
                    fatal=True,
                )
            if record.message_type == "response":
                if record.operation != operation:
                    raise WorkerClientError(
                        "unexpected_operation",
                        f"unexpected terminal operation {record.operation}",
                        fatal=True,
                    )
                return record.payload
            if record.message_type == "error":
                raise self._worker_error(record)
            raise WorkerClientError(
                "unexpected_event",
                f"unexpected event before terminal response: {record.operation}",
            )

    def hello(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("hello", payload)

    def dialogs(self) -> list[dict[str, Any]]:
        payload = self.request("dialogs.list")
        dialogs = payload.get("dialogs")
        if not isinstance(dialogs, list):
            raise WorkerClientError("invalid_response", "dialogs response must contain an array")
        return dialogs

    def stream_messages(
        self,
        query: dict[str, Any],
        on_message: Callable[[dict[str, Any]], None],
    ) -> ExportSummary:
        request_id = self._send_request("messages.export", query)
        while True:
            record = self._read_record()
            if record.request_id != request_id:
                raise WorkerClientError(
                    "unexpected_record",
                    f"unexpected request_id {record.request_id}",
                    fatal=True,
                )
            if record.message_type == "event":
                if record.operation == "export.started":
                    continue
                if record.operation != "export.message":
                    raise WorkerClientError(
                        "unexpected_event",
                        f"unexpected export event {record.operation}",
                    )
                message = record.payload.get("message")
                if not isinstance(message, dict):
                    raise WorkerClientError(
                        "invalid_event",
                        "export.message payload must contain a message object",
                    )
                on_message(message)
                continue
            if record.message_type == "response":
                if record.operation != "messages.export":
                    raise WorkerClientError(
                        "unexpected_operation",
                        f"unexpected terminal operation {record.operation}",
                        fatal=True,
                    )
                return ExportSummary(
                    messages=int(record.payload.get("messages", 0)),
                    truncated=bool(record.payload.get("truncated", False)),
                )
            if record.message_type == "error":
                raise self._worker_error(record)
            raise WorkerClientError("unexpected_record", "unexpected worker record")

    def shutdown(self) -> dict[str, Any]:
        return self.request("shutdown")

    def _send_request(self, operation: str, payload: dict[str, Any]) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        envelope = Envelope(
            message_type="request",
            request_id=request_id,
            operation=operation,
            payload=payload,
        )
        self._output.write(encode_envelope(envelope, self._max_jsonl_bytes))
        self._output.flush()
        return request_id

    def _read_record(self) -> Envelope:
        raw = self._input.readline(self._max_jsonl_bytes + 1)
        if raw == b"":
            raise WorkerClientError("worker_eof", "worker stdout closed", fatal=True)
        if len(raw) > self._max_jsonl_bytes:
            raise WorkerClientError(
                "jsonl_record_too_large",
                "inbound JSONL record is too large",
                fatal=True,
            )
        if not raw.endswith(b"\n"):
            raise WorkerClientError(
                "unterminated_jsonl_record",
                "inbound JSONL record must end with LF",
                fatal=True,
            )
        try:
            return decode_envelope(raw)
        except ProtocolError as exc:
            raise WorkerClientError(exc.code, exc.message, fatal=True) from exc

    @staticmethod
    def _worker_error(record: Envelope) -> WorkerClientError:
        return WorkerClientError(
            str(record.payload.get("code", "worker_error")),
            str(record.payload.get("message", "worker error")),
            bool(record.payload.get("fatal", False)),
        )
