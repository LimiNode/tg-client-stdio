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
- C++17 header-only envelope helpers;
- tests for the mock worker and C++ protocol helper.

The real Telethon backend is planned next. The mock backend exists so host
applications can build and test the stdio contract before Telegram
authorization is wired in.

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
```

Process spawning and restart policy intentionally stay outside this helper so a
supervisor can decide how sessions, proxies and credentials are isolated.

## C++ Helper

The first C++ layer is intentionally small: it builds protocol envelopes and
keeps message type names consistent with the worker. Process supervision will be
added after the worker protocol has settled.

## Multi-Account Policy

Version 1 uses one Telegram account/session per worker process. A supervisor can
start multiple workers for multiple accounts, each with its own session file and
proxy settings.
