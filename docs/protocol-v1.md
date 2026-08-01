# tg-client-stdio Protocol v1

This document defines the first protocol used between a host application and a
Telegram user-client worker process.

The initial transport is newline-delimited JSON over worker stdin/stdout bytes.
Payload text is UTF-8. Each line contains exactly one JSON object terminated by
LF.

Protocol v1 default:

```text
MAX_JSONL_RECORD_BYTES = 1048576
MIN_MAX_JSONL_RECORD_BYTES = 512
```

The terminating LF counts toward the limit. Hosts should keep pre-handshake
requests within this default. A peer must not advertise or accept an effective
limit below `MIN_MAX_JSONL_RECORD_BYTES`; this minimum is sized so protocol
handshake and session-level errors remain serializable. A worker may be
configured with a different effective limit and must announce it in
`hello.capabilities`.

## Envelope

Every JSONL record uses the same envelope:

```json
{
  "protocol_version": 1,
  "message_type": "request",
  "request_id": 42,
  "operation": "messages.export",
  "payload": {}
}
```

Fields:

- `protocol_version`: integer, currently `1`.
- `message_type`: one of `request`, `response`, `event`, `error`.
- `request_id`: non-zero for host-initiated requests. Worker live events may
  use `0` when they are not tied to a request.
- `operation`: operation or event name.
- `payload`: operation-specific JSON object.

Each request produces exactly one terminal record: either `response` or
`error`. The `request_id` remains active until that terminal record and must not
be reused while active.

Session-fatal errors terminate all active requests and the worker session. If
the worker exits before emitting per-request terminal records, the host must
report active requests as failed locally.

Both peers must enforce a maximum outbound JSONL record size before writing and
a maximum inbound JSONL record size while reading, before unbounded allocation
or JSON parsing. Hosts should also use bounded event queues and fail the worker
session explicitly when the application stops draining events.

## Initial Operations

### `hello`

Host request:

```json
{
  "protocol_version": 1,
  "message_type": "request",
  "request_id": 1,
  "operation": "hello",
  "payload": {
    "client_name": "example-host",
    "client_version": "0.1.0"
  }
}
```

Worker response:

```json
{
  "protocol_version": 1,
  "message_type": "response",
  "request_id": 1,
  "operation": "hello",
    "payload": {
      "worker_name": "tg-client-stdio-worker",
      "worker_version": "0.1.0",
      "backend": "telethon",
      "capabilities": {
      "dialogs_list": true,
      "messages_export": true,
      "messages_listen": true,
      "messages_stop": true,
      "auth_status": true,
      "auth_send_code": true,
      "auth_submit_code": true,
      "auth_submit_password": true,
      "auth_interactive": true,
      "multi_account": false,
      "max_jsonl_record_bytes": 1048576
    }
  }
}
```

### `dialogs.list`

Returns dialogs visible to the current worker session.

Worker response payload:

```json
{
  "dialogs": [
    {
      "chat_id": "-1001234567890",
      "title": "Signals",
      "username": "signals",
      "kind": "channel"
    }
  ]
}
```

### `messages.export`

Exports historical raw messages. The worker must stream records instead of
returning one large array.

Request payload:

```json
{
  "chat": "-1001234567890",
  "topic_id": "42",
  "from_date_ms": 1784830000000,
  "to_date_ms": 1784916400000,
  "limit": 1000,
  "order": "oldest_first",
  "include_media": false
}
```

`topic_id` is a filter, not an identity override. Exported messages derive
their topic identity from Telegram reply metadata. When a topic is requested,
the topic root message is included and merged with its replies according to
the requested date range, order, and limit.

Streaming lifecycle:

```text
messages.export request
  -> export.started event
  -> export.message event x N
  -> messages.export response
```

`export.message` event payload:

```json
{
  "message": {
    "chat_id": "-1001234567890",
    "chat_title": "Signals",
    "topic_id": "42",
    "message_id": 1234,
    "date_ms": 1784830000000,
    "edit_date_ms": 0,
    "sender_id": "777",
    "reply_to_message_id": 1230,
    "grouped_id": "987654321",
    "text": "EURUSD BUY 5m",
    "media": []
  }
}
```

Terminal `messages.export` response payload:

```json
{
  "messages": 1000,
  "truncated": false
}
```

### `messages.listen`

Starts one live listener for the configured chats. A worker accepts at most one
active listener; a second request fails with `listen_already_active`. The
request payload is:

```json
{
  "chats": ["-1001234567890", "signals"],
  "topic_ids": []
}
```

The terminal response confirms acceptance:

```json
{
  "accepted": true,
  "chats": ["-1001234567890"],
  "topic_ids": []
}
```

After that response, each live message is emitted as a worker-originated
`message.received` event with `request_id = 0`:

```json
{
  "protocol_version": 1,
  "message_type": "event",
  "request_id": 0,
  "operation": "message.received",
  "payload": {"message": {}}
}
```

The host must keep draining the event stream or stop the listener rather than
allow unbounded buffering.

If polling terminates with a non-fatal error, the worker clears the active
listener before emitting the error event. A subsequent `messages.listen`
request may therefore start a new listener.

### `messages.stop`

Stops the active live listener. It is idempotent and returns
`{"stopped":true}` even when no listener is active.

### Authentication operations

The worker keeps Telegram authorization state in its session. JSONL stdin is
still never used for an interactive prompt; authentication values are sent as
normal request payloads:

- `auth.status` returns `authorized` and `password_required`.
- `auth.send_code` accepts `{ "phone": "+10000000000" }`.
- `auth.submit_code` accepts `{ "code": "12345" }`.
- `auth.submit_password` accepts `{ "password": "..." }`.

Passwords must not be logged or echoed in responses. A successful
`auth.submit_code` may return `password_required=true`; the host then submits
the second-factor password through the dedicated operation. Authorization
errors remain correlated to the request that caused them.

### `shutdown`

Requests graceful worker shutdown. The worker should send a terminal response
before exiting when possible.

## Raw Message Identity

Keep message identity separate from revision identity:

```text
message_identity  = telegram:<chat_id>:<topic_id-or-0>:<message_id>
revision_identity = <message_identity>:<edit_date_ms-or-0>
```

`message_identity` is stable across edits. `revision_identity` changes when
Telegram exposes a new edit timestamp. The worker only reports Telegram message
identity and revision identity; application-specific execution, dedupe and
correction policy belongs to the host application.

The topic component is derived from the message itself, so exporting a message
from the whole chat and exporting the same message through a topic filter yields
the same identity.

`reply_to_message_id` should be preserved when Telegram exposes it because
result messages are often replies to the original signal. `grouped_id` should be
preserved for media albums.

## Error Payload

Recoverable errors use `message_type = "error"` and are terminal for their
`request_id`.

```json
{
  "protocol_version": 1,
  "message_type": "error",
  "request_id": 42,
  "operation": "messages.export",
  "payload": {
    "code": "authorization_required",
    "message": "Telegram session is not authorized",
    "fatal": false
  }
}
```
