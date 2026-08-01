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
contract before Telegram authorization is wired in.

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

The worker also exposes `auth.status`, `auth.send_code`,
`auth.submit_code`, and `auth.submit_password` over JSONL. This keeps stdin
reserved for protocol records while allowing a host application to own the
login UI. Proxy URLs can be supplied with `--proxy` using `http://`,
`socks5://`, or `socks5h://` schemes.

Interactive Telegram login is intentionally outside JSONL stdio; stdin is
reserved for protocol records.

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
events, and enforces bounded JSONL input/output and event-queue limits:

```cpp
#include <tg_client_stdio/worker_client.hpp>

tg_client_stdio::WorkerProcessConfig config;
config.command = {"python", "-m", "tg_client_stdio_worker", "--mock"};

tg_client_stdio::WorkerClient worker;
worker.start(config, [](const auto& event) {
    // Handle message.received or other worker-originated events.
});

const auto dialogs = worker.dialogs();
worker.start_listening({"-1001234567890"});
worker.stop_listening();
worker.stop();
```

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
