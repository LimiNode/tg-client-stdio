from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Full, Queue
from threading import Event, Lock, Thread, current_thread
from typing import Any, Callable, Iterable

from .backend import BackendError, Dialog, ExportQuery, LiveQuery, RawMessage

_TELEGRAM_INT32_MIN = -0x8000_0000
_TELEGRAM_INT32_MAX = 0x7FFF_FFFF


@dataclass(frozen=True)
class TelethonBackendConfig:
    api_id: int
    api_hash: str
    session: str
    proxy: Any = None
    live_poll_interval_seconds: float = 1.0
    phone: str = ""


@dataclass
class _CallTask:
    operation: Callable[[Any], Any]
    result: Future[Any]


@dataclass
class _StreamTask:
    operation: Callable[[Any, Callable[[Any], None]], None]
    output: Queue[Any]
    cancel: Event


@dataclass(frozen=True)
class _StreamDone:
    pass


@dataclass(frozen=True)
class _StreamFailure:
    error: BaseException


class _StreamCancelled(Exception):
    pass


class _TelethonRuntime:
    """Owns one Telethon client and its asyncio loop in one thread.

    The backend API is synchronous because the JSONL server is synchronous,
    but the Telethon object never crosses this thread boundary. The sync
    Telethon facade therefore always finds the loop it was created with.
    """

    def __init__(
            self,
            config: TelethonBackendConfig,
            factory: Callable[..., Any] | None) -> None:
        self._config = config
        self._factory = factory
        self._tasks: Queue[_CallTask | _StreamTask | None] = Queue()
        self._thread: Thread | None = None
        self._ready = Event()
        self._start_lock = Lock()
        self._state_lock = Lock()
        self._startup_error: BaseException | None = None
        self._client_active = False

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="tg-client-stdio-telethon",
                    daemon=True,
                )
                self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ready.set()
        client: Any | None = None
        try:
            while True:
                task = self._tasks.get()
                if task is None:
                    break
                if isinstance(task, _CallTask):
                    try:
                        if client is None:
                            client = self._create_client()
                        task.result.set_result(task.operation(client))
                    except BaseException as exc:
                        task.result.set_exception(exc)
                    continue

                try:
                    if client is None:
                        client = self._create_client()

                    def emit(value: Any) -> None:
                        while not task.cancel.is_set():
                            try:
                                task.output.put(value, timeout=0.1)
                                return
                            except Full:
                                continue
                        raise _StreamCancelled()

                    task.operation(client, emit)
                except _StreamCancelled:
                    pass
                except BaseException as exc:
                    self._put_stream_marker(task, _StreamFailure(exc))
                finally:
                    self._put_stream_marker(task, _StreamDone())
        finally:
            if client is not None:
                _disconnect_best_effort(client)
            with self._state_lock:
                self._client_active = False
            loop.close()

    @staticmethod
    def _put_stream_marker(task: _StreamTask, marker: Any) -> None:
        while not task.cancel.is_set():
            try:
                task.output.put(marker, timeout=0.1)
                return
            except Full:
                continue

    def _create_client(self) -> Any:
        client: Any | None = None
        try:
            factory = self._factory or _load_telegram_client_factory()
            client = factory(
                self._config.session,
                self._config.api_id,
                self._config.api_hash,
                proxy=self._config.proxy,
            )
            client.connect()
        except BackendError:
            if client is not None:
                _disconnect_best_effort(client)
            raise
        except Exception as exc:
            if client is not None:
                _disconnect_best_effort(client)
            raise BackendError("telegram_backend_error", str(exc)) from exc
        with self._state_lock:
            self._client_active = True
        return client

    def call(self, operation: Callable[[Any], Any]) -> Any:
        self._ensure_started()
        result: Future[Any] = Future()
        self._tasks.put(_CallTask(operation, result))
        return result.result()

    def stream(
            self,
            operation: Callable[[Any, Callable[[Any], None]], None]) -> Iterable[Any]:
        self._ensure_started()
        output: Queue[Any] = Queue(maxsize=128)
        cancel = Event()
        self._tasks.put(_StreamTask(operation, output, cancel))
        try:
            while True:
                value = output.get()
                if isinstance(value, _StreamDone):
                    return
                if isinstance(value, _StreamFailure):
                    raise value.error
                yield value
        finally:
            cancel.set()

    def has_client(self) -> bool:
        with self._state_lock:
            return self._client_active

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._tasks.put(None)
        if thread is not current_thread():
            thread.join()
        self._thread = None
        self._ready.clear()
        self._startup_error = None


class TelethonBackend:
    """Telethon-backed user-client adapter with one owner thread per client."""

    def __init__(
            self,
            config: TelethonBackendConfig,
            telegram_client_factory: Callable[..., Any] | None = None,
            forum_topic_resolver: Callable[..., Any] | None = None) -> None:
        self._config = config
        self._forum_topic_resolver = forum_topic_resolver
        if config.live_poll_interval_seconds <= 0:
            raise ValueError("live_poll_interval_seconds must be positive")
        self._runtime = _TelethonRuntime(
            config,
            telegram_client_factory,
        )
        self._live_lock = Lock()
        self._live_stop: Event | None = None
        self._live_thread: Thread | None = None
        self._live_generation = 0
        self._auth_phone = config.phone
        self._phone_code_hash: str | None = None
        self._password_required = False

    def dialogs(self) -> Iterable[Dialog]:
        try:
            values = self._runtime.call(self._dialogs_on_runtime)
            yield from values
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_backend_error", str(exc)) from exc

    def _dialogs_on_runtime(self, client: Any) -> list[Dialog]:
        client = self._require_authorized(client)
        return [
            Dialog(
                chat_id=str(dialog.id),
                title=str(dialog.name or ""),
                username=str(getattr(dialog.entity, "username", "") or ""),
                kind=_dialog_kind(dialog.entity),
            )
            for dialog in client.iter_dialogs()
        ]

    def iter_export_messages(self, query: ExportQuery) -> Iterable[RawMessage]:
        if query.include_media:
            raise BackendError(
                "unsupported_export_query",
                "include_media is not supported by the Telethon backend scaffold",
            )

        reply_to = _topic_id_to_reply_to(query.topic_id)
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

        def export_on_runtime(
                client: Any,
                emit: Callable[[RawMessage], None]) -> None:
            self._export_on_runtime(client, query, reply_to, reverse, kwargs, emit)

        try:
            yield from self._runtime.stream(export_on_runtime)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_backend_error", str(exc)) from exc

    def _export_on_runtime(
            self,
            client: Any,
            query: ExportQuery,
            reply_to: int | None,
            reverse: bool,
            kwargs: dict[str, Any],
            emit: Callable[[RawMessage], None]) -> None:
        client = self._require_authorized(client)
        chat_ref = _telethon_chat_ref(query.chat)
        entity = client.get_entity(chat_ref)
        input_entity = client.get_input_entity(entity)
        is_forum = bool(getattr(entity, "forum", False))
        if reply_to is not None and not is_forum:
            raise BackendError(
                "invalid_export_query",
                "topic_id requires a Telegram forum entity",
            )
        if reply_to == 1:
            raise BackendError(
                "unsupported_export_query",
                "exporting the General forum topic is not supported",
            )
        if reply_to is not None:
            resolved_topic = self._resolve_forum_topic(client, input_entity, reply_to)
            if resolved_topic is None:
                raise BackendError(
                    "invalid_export_query",
                    "topic_id does not identify a Telegram forum topic",
                )
        canonical_chat_id = _canonical_peer_id(entity)
        chat_title = _entity_title(entity, query.chat)
        messages = client.iter_messages(input_entity, **kwargs)
        if reply_to is not None:
            root_message = client.get_messages(input_entity, ids=reply_to)
            if root_message is None:
                raise BackendError(
                    "invalid_export_query",
                    "forum topic root was not found",
                )
            messages = _merge_topic_root(root_message, messages, oldest_first=reverse)

        emitted = 0
        for message in messages:
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
            emit(RawMessage(
                chat_id=canonical_chat_id,
                chat_title=chat_title,
                topic_id=_message_topic_id(message, reply_to, is_forum=is_forum),
                message_id=int(message.id),
                date_ms=date_ms,
                edit_date_ms=_datetime_to_ms(getattr(message, "edit_date", None)),
                sender_id=str(getattr(message, "sender_id", "") or ""),
                reply_to_message_id=int(getattr(message, "reply_to_msg_id", 0) or 0),
                grouped_id=str(getattr(message, "grouped_id", "") or ""),
                text=str(getattr(message, "raw_text", "") or ""),
                media=[],
            ))

    def start_listening(
            self,
            query: LiveQuery,
            on_message: Callable[[RawMessage], None],
            on_error: Callable[[BackendError], None]) -> None:
        with self._live_lock:
            if self._live_thread is not None:
                raise BackendError("listen_already_active", "a live listener is already active")
            try:
                watches = self._runtime.call(
                    lambda client: self._prepare_live_watches(client, query))
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError("telegram_backend_error", str(exc)) from exc

            stop = Event()
            self._live_generation += 1
            generation = self._live_generation
            thread = Thread(
                target=self._poll_live,
                args=(generation, watches, query, stop, on_message, on_error),
                name="tg-client-stdio-live",
                daemon=True,
            )
            self._live_stop = stop
            self._live_thread = thread
            thread.start()

    def stop_listening(self) -> None:
        with self._live_lock:
            stop = self._live_stop
            thread = self._live_thread
            self._live_stop = None
            self._live_thread = None
        if stop is None or thread is None:
            return
        stop.set()
        if thread is not current_thread():
            thread.join()

    def auth_status(self) -> dict[str, Any]:
        try:
            authorized = self._runtime.call(
                lambda client: bool(self._connected_client(client).is_user_authorized()))
            return {
                "authorized": authorized,
                "password_required": bool(self._password_required and not authorized),
            }
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_auth_error", str(exc)) from exc

    def auth_send_code(self, phone: str) -> dict[str, Any]:
        normalized_phone = phone.strip() or self._auth_phone.strip()
        if not normalized_phone:
            raise BackendError("invalid_auth_request", "phone must not be empty")
        try:
            result = self._runtime.call(
                lambda client: self._connected_client(client).send_code_request(normalized_phone))
            self._auth_phone = normalized_phone
            self._phone_code_hash = str(getattr(result, "phone_code_hash", "") or "")
            self._password_required = False
            return {"authorized": False, "code_sent": True, "password_required": False}
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("telegram_auth_error", str(exc)) from exc

    def auth_submit_code(self, code: str) -> dict[str, Any]:
        if not code.strip():
            raise BackendError("invalid_auth_request", "code must not be empty")
        if not self._auth_phone:
            raise BackendError("invalid_auth_request", "send auth code before submitting it")
        try:
            kwargs: dict[str, Any] = {"phone": self._auth_phone, "code": code.strip()}
            if self._phone_code_hash:
                kwargs["phone_code_hash"] = self._phone_code_hash
            self._runtime.call(
                lambda client: self._connected_client(client).sign_in(**kwargs))
            self._password_required = False
            return self.auth_status()
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                self._password_required = True
                return {"authorized": False, "code_sent": False, "password_required": True}
            if isinstance(exc, BackendError):
                raise
            raise BackendError("telegram_auth_error", str(exc)) from exc

    def auth_submit_password(self, password: str) -> dict[str, Any]:
        if not password:
            raise BackendError("invalid_auth_request", "password must not be empty")
        try:
            self._runtime.call(
                lambda client: self._connected_client(client).sign_in(password=password))
            self._password_required = False
            return self.auth_status()
        except Exception as exc:
            if isinstance(exc, BackendError):
                raise
            raise BackendError("telegram_auth_error", str(exc)) from exc

    def _prepare_live_watches(
            self,
            client: Any,
            query: LiveQuery) -> list[tuple[Any, str, str, int, bool]]:
        client = self._require_authorized(client)
        watches: list[tuple[Any, str, str, int, bool]] = []
        for chat in query.chats:
            entity = client.get_entity(_telethon_chat_ref(chat))
            input_entity = client.get_input_entity(entity)
            canonical_chat_id = _canonical_peer_id(entity)
            chat_title = _entity_title(entity, chat)
            latest = next(iter(client.iter_messages(input_entity, limit=1)), None)
            last_message_id = int(getattr(latest, "id", 0) or 0)
            watches.append((
                input_entity,
                canonical_chat_id,
                chat_title,
                last_message_id,
                bool(getattr(entity, "forum", False)),
            ))
        return watches

    def _poll_live(
            self,
            generation: int,
            watches: list[tuple[Any, str, str, int, bool]],
            query: LiveQuery,
            stop: Event,
            on_message: Callable[[RawMessage], None],
            on_error: Callable[[BackendError], None]) -> None:
        failure: BackendError | None = None
        try:
            while not stop.is_set():
                for index, (
                        input_entity,
                        chat_id,
                        chat_title,
                        last_message_id,
                        is_forum) in enumerate(watches):
                    messages = self._runtime.call(lambda client: list(client.iter_messages(
                        input_entity,
                        limit=100,
                        min_id=last_message_id,
                        reverse=True,
                    )))
                    new_messages = [
                        message for message in messages
                        if int(getattr(message, "id", 0) or 0) > last_message_id
                    ]
                    if not new_messages:
                        continue
                    new_messages.sort(key=lambda message: int(message.id))
                    watches[index] = (
                        input_entity,
                        chat_id,
                        chat_title,
                        max(int(message.id) for message in new_messages),
                        is_forum,
                    )
                    for message in new_messages:
                        topic_id = _message_topic_id(message, None, is_forum=is_forum)
                        if query.topic_ids and topic_id not in query.topic_ids:
                            continue
                        on_message(_raw_message(
                            message,
                            chat_id=chat_id,
                            chat_title=chat_title,
                            topic_id=topic_id,
                        ))
                stop.wait(self._config.live_poll_interval_seconds)
        except Exception as exc:
            failure = BackendError("telegram_live_error", str(exc), fatal=False)
        finally:
            self._clear_live_generation(generation)
        if failure is not None and not stop.is_set():
            on_error(failure)

    def _clear_live_generation(self, generation: int) -> None:
        with self._live_lock:
            if (self._live_generation == generation and
                    self._live_thread is current_thread()):
                self._live_thread = None
                self._live_stop = None

    def _require_authorized(self, client: Any) -> Any:
        if not client.is_user_authorized():
            raise BackendError(
                "authorization_required",
                "Telethon session is not authorized; authorize it through auth operations first",
            )
        return client

    @staticmethod
    def _connected_client(client: Any) -> Any:
        return client

    def _resolve_forum_topic(
            self,
            client: Any,
            input_entity: Any,
            topic_id: int) -> Any | None:
        if self._forum_topic_resolver is not None:
            resolved = self._forum_topic_resolver(client, input_entity, topic_id)
            return resolved if _is_forum_topic_record(resolved, topic_id) else None

        try:
            from telethon import functions  # type: ignore
        except ImportError as exc:
            raise BackendError(
                "dependency_missing",
                "install tg-client-stdio-worker[telegram] to resolve forum topics",
                fatal=True,
            ) from exc

        result = client(functions.messages.GetForumTopicsByIDRequest(
            peer=input_entity,
            topics=[topic_id],
        ))
        for topic in getattr(result, "topics", ()) or ():
            if _is_forum_topic_record(topic, topic_id):
                return topic
        return None

    def close(self) -> None:
        self.stop_listening()
        self._runtime.close()


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


def _raw_message(
        message: Any,
        *,
        chat_id: str,
        chat_title: str,
        topic_id: str) -> RawMessage:
    return RawMessage(
        chat_id=chat_id,
        chat_title=chat_title,
        topic_id=topic_id,
        message_id=int(message.id),
        date_ms=_datetime_to_ms(message.date),
        edit_date_ms=_datetime_to_ms(getattr(message, "edit_date", None)),
        sender_id=str(getattr(message, "sender_id", "") or ""),
        reply_to_message_id=int(getattr(message, "reply_to_msg_id", 0) or 0),
        grouped_id=str(getattr(message, "grouped_id", "") or ""),
        text=str(getattr(message, "raw_text", "") or ""),
        media=[],
    )


def _is_forum_topic_record(topic: Any, topic_id: int) -> bool:
    return (
        topic is not None
        and getattr(topic, "id", None) == topic_id
        and getattr(topic, "top_message", None) is not None
    )


def _telethon_chat_ref(value: str) -> str | int:
    if value.lstrip("-").isdecimal():
        return int(value)
    return value


def _merge_topic_root(
        root_message: Any,
        replies: Iterable[Any],
        *,
        oldest_first: bool) -> Iterable[Any]:
    root_id = getattr(root_message, "id", None) if root_message is not None else None

    def ordered() -> Iterable[Any]:
        if oldest_first and root_message is not None:
            yield root_message
        for message in replies:
            if root_id is not None and getattr(message, "id", None) == root_id:
                continue
            yield message
        if not oldest_first and root_message is not None:
            yield root_message

    return ordered()


def _message_topic_id(
        message: Any,
        topic_root_id: int | None = None,
        *,
        is_forum: bool = False) -> str:
    if topic_root_id is not None and getattr(message, "id", None) == topic_root_id:
        return str(topic_root_id)
    if not is_forum:
        return "0"
    reply_header = getattr(message, "reply_to", None)
    top_id = getattr(reply_header, "reply_to_top_id", None)
    if top_id is None:
        top_id = getattr(message, "reply_to_top_id", None)
    if isinstance(top_id, int) and top_id > 0:
        return str(top_id)
    action = getattr(message, "action", None)
    if type(action).__name__ == "MessageActionTopicCreate":
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int) and message_id > 0:
            return str(message_id)
    if topic_root_id is not None:
        return str(topic_root_id)
    return "0"


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
