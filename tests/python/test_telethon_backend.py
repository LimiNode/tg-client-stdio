from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tg_client_stdio_worker.backend import ExportQuery
from tg_client_stdio_worker.cli import main
from tg_client_stdio_worker.protocol import Envelope, encode_envelope
from tg_client_stdio_worker.server import JsonlWorkerServer
from tg_client_stdio_worker.telethon_backend import TelethonBackend, TelethonBackendConfig


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


@dataclass
class FakeEntity:
    id: int = 42
    peer_id: int = -10042
    title: str = "Signals"
    username: str = "signals"
    broadcast: bool = True
    megagroup: bool = False
    bot: bool = False


@dataclass
class FakeGroupEntity:
    id: int = 42
    title: str = "Plain Group"
    username: str = ""
    broadcast: bool = False
    megagroup: bool = False
    bot: bool = False


@dataclass
class FakeDialog:
    id: int
    name: str
    entity: FakeEntity


@dataclass
class FakeMessage:
    id: int
    date: datetime
    raw_text: str = "EURUSD BUY 5m"
    edit_date: datetime | None = None
    sender_id: int = 777
    reply_to_msg_id: int = 0
    grouped_id: str = ""


class FakeTelethonClient:
    last_instance: "FakeTelethonClient | None" = None

    def __init__(
            self,
            *args: Any,
            authorized: bool = True,
            connect_raises: bool = False,
            entity: Any | None = None,
            messages: list[FakeMessage] | None = None,
            **kwargs: Any) -> None:
        self.authorized = authorized
        self.connect_raises = connect_raises
        self.entity = entity or FakeEntity()
        self.connected = False
        self.disconnected = False
        self.messages = messages or []
        self.consumed_messages = 0
        self.iter_messages_kwargs: dict[str, Any] = {}
        FakeTelethonClient.last_instance = self

    def connect(self) -> None:
        if self.connect_raises:
            raise RuntimeError("connect failed")
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def is_user_authorized(self) -> bool:
        return self.authorized

    def iter_dialogs(self) -> list[FakeDialog]:
        return [FakeDialog(id=-10042, name="Signals", entity=self.entity)]

    def get_entity(self, chat: str) -> FakeEntity:
        entity = self.entity
        if hasattr(entity, "title"):
            entity.title = f"title:{chat}"
        return entity

    def iter_messages(self, chat: str, **kwargs: Any) -> Iterable[FakeMessage]:
        self.iter_messages_kwargs = kwargs
        offset_date = kwargs.get("offset_date")
        reverse = bool(kwargs.get("reverse", False))

        def consume() -> Any:
            for message in self.messages:
                if offset_date is not None:
                    if reverse and not (message.date > offset_date):
                        continue
                    if not reverse and not (message.date < offset_date):
                        continue
                self.consumed_messages += 1
                yield message

        return consume()


class TelethonBackendCliTest(unittest.TestCase):
    @unittest.skipIf(
        importlib.util.find_spec("telethon") is not None,
        "test only covers the missing optional dependency path",
    )
    def test_telethon_backend_reports_missing_optional_dependency_as_protocol_error(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session")
        )
        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(request(1, "dialogs.list")),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=backend,
        )

        self.assertEqual(server.run(), 1)
        record = json.loads(output_stream.getvalue().decode("utf-8"))
        self.assertEqual(record["message_type"], "error")
        self.assertEqual(record["payload"]["code"], "dependency_missing")
        self.assertTrue(record["payload"]["fatal"])

    def test_telethon_backend_requires_credentials(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--backend", "telethon"])

        self.assertEqual(result, 2)
        self.assertIn("--api-id", stderr.getvalue())

    def test_fake_client_exports_dialogs_and_messages(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=[FakeMessage(id=10, date=_dt(1784830000000))],
                **kwargs,
            ),
        )

        dialogs = [dialog.to_payload() for dialog in backend.dialogs()]
        messages = list(backend.iter_export_messages(ExportQuery(chat="-10042")))

        self.assertEqual(dialogs[0]["title"], "Signals")
        self.assertEqual(messages[0].chat_title, "title:-10042")
        self.assertEqual(messages[0].chat_id, "-10042")
        self.assertEqual(messages[0].message_id, 10)

    def test_export_identity_is_independent_of_query_form(self) -> None:
        messages = [FakeMessage(id=10, date=_dt(1784830000000))]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        by_username = list(backend.iter_export_messages(ExportQuery(chat="@signals")))
        by_plain_name = list(backend.iter_export_messages(ExportQuery(chat="signals")))
        by_numeric_id = list(backend.iter_export_messages(ExportQuery(chat="-10042")))

        self.assertEqual(by_username[0].message_identity, "telegram:-10042:0:10")
        self.assertEqual(by_plain_name[0].message_identity, by_username[0].message_identity)
        self.assertEqual(by_numeric_id[0].message_identity, by_username[0].message_identity)

    def test_export_normalizes_topic_id_identity(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=[FakeMessage(id=10, date=_dt(1784830000000))],
                **kwargs,
            ),
        )

        messages = list(backend.iter_export_messages(ExportQuery(chat="@signals", topic_id="042")))

        self.assertEqual(messages[0].topic_id, "42")
        self.assertEqual(messages[0].message_identity, "telegram:-10042:42:10")

    def test_zero_padded_topic_zero_does_not_set_reply_to(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=[FakeMessage(id=10, date=_dt(1784830000000))],
                **kwargs,
            ),
        )

        messages = list(backend.iter_export_messages(ExportQuery(chat="@signals", topic_id="000")))
        client = FakeTelethonClient.last_instance

        self.assertEqual(messages[0].topic_id, "0")
        assert client is not None
        self.assertNotIn("reply_to", client.iter_messages_kwargs)

    def test_plain_group_entity_uses_marked_negative_id(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                entity=FakeGroupEntity(),
                messages=[FakeMessage(id=10, date=_dt(1784830000000))],
                **kwargs,
            ),
        )

        messages = list(backend.iter_export_messages(ExportQuery(chat="plain-group")))

        self.assertEqual(messages[0].chat_id, "-42")
        self.assertEqual(messages[0].message_identity, "telegram:-42:0:10")

    def test_invalid_topic_id_is_nonfatal_export_error(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(*args, **kwargs),
        )
        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(request(1, "messages.export", {"chat": "-10042", "topic_id": "abc"})),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=backend,
        )

        self.assertEqual(server.run(), 0)
        records = [
            json.loads(line)
            for line in output_stream.getvalue().decode("utf-8").splitlines()
            if line
        ]
        self.assertEqual(records[-1]["message_type"], "error")
        self.assertEqual(records[-1]["payload"]["code"], "invalid_export_query")
        self.assertFalse(records[-1]["payload"]["fatal"])

    def test_include_media_is_rejected_until_supported(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(*args, **kwargs),
        )

        with self.assertRaises(Exception) as caught:
            list(backend.iter_export_messages(ExportQuery(chat="-10042", include_media=True)))

        self.assertIn("include_media", str(caught.exception))

    def test_newest_first_from_date_breaks_after_lower_bound(self) -> None:
        messages = [
            FakeMessage(id=3, date=_dt(3000)),
            FakeMessage(id=2, date=_dt(2000)),
            FakeMessage(id=1, date=_dt(1000)),
            FakeMessage(id=0, date=_dt(0)),
        ]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        exported = list(backend.iter_export_messages(
            ExportQuery(chat="-10042", from_date_ms=1500, order="newest_first")))
        client = FakeTelethonClient.last_instance

        self.assertEqual([message.message_id for message in exported], [3, 2])
        assert client is not None
        self.assertEqual(client.consumed_messages, 3)

    def test_oldest_first_to_date_breaks_after_upper_bound(self) -> None:
        messages = [
            FakeMessage(id=1, date=_dt(1000)),
            FakeMessage(id=2, date=_dt(2000)),
            FakeMessage(id=3, date=_dt(3000)),
            FakeMessage(id=4, date=_dt(4000)),
        ]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        exported = list(backend.iter_export_messages(
            ExportQuery(chat="-10042", to_date_ms=2500, order="oldest_first")))
        client = FakeTelethonClient.last_instance

        self.assertEqual([message.message_id for message in exported], [1, 2])
        assert client is not None
        self.assertEqual(client.consumed_messages, 3)

    def test_oldest_first_from_date_includes_boundary_message(self) -> None:
        messages = [
            FakeMessage(id=1, date=_dt(1000)),
            FakeMessage(id=2, date=_dt(2000)),
            FakeMessage(id=3, date=_dt(3000)),
        ]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        exported = list(backend.iter_export_messages(
            ExportQuery(chat="-10042", from_date_ms=2000, order="oldest_first")))

        self.assertEqual([message.message_id for message in exported], [2, 3])

    def test_newest_first_to_date_includes_boundary_message(self) -> None:
        messages = [
            FakeMessage(id=3, date=_dt(3000)),
            FakeMessage(id=2, date=_dt(2000)),
            FakeMessage(id=1, date=_dt(1000)),
        ]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        exported = list(backend.iter_export_messages(
            ExportQuery(chat="-10042", to_date_ms=2000, order="newest_first")))

        self.assertEqual([message.message_id for message in exported], [2, 1])
        client = FakeTelethonClient.last_instance
        assert client is not None
        self.assertEqual(client.iter_messages_kwargs["offset_date"], _dt(3000))

    def test_rejects_out_of_range_telegram_timestamp(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(*args, **kwargs),
        )

        with self.assertRaises(Exception) as caught:
            list(backend.iter_export_messages(
                ExportQuery(
                    chat="-10042",
                    to_date_ms=0x7FFF_FFFF_000 + 1000,
                    order="newest_first",
                )))

        self.assertIn("outside Telegram timestamp range", str(caught.exception))

    def test_max_telegram_to_date_uses_local_filter_without_offset(self) -> None:
        max_ms = 0x7FFF_FFFF * 1000
        messages = [
            FakeMessage(id=2, date=_dt(max_ms)),
            FakeMessage(id=1, date=_dt(max_ms - 1000)),
        ]
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                messages=messages,
                **kwargs,
            ),
        )

        exported = list(backend.iter_export_messages(
            ExportQuery(chat="-10042", to_date_ms=max_ms, order="newest_first")))
        client = FakeTelethonClient.last_instance

        self.assertEqual([message.message_id for message in exported], [2, 1])
        assert client is not None
        self.assertNotIn("offset_date", client.iter_messages_kwargs)

    def test_connect_failure_does_not_poison_cached_client(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(
                *args,
                connect_raises=True,
                **kwargs,
            ),
        )

        with self.assertRaises(Exception) as caught:
            list(backend.dialogs())

        client = FakeTelethonClient.last_instance
        self.assertIn("connect failed", str(caught.exception))
        self.assertIsNone(backend._client)
        assert client is not None
        self.assertTrue(client.disconnected)

    def test_server_shutdown_closes_backend(self) -> None:
        backend = TelethonBackend(
            TelethonBackendConfig(api_id=1, api_hash="hash", session="session"),
            telegram_client_factory=lambda *args, **kwargs: FakeTelethonClient(*args, **kwargs),
        )
        output_stream = io.BytesIO()
        server = JsonlWorkerServer(
            input_stream=io.BytesIO(
                request(1, "dialogs.list") +
                request(2, "shutdown")
            ),
            output_stream=output_stream,
            error_stream=io.StringIO(),
            backend=backend,
        )

        self.assertEqual(server.run(), 0)
        client = FakeTelethonClient.last_instance
        assert client is not None
        self.assertTrue(client.disconnected)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


if __name__ == "__main__":
    unittest.main()
