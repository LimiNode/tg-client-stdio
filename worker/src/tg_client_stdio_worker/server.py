from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import Any, BinaryIO, TextIO

from . import __version__
from .backend import BackendError, ExportQuery, MockTelegramBackend, TelegramBackend
from .protocol import (
    DEFAULT_MAX_JSONL_RECORD_BYTES,
    Envelope,
    MIN_MAX_JSONL_RECORD_BYTES,
    ProtocolError,
    RequestIdentity,
    decode_envelope,
    encode_envelope,
    error,
    error_for_identity,
    event,
    extract_request_identity,
    response,
)


@dataclass
class ServerConfig:
    max_jsonl_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES

    def __post_init__(self) -> None:
        if self.max_jsonl_bytes < MIN_MAX_JSONL_RECORD_BYTES:
            raise ValueError(
                f"max_jsonl_bytes must be at least {MIN_MAX_JSONL_RECORD_BYTES}")


class JsonlWorkerServer:
    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        error_stream: TextIO,
        backend: TelegramBackend,
        config: ServerConfig | None = None,
        backend_name: str | None = None,
    ) -> None:
        self._input = input_stream
        self._output = output_stream
        self._error = error_stream
        self._backend = backend
        self._config = config or ServerConfig()
        self._backend_name = backend_name or _infer_backend_name(backend)
        self._shutdown_requested = False
        self._exit_code = 0

    def run(self) -> int:
        try:
            return self._run_loop()
        finally:
            self._close_backend()

    def _run_loop(self) -> int:
        while not self._shutdown_requested:
            raw = self._input.readline(self._config.max_jsonl_bytes + 1)
            if raw == b"":
                return 0
            if len(raw) > self._config.max_jsonl_bytes:
                self._write_session_error(
                    "jsonl_record_too_large",
                    "inbound JSONL record is too large",
                    fatal=True,
                )
                return 1
            if not raw.endswith(b"\n"):
                self._write_session_error(
                    "unterminated_jsonl_record",
                    "inbound JSONL record must end with LF",
                    fatal=True,
                )
                return 1

            request: Envelope | None = None
            try:
                request = decode_envelope(raw)
                if request.message_type != "request":
                    self._write(error(request, "invalid_message_direction", "worker accepts only request records"))
                    continue
                self._handle_request(request)
            except ProtocolError as exc:
                if request is not None:
                    if not self._try_write_terminal_error(request, exc):
                        return 1
                    continue

                identity = extract_request_identity(raw)
                if identity is not None:
                    if not self._try_write_identity_error(identity, exc):
                        return 1
                    continue

                self._write_session_error(exc.code, exc.message, fatal=True)
                return 1
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                print(f"unexpected worker error: {exc}", file=self._error)
                if request is not None:
                    failure = ProtocolError("internal_error", "unexpected worker error")
                    if not self._try_write_terminal_error(request, failure, fatal=True):
                        return 1
                    return 1
                self._write_session_error("internal_error", "unexpected worker error", fatal=True)
                return 1
        return self._exit_code

    def _close_backend(self) -> None:
        try:
            self._backend.close()
        except Exception as exc:
            print(f"failed to close backend: {exc}", file=self._error)

    def _handle_request(self, request: Envelope) -> None:
        if request.request_id == 0:
            self._write(error(request, "invalid_request_id", "request_id must be non-zero"))
            return

        if request.operation == "hello":
            self._write(response(request, self._hello_payload()))
            return

        if request.operation == "dialogs.list":
            try:
                dialogs = [dialog.to_payload() for dialog in self._backend.dialogs()]
            except BackendError as exc:
                self._write_backend_error(request, exc)
                return
            self._write(response(request, {"dialogs": dialogs}))
            return

        if request.operation == "messages.export":
            try:
                query = ExportQuery.from_payload(request.payload)
            except ValueError as exc:
                self._write(error(request, "invalid_export_query", str(exc)))
                return
            self._write(event(request.request_id, "export.started", {}))
            count = 0
            truncated = False
            backend_query = query
            if query.limit is not None:
                backend_query = replace(query, limit=query.limit + 1)
            try:
                for message in self._backend.iter_export_messages(backend_query):
                    if query.limit is not None and count >= query.limit:
                        truncated = True
                        break
                    self._write(event(request.request_id, "export.message", {"message": message.to_payload()}))
                    count += 1
            except BackendError as exc:
                self._write_backend_error(request, exc)
                return
            self._write(response(request, {"messages": count, "truncated": truncated}))
            return

        if request.operation == "shutdown":
            self._write(response(request, {"accepted": True}))
            self._shutdown_requested = True
            return

        self._write(error(request, "unknown_operation", f"unknown operation: {request.operation}"))

    def _hello_payload(self) -> dict[str, Any]:
        return {
            "worker_name": "tg-client-stdio-worker",
            "worker_version": __version__,
            "backend": self._backend_name,
            "capabilities": {
                "dialogs_list": True,
                "messages_export": True,
                "messages_listen": False,
                "auth_interactive": False,
                "multi_account": False,
                "max_jsonl_record_bytes": self._config.max_jsonl_bytes,
            },
        }

    def _write(self, envelope: Envelope) -> None:
        self._output.write(encode_envelope(envelope, self._config.max_jsonl_bytes))
        self._output.flush()

    def _write_backend_error(self, request: Envelope, exc: BackendError) -> None:
        self._write(error(request, exc.code, exc.message, exc.fatal))
        if exc.fatal:
            self._shutdown_requested = True
            self._exit_code = 1

    def _write_session_error(self, code: str, message: str, fatal: bool) -> None:
        try:
            self._write(error(None, code, message, fatal))
        except ProtocolError as exc:
            print(f"failed to serialize session error: {exc.message}", file=self._error)

    def _try_write_terminal_error(
        self,
        request: Envelope,
        exc: ProtocolError,
        fatal: bool = False,
    ) -> bool:
        try:
            self._write(error(request, exc.code, exc.message, fatal))
            return True
        except ProtocolError as write_exc:
            print(f"failed to serialize terminal error: {write_exc.message}", file=self._error)
            return False

    def _try_write_identity_error(
        self,
        identity: RequestIdentity,
        exc: ProtocolError,
    ) -> bool:
        try:
            self._write(error_for_identity(identity, exc.code, exc.message))
            return True
        except ProtocolError as write_exc:
            print(f"failed to serialize correlated error: {write_exc.message}", file=self._error)
            return False


def _infer_backend_name(backend: TelegramBackend) -> str:
    if isinstance(backend, MockTelegramBackend):
        return "mock"
    return "unknown"
