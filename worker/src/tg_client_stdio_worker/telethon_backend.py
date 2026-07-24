from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .backend import BackendError, Dialog, ExportQuery, RawMessage

_TELEGRAM_INT32_MIN = -0x8000_0000
_TELEGRAM_INT32_MAX = 0x7FFF_FFFF


@dataclass(frozen=True)
class TelethonBackendConfig:
    api_id: int
    api_hash: str
    session: str
    proxy: Any = None


class TelethonBackend:
    """Telethon-backed user-client adapter.

    This first implementation intentionally requires a pre-authorized Telethon
    session. Interactive login cannot use stdin because stdin is reserved for
    JSONL protocol records.
    """

    def __init__(
            self,
            config: TelethonBackendConfig,
            telegram_client_factory: Callable[..., Any] | None = None) -> None:
        self._config = config
        self._client: Any | None = None
        self._telegram_client_factory = telegram_client_factory

    def dialogs(self) -> Iterable[Dialog]:
        try:
            client = self._authorized_client()
            dialogs = client.iter_dialogs()
            for dialog in dialogs:
                entity = dialog.entity
                yield Dialog(
                    chat_id=str(dialog.id),
                    title=str(dialog.name or ""),
                    username=str(getattr(entity, "username", "") or ""),
                    kind=_dialog_kind(entity),
                )
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_backend_error", str(exc)) from exc

    def iter_export_messages(self, query: ExportQuery) -> Iterable[RawMessage]:
        if query.include_media:
            raise BackendError(
                "unsupported_export_query",
                "include_media is not supported by the Telethon backend scaffold",
            )

        reply_to = _topic_id_to_reply_to(query.topic_id)
        canonical_topic_id = str(reply_to or 0)
        reverse = query.order == "oldest_first"
        kwargs: dict[str, Any] = {
            "limit": None if query.from_date_ms is not None or query.to_date_ms is not None else query.limit,
            "reverse": reverse,
        }
        offset_date = _offset_date(query)
        if offset_date is not None:
            kwargs["offset_date"] = offset_date
        if reply_to is not None:
            kwargs["reply_to"] = reply_to

        emitted = 0
        try:
            client = self._authorized_client()
            entity = client.get_entity(query.chat)
            canonical_chat_id = _canonical_peer_id(entity)
            chat_title = _entity_title(entity, query.chat)
            for message in client.iter_messages(query.chat, **kwargs):
                date_ms = _datetime_to_ms(message.date)
                if _past_requested_range(query, date_ms):
                    break
                if query.from_date_ms is not None and date_ms < query.from_date_ms:
                    continue
                if query.to_date_ms is not None and date_ms > query.to_date_ms:
                    continue
                if query.limit is not None and emitted >= query.limit:
                    break
                emitted += 1
                yield RawMessage(
                    chat_id=canonical_chat_id,
                    chat_title=chat_title,
                    topic_id=canonical_topic_id,
                    message_id=int(message.id),
                    date_ms=date_ms,
                    edit_date_ms=_datetime_to_ms(getattr(message, "edit_date", None)),
                    sender_id=str(getattr(message, "sender_id", "") or ""),
                    reply_to_message_id=int(getattr(message, "reply_to_msg_id", 0) or 0),
                    grouped_id=str(getattr(message, "grouped_id", "") or ""),
                    text=str(getattr(message, "raw_text", "") or ""),
                    media=[],
                )
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_backend_error", str(exc)) from exc

    def _authorized_client(self) -> Any:
        if self._client is None:
            factory = self._telegram_client_factory or _load_telegram_client_factory()
            client = factory(
                self._config.session,
                self._config.api_id,
                self._config.api_hash,
                proxy=self._config.proxy,
            )
            try:
                client.connect()
                if not client.is_user_authorized():
                    raise BackendError(
                        "authorization_required",
                        "Telethon session is not authorized; authorize it outside JSONL stdio first",
                    )
            except BackendError:
                _disconnect_best_effort(client)
                raise
            except Exception as exc:
                _disconnect_best_effort(client)
                raise BackendError("telegram_backend_error", str(exc)) from exc
            self._client = client
        if not self._client.is_user_authorized():
            raise BackendError(
                "authorization_required",
                "Telethon session is not authorized; authorize it outside JSONL stdio first",
            )
        return self._client

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            _disconnect_best_effort(client)


def _load_telegram_client_factory() -> Callable[..., Any]:
    try:
        from telethon.sync import TelegramClient  # type: ignore
    except ImportError as exc:
        raise BackendError(
            "dependency_missing",
            "install tg-client-stdio-worker[telegram] to use the Telethon backend",
            fatal=True,
        ) from exc
    return TelegramClient


def _topic_id_to_reply_to(topic_id: str) -> int | None:
    if not topic_id or topic_id == "0":
        return None
    if not topic_id.isdecimal():
        raise BackendError("invalid_export_query", "topic_id must be a decimal integer")
    numeric = int(topic_id)
    return numeric if numeric > 0 else None


def _offset_date(query: ExportQuery) -> datetime | None:
    if query.order == "newest_first" and query.to_date_ms is not None:
        offset_seconds = query.to_date_ms // 1000 + 1
        if offset_seconds > _TELEGRAM_INT32_MAX:
            _checked_telegram_seconds(query.to_date_ms // 1000, "to_date_ms")
            return None
        return _seconds_to_datetime(_checked_telegram_seconds(offset_seconds, "to_date_ms"))
    if query.order == "oldest_first" and query.from_date_ms is not None:
        return _seconds_to_datetime(_checked_telegram_seconds(
            query.from_date_ms // 1000 - 1,
            "from_date_ms",
        ))
    return None


def _past_requested_range(query: ExportQuery, date_ms: int) -> bool:
    if query.order == "newest_first":
        return query.from_date_ms is not None and date_ms < query.from_date_ms
    return query.to_date_ms is not None and date_ms > query.to_date_ms


def _canonical_peer_id(entity: Any) -> str:
    try:
        from telethon import utils as telethon_utils  # type: ignore
    except ImportError:
        telethon_utils = None
    if telethon_utils is not None:
        try:
            return str(telethon_utils.get_peer_id(entity))
        except Exception:
            pass

    peer_id = getattr(entity, "peer_id", None)
    if peer_id is not None:
        return str(int(peer_id))
    entity_id = getattr(entity, "id", None)
    if entity_id is None:
        raise BackendError("telegram_backend_error", "resolved Telegram entity has no id")
    numeric = int(entity_id)
    if numeric < 0:
        return str(numeric)
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        return f"-100{numeric}"
    if _is_group_chat_entity(entity):
        return str(-numeric)
    return str(numeric)


def _entity_title(entity: Any, fallback: str) -> str:
    return str(
        getattr(entity, "title", None) or
        getattr(entity, "first_name", None) or
        getattr(entity, "username", None) or
        fallback)


def _disconnect_best_effort(client: Any) -> None:
    try:
        client.disconnect()
    except AttributeError:
        return
    except Exception:
        return


def _dialog_kind(entity: Any) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "group"
    if getattr(entity, "bot", False):
        return "bot"
    return "chat"


def _datetime_to_ms(value: datetime | None) -> int:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _ms_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _seconds_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _checked_telegram_seconds(value: int, name: str) -> int:
    if value < _TELEGRAM_INT32_MIN or value > _TELEGRAM_INT32_MAX:
        raise BackendError(
            "invalid_export_query",
            f"{name} is outside Telegram timestamp range",
        )
    return value


def _is_group_chat_entity(entity: Any) -> bool:
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        return False
    if getattr(entity, "bot", False):
        return False
    username = getattr(entity, "username", None)
    if username:
        return False
    return hasattr(entity, "title")
