from __future__ import annotations

import os
import unittest

from tg_client_stdio_worker.backend import ExportQuery
from tg_client_stdio_worker.cli import parse_proxy
from tg_client_stdio_worker.telethon_backend import TelethonBackend, TelethonBackendConfig


def _required_environment() -> dict[str, str]:
    names = {
        "api_id": "TG_CLIENT_STDIO_API_ID",
        "api_hash": "TG_CLIENT_STDIO_API_HASH",
        "session": "TG_CLIENT_STDIO_SESSION",
        "chat": "TG_CLIENT_STDIO_E2E_CHAT",
    }
    values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    missing = [name for key, name in names.items() if not values[key]]
    if missing:
        raise RuntimeError("missing authorized-session environment: " + ", ".join(missing))
    return values


@unittest.skipUnless(
    os.environ.get("TG_CLIENT_STDIO_E2E") == "1",
    "set TG_CLIENT_STDIO_E2E=1 to run against an authorized Telegram session",
)
class TelethonAuthorizedSessionE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        values = _required_environment()
        cls.chat = values["chat"]
        proxy_url = os.environ.get("TG_CLIENT_STDIO_PROXY", "").strip()
        cls.backend = TelethonBackend(
            TelethonBackendConfig(
                api_id=int(values["api_id"]),
                api_hash=values["api_hash"],
                session=values["session"],
                proxy=parse_proxy(proxy_url) if proxy_url else None,
            ),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        backend = getattr(cls, "backend", None)
        if backend is not None:
            backend.close()

    def test_authorized_session_lists_dialogs_and_exports_history(self) -> None:
        status = self.backend.auth_status()
        self.assertTrue(status["authorized"])
        self.assertFalse(status["password_required"])

        dialogs = list(self.backend.dialogs())
        self.assertTrue(dialogs)

        messages = list(self.backend.iter_export_messages(
            ExportQuery(chat=self.chat, limit=1)))
        self.assertLessEqual(len(messages), 1)
        if messages:
            self.assertTrue(messages[0].chat_id)
            self.assertGreater(messages[0].message_id, 0)


if __name__ == "__main__":
    unittest.main()
