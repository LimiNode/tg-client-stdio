from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from .protocol import (
    DEFAULT_MAX_JSONL_RECORD_BYTES,
    MIN_MAX_JSONL_RECORD_BYTES,
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
        if max_jsonl_bytes < MIN_MAX_JSONL_RECORD_BYTES:
            raise ValueError(
                f"max_jsonl_bytes must be at least {MIN_MAX_JSONL_RECORD_BYTES}")
        self._input = input_stream
        self._output = output_stream
        self._max_inbound_jsonl_bytes = max_jsonl_bytes
        self._local_max_outbound_jsonl_bytes = max_jsonl_bytes
        self._max_outbound_jsonl_bytes = max_jsonl_bytes
        self._next_request_id = 1
        self._session_error: WorkerClientError | None = None

    def request(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._send_request(operation, payload or {})
        while True:
            record = self._read_record()
            if record.request_id != request_id:
                raise self._poison_session(
                    "unexpected_record",
                    f"unexpected request_id {record.request_id}",
                )
            if record.message_type == "response":
                if record.operation != operation:
                    raise self._poison_session(
                        "unexpected_operation",
                        f"unexpected terminal operation {record.operation}",
                    )
                return record.payload
            if record.message_type == "error":
                worker_error = self._worker_error(record)
                if worker_error.fatal:
                    self._poison_session(worker_error.code, worker_error.message)
                raise worker_error
            raise self._poison_session(
                "unexpected_event",
                f"unexpected event before terminal response: {record.operation}",
            )

    def hello(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response_payload = self.request("hello", payload)
        self._apply_hello_capabilities(response_payload)
        return response_payload

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
                raise self._poison_session(
                    "unexpected_record",
                    f"unexpected request_id {record.request_id}",
                )
            if record.message_type == "event":
                if record.operation == "export.started":
                    continue
                if record.operation != "export.message":
                    raise self._poison_session(
                        "unexpected_event",
                        f"unexpected export event {record.operation}",
                    )
                message = record.payload.get("message")
                if not isinstance(message, dict):
                    raise self._poison_session(
                        "invalid_event",
                        "export.message payload must contain a message object",
                    )
                try:
                    on_message(message)
                except BaseException as exc:
                    failure = self._poison_session(
                        "callback_failed",
                        "export callback failed; worker session is unusable",
                    )
                    raise failure from exc
                continue
            if record.message_type == "response":
                if record.operation != "messages.export":
                    raise self._poison_session(
                        "unexpected_operation",
                        f"unexpected terminal operation {record.operation}",
                    )
                messages = record.payload.get("messages")
                truncated = record.payload.get("truncated")
                if type(messages) is not int or messages < 0:
                    raise self._poison_session(
                        "invalid_response",
                        "messages.export response messages must be a non-negative integer",
                    )
                if type(truncated) is not bool:
                    raise self._poison_session(
                        "invalid_response",
                        "messages.export response truncated must be boolean",
                    )
                return ExportSummary(messages=messages, truncated=truncated)
            if record.message_type == "error":
                worker_error = self._worker_error(record)
                if worker_error.fatal:
                    self._poison_session(worker_error.code, worker_error.message)
                raise worker_error
            raise self._poison_session("unexpected_record", "unexpected worker record")

    def shutdown(self) -> dict[str, Any]:
        return self.request("shutdown")

    def _send_request(self, operation: str, payload: dict[str, Any]) -> int:
        self._ensure_session_usable()
        request_id = self._next_request_id
        self._next_request_id += 1
        envelope = Envelope(
            message_type="request",
            request_id=request_id,
            operation=operation,
            payload=payload,
        )
        self._output.write(encode_envelope(envelope, self._max_outbound_jsonl_bytes))
        self._output.flush()
        return request_id

    def _read_record(self) -> Envelope:
        raw = self._input.readline(self._max_inbound_jsonl_bytes + 1)
        if raw == b"":
            raise self._poison_session("worker_eof", "worker stdout closed")
        if len(raw) > self._max_inbound_jsonl_bytes:
            raise self._poison_session(
                "jsonl_record_too_large",
                "inbound JSONL record is too large",
            )
        if not raw.endswith(b"\n"):
            raise self._poison_session(
                "unterminated_jsonl_record",
                "inbound JSONL record must end with LF",
            )
        try:
            return decode_envelope(raw)
        except ProtocolError as exc:
            raise self._poison_session(exc.code, exc.message) from exc

    def _ensure_session_usable(self) -> None:
        if self._session_error is not None:
            raise self._session_error

    def _poison_session(self, code: str, message: str) -> WorkerClientError:
        if self._session_error is None:
            self._session_error = WorkerClientError(code, message, fatal=True)
        return self._session_error

    @staticmethod
    def _worker_error(record: Envelope) -> WorkerClientError:
        code = record.payload.get("code")
        message = record.payload.get("message")
        fatal = record.payload.get("fatal")
        if not isinstance(code, str) or not code:
            return WorkerClientError(
                "invalid_error_payload",
                "worker error payload code must be a non-empty string",
                fatal=True,
            )
        if not isinstance(message, str):
            return WorkerClientError(
                "invalid_error_payload",
                "worker error payload message must be a string",
                fatal=True,
            )
        if type(fatal) is not bool:
            return WorkerClientError(
                "invalid_error_payload",
                "worker error payload fatal must be boolean",
                fatal=True,
            )
        return WorkerClientError(
            code,
            message,
            fatal,
        )

    def _apply_hello_capabilities(self, payload: dict[str, Any]) -> None:
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict):
            raise self._poison_session(
                "invalid_response",
                "hello response must contain capabilities",
            )
        max_jsonl_record_bytes = capabilities.get("max_jsonl_record_bytes")
        if (
            type(max_jsonl_record_bytes) is not int
            or max_jsonl_record_bytes < MIN_MAX_JSONL_RECORD_BYTES
        ):
            raise self._poison_session(
                "invalid_response",
                "hello capabilities max_jsonl_record_bytes is below protocol minimum",
            )
        self._max_outbound_jsonl_bytes = min(
            self._local_max_outbound_jsonl_bytes,
            max_jsonl_record_bytes,
        )
