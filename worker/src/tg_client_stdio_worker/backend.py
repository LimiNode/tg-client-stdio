from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class Dialog:
    chat_id: str
    title: str
    username: str = ""
    kind: str = "channel"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExportQuery:
    chat: str = "-1001234567890"
    topic_id: str = "0"
    from_date_ms: int | None = None
    to_date_ms: int | None = None
    limit: int | None = None
    order: str = "oldest_first"
    include_media: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ExportQuery":
        chat = str(payload.get("chat", cls.chat))
        topic_id = str(payload.get("topic_id", cls.topic_id))
        from_date_ms = _optional_int(payload.get("from_date_ms"), "from_date_ms")
        to_date_ms = _optional_int(payload.get("to_date_ms"), "to_date_ms")
        limit = _optional_int(payload.get("limit"), "limit")
        order = str(payload.get("order", cls.order))
        include_media = bool(payload.get("include_media", cls.include_media))

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if order not in {"oldest_first", "newest_first"}:
            raise ValueError("order must be oldest_first or newest_first")

        return cls(
            chat=chat,
            topic_id=topic_id,
            from_date_ms=from_date_ms,
            to_date_ms=to_date_ms,
            limit=limit,
            order=order,
            include_media=include_media,
        )


@dataclass(frozen=True)
class RawMessage:
    chat_id: str
    chat_title: str
    topic_id: str
    message_id: int
    date_ms: int
    edit_date_ms: int = 0
    sender_id: str = ""
    reply_to_message_id: int = 0
    grouped_id: str = ""
    text: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)

    @property
    def message_identity(self) -> str:
        return f"telegram:{self.chat_id}:{self.topic_id or '0'}:{self.message_id}"

    @property
    def revision_identity(self) -> str:
        return f"{self.message_identity}:{self.edit_date_ms or 0}"

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["message_identity"] = self.message_identity
        data["revision_identity"] = self.revision_identity
        return data


class TelegramBackend(Protocol):
    def dialogs(self) -> Iterable[Dialog]:
        ...

    def iter_export_messages(self, query: ExportQuery) -> Iterable[RawMessage]:
        ...


class MockTelegramBackend:
    """Deterministic backend used by protocol tests and early host integration."""

    def dialogs(self) -> Iterable[Dialog]:
        yield Dialog(
            chat_id="-1001234567890",
            title="Signals",
            username="signals",
            kind="channel",
        )

    def iter_export_messages(self, query: ExportQuery) -> Iterable[RawMessage]:
        yield RawMessage(
            chat_id=query.chat,
            chat_title="Signals",
            topic_id=query.topic_id,
            message_id=1234,
            date_ms=1784830000000,
            edit_date_ms=0,
            sender_id="777",
            reply_to_message_id=0,
            grouped_id="",
            text="EURUSD BUY 5m",
            media=[],
        )
        yield RawMessage(
            chat_id=query.chat,
            chat_title="Signals",
            topic_id=query.topic_id,
            message_id=1235,
            date_ms=1784830300000,
            edit_date_ms=0,
            sender_id="777",
            reply_to_message_id=1234,
            grouped_id="",
            text="WIN EURUSD",
            media=[],
        )


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value
