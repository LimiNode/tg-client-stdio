from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1
DEFAULT_MAX_JSONL_RECORD_BYTES = 1024 * 1024
MIN_MAX_JSONL_RECORD_BYTES = 512
MAX_REQUEST_ID = 0xFFFF_FFFF_FFFF_FFFF

MessageType = Literal["request", "response", "event", "error"]


@dataclass
class Envelope:
    message_type: MessageType
    request_id: int
    operation: str
    payload: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "operation": self.operation,
            "payload": self.payload,
        }


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RequestIdentity:
    request_id: int
    operation: str


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def decode_envelope(raw: bytes) -> Envelope:
    try:
        line = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid_utf8", str(exc)) from exc

    try:
        data = json.loads(line, parse_constant=reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("invalid_json", str(exc)) from exc

    if not isinstance(data, dict):
        raise ProtocolError("invalid_envelope", "envelope must be a JSON object")

    version = data.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol_version", "unsupported protocol_version")

    message_type = data.get("message_type")
    if message_type not in {"request", "response", "event", "error"}:
        raise ProtocolError("invalid_message_type", "invalid message_type")

    request_id = data.get("request_id")
    if type(request_id) is not int or request_id < 0 or request_id > MAX_REQUEST_ID:
        raise ProtocolError("invalid_request_id", "request_id must be an unsigned 64-bit integer")

    operation = data.get("operation")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("invalid_operation", "operation must be a non-empty string")

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_payload", "payload must be a JSON object")

    return Envelope(
        message_type=message_type,  # type: ignore[arg-type]
        request_id=request_id,
        operation=operation,
        payload=payload,
    )


def extract_request_identity(raw: bytes) -> RequestIdentity | None:
    try:
        data = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    request_id = data.get("request_id")
    operation = data.get("operation")
    if type(request_id) is not int or request_id <= 0 or request_id > MAX_REQUEST_ID:
        return None
    if not isinstance(operation, str) or not operation:
        return None
    return RequestIdentity(request_id=request_id, operation=operation)


def encode_envelope(envelope: Envelope, max_jsonl_bytes: int) -> bytes:
    try:
        line = json.dumps(
            envelope.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError as exc:
        raise ProtocolError("invalid_json_value", str(exc)) from exc
    raw = line.encode("utf-8", errors="strict") + b"\n"
    if len(raw) > max_jsonl_bytes:
        raise ProtocolError("jsonl_record_too_large", "outbound JSONL record is too large")
    return raw


def response(request: Envelope, payload: dict[str, Any]) -> Envelope:
    return Envelope(
        message_type="response",
        request_id=request.request_id,
        operation=request.operation,
        payload=payload,
    )


def event(request_id: int, operation: str, payload: dict[str, Any]) -> Envelope:
    return Envelope(
        message_type="event",
        request_id=request_id,
        operation=operation,
        payload=payload,
    )


def error(request: Envelope | None, code: str, message: str, fatal: bool = False) -> Envelope:
    return Envelope(
        message_type="error",
        request_id=request.request_id if request else 0,
        operation=request.operation if request else "protocol.error",
        payload={
            "code": code,
            "message": message,
            "fatal": fatal,
        },
    )


def error_for_identity(
    identity: RequestIdentity,
    code: str,
    message: str,
    fatal: bool = False,
) -> Envelope:
    return Envelope(
        message_type="error",
        request_id=identity.request_id,
        operation=identity.operation,
        payload={
            "code": code,
            "message": message,
            "fatal": fatal,
        },
    )
