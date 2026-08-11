# tg-client-stdio

Telegram user-client worker over JSONL stdio for dialogs, history export, and
live message events.

`tg-client-stdio` is not a Telegram Bot API wrapper. It is designed around a
Telegram user-client session, implemented by a sidecar worker process and
supervised by a host application over stdin/stdout.

## Status

Early scaffold. The repository currently contains:

- protocol v1 design and envelope rules;
- a Python mock worker that speaks the JSONL protocol;
- an optional Telethon-backed worker backend for pre-authorized sessions;
- C++17 header-only envelope and process-supervisor helpers;
- tests for the mock worker and C++ protocol helper.

The mock backend exists so host applications can build and test the stdio
contract without Telegram credentials. The Telethon backend already exposes
the authorization lifecycle through the protocol; an authorized-session E2E
check is kept separate because it requires operator-provided Telegram access.

## Repository Layout

```text
docs/                    Protocol and architecture notes.
include/tg_client_stdio/ C++ protocol/client-facing headers.
worker/src/              Python worker package.
tests/                   Python and C++ protocol tests.
```

## Protocol

See `docs/protocol-v1.md`.

Every stdout line is protocol JSON. Logs and diagnostics must go to stderr.
Large history exports are streamed:

```text
messages.export request
  -> export.started event
  -> export.message event x N
  -> messages.export response
```

## Python Mock Worker

Install the worker package, then run a local mock worker:

```powershell
python -m pip install -e .
tg-client-stdio-worker --mock
```

The Telethon backend is optional and requires a pre-authorized session:

```powershell
python -m pip install -e ".[telegram]"
tg-client-stdio-worker --backend telethon --api-id 123 --api-hash ... --session ./session
```

The Telegram client and its asyncio loop are owned by one dedicated worker
thread. Host requests are serialized through that owner; the Telethon client
must not be used directly from host or callback threads. The `telegram` extra
also installs `PySocks`, which is required for `socks5://` and `socks5h://`
proxy URLs.

The worker also exposes `auth.status`, `auth.send_code`,
`auth.submit_code`, and `auth.submit_password` over JSONL. This keeps stdin
reserved for protocol records while allowing a host application to own the
login UI. Proxy URLs can be supplied with `--proxy` using `http://`,
`socks5://`, or `socks5h://` schemes.

For a one-time interactive session setup, use the helper script. It stores the
Telethon session locally and prints only a non-secret status record:

```powershell
python scripts/authorize_telegram_session.py `
  --api-id 123 --api-hash ... --session .\sessions\account-a `
  --phone +10000000000 --proxy socks5://127.0.0.1:1080
```

Proxy credentials belong in the URL and must be percent-encoded. For example,
`p@ss:word` becomes `p%40ss%3Aword`:

```powershell
$env:TG_CLIENT_STDIO_PROXY = "socks5://proxy-user:p%40ss%3Aword@127.0.0.1:1080"
python scripts/authorize_telegram_session.py `
  --api-id 123 --api-hash ... --session .\sessions\account-a
```

Using the environment variable avoids exposing the proxy password in shell
history and process arguments. The parser decodes the username and password
before passing them to PySocks.

The script asks for the login code and, when enabled, the 2FA password without
echoing the password. Use a separate session path and preferably a separate
proxy per account when operating multiple accounts.

Interactive Telegram login is intentionally outside JSONL stdio; stdin is
reserved for protocol records.

An opt-in authorized-session smoke test is available for operator validation.
It never runs in normal CI unless explicitly enabled:

```powershell
$env:TG_CLIENT_STDIO_E2E = "1"
$env:TG_CLIENT_STDIO_API_ID = "123456"
$env:TG_CLIENT_STDIO_API_HASH = "..."
$env:TG_CLIENT_STDIO_SESSION = "C:\path\to\session"
$env:TG_CLIENT_STDIO_E2E_CHAT = "@signal_channel"
$env:TG_CLIENT_STDIO_PROXY = "socks5://127.0.0.1:1080"
python -m unittest discover -s tests/python -p test_telethon_authorized_e2e.py
```

The test checks the existing authorization state, lists dialogs, and exports
at most one message from the configured chat. It does not print credentials or
perform interactive login. Clear these environment variables after the run.

## Operator CLI

After installing the package, `scripts/tg_client_cli.py` provides small
operator-facing wrappers around the existing worker operations. It uses the
same `TG_CLIENT_STDIO_API_ID`, `TG_CLIENT_STDIO_API_HASH`,
`TG_CLIENT_STDIO_SESSION`, and `TG_CLIENT_STDIO_PROXY` environment variables
as the authorization helper.

List all dialogs as a table, or search by chat id, title, username, or kind:

```powershell
python scripts/tg_client_cli.py dialogs
python scripts/tg_client_cli.py dialogs --search "MONEY BOT"
python scripts/tg_client_cli.py dialogs --search "MONEY BOT" --json
```

Export history without accumulating it in memory. stdout contains only one
raw-message JSON object per line; the summary is written to stderr. Use `-` for
stdout or `--output` for a file:

```powershell
python scripts/tg_client_cli.py export `
  --chat "Сигналы MONEY BOT" `
  --from 2026-08-01 `
  --to 2026-08-10 `
  --order oldest_first `
  --output .\exports\money-bot.jsonl
```

`--from` and `--to` accept UTC milliseconds or ISO-8601. A date-only value
covers the complete UTC day. `--limit` bounds the number of messages, and
`--topic-id` selects a forum topic when supported by the worker.

Example request:

```json
{"protocol_version":1,"message_type":"request","request_id":1,"operation":"hello","payload":{}}
```

## Python Host Client

The package also includes a small host-side helper for code that already owns a
worker process and has connected binary stdin/stdout streams:

```python
from tg_client_stdio_worker.client import JsonlWorkerClient

client = JsonlWorkerClient(worker_stdout, worker_stdin)
hello = client.hello({"client_name": "demo"})
dialogs = client.dialogs()

messages = []
summary = client.stream_messages({"chat": "-1001234567890"}, messages.append)

client.start_listening(["-1001234567890"], messages.append)
next_message = client.read_event()
client.stop_listening()
```

Process spawning and restart policy intentionally stay outside this helper so a
supervisor can decide how sessions, proxies and credentials are isolated.

For simple tools and tests, `WorkerProcess` can own one worker subprocess. The
backend must be selected explicitly; it never silently falls back to the mock
backend:

```python
from tg_client_stdio_worker.process import WorkerProcess, WorkerProcessConfig

with WorkerProcess(WorkerProcessConfig(args=["--mock"])) as client:
    dialogs = client.dialogs()
```

## Regex Parser

The first parser layer is regex-based and intentionally small. It can extract
basic executable signals and simple outcome messages from raw Telegram messages:

```python
from tg_client_stdio_worker.parsing import RegexSignalParser

parser = RegexSignalParser.default()
parsed = parser.parse_message(raw_message)
signals = parsed.signals
outcomes = parsed.outcomes
```

`parse_message()` reports every accepted non-overlapping signal and outcome,
plus parser diagnostics. The older `parse_signal()` and `parse_outcome()`
methods remain convenience wrappers that return the first accepted result.

Custom rule sets can be loaded from JSON-compatible dictionaries:

```python
parser = RegexSignalParser.from_payload({
    "signal_rules": [
        {
            "name": "pair-direction-expiry",
            "pattern": r"PAIR=(?P<symbol>[A-Z]{6}) DIR=(?P<direction>CALL|PUT) EXP=(?P<expiry>\\d+)m"
        }
    ]
})
```

The parser returns neutral Python dataclasses. Mapping them to broker-specific
trade DTOs is a host-application concern.

## C++ Worker Supervisor

`include/tg_client_stdio/worker_client.hpp` provides a small C++17 host-side
supervisor for one worker process. It owns the process, performs the `hello`
handshake, correlates request responses, dispatches request-id-zero live
events, streams typed `RawMessage` records for archive export, and enforces
bounded JSONL input/output and event-queue limits:

```cpp
#include <tg_client_stdio/worker_client.hpp>

tg_client_stdio::WorkerProcessConfig config;
config.command = {"python", "-m", "tg_client_stdio_worker", "--mock"};

tg_client_stdio::WorkerClient worker;
worker.start(config, [](const auto& event) {
    // Handle message.received or other worker-originated events.
});

const auto dialogs = worker.dialogs();
const auto typed_dialogs = worker.list_dialogs();
const auto auth = worker.get_auth_status();
tg_client_stdio::ExportQuery query;
query.chat = "-1001234567890";
worker.stream_messages(query, [](const tg_client_stdio::RawMessage& message) {
    // Convert or persist one message at a time.
});
worker.start_listening({"-1001234567890"});
worker.stop_listening();
worker.stop();
```

`list_dialogs()` and `get_auth_status()` are typed convenience wrappers around
the same JSONL operations. They validate the normalized dialog identity/title
and authorization booleans before returning C++ DTOs.

`WorkerClient` is deliberately a process/protocol API, not an OptionX DTO
layer. One instance owns one worker and therefore one Telegram session. A
host that needs several accounts should create one instance per session and
coordinate them at the application level.

The C++ supervisor uses the vendored `tiny-process-library` and
`nlohmann-json` submodules. They are implementation dependencies of this
repository and are not part of the Python worker contract.

## Multi-Account Policy

Version 1 uses one Telegram account/session per worker process. A supervisor can
start multiple workers for multiple accounts, each with its own session file and
proxy settings.
