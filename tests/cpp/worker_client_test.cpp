#include <tg_client_stdio/worker_client.hpp>

#include <condition_variable>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef TG_CLIENT_STDIO_TEST_PYTHON_EXECUTABLE
#error "TG_CLIENT_STDIO_TEST_PYTHON_EXECUTABLE must be defined"
#endif

namespace {

void expect(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

} // namespace

int main() {
    const std::string worker_script = R"PY(
import json
import sys

def send(request_id, message_type, operation, payload):
    sys.stdout.write(json.dumps({
        "protocol_version": 1,
        "message_type": message_type,
        "request_id": request_id,
        "operation": operation,
        "payload": payload,
    }, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    operation = request["operation"]
    request_id = request["request_id"]
    if operation == "hello":
        send(request_id, "response", operation, {
            "capabilities": {"messages_listen": True, "messages_stop": True},
            "max_jsonl_record_bytes": 1048576,
        })
    elif operation == "dialogs.list":
        send(request_id, "response", operation, {"dialogs": [{
            "chat_id": "42", "title": "Signals",
            "username": "signals", "kind": "channel"
        }]})
    elif operation == "auth.status":
        send(request_id, "response", operation, {
            "authorized": False, "password_required": False
        })
    elif operation == "messages.export":
        send(request_id, "event", "export.started", {})
        send(request_id, "event", "export.message", {
            "message": {
                "chat_id": "42", "chat_title": "Signals", "topic_id": "7",
                "message_id": 11, "date_ms": 1800000000000,
                "edit_date_ms": 0, "sender_id": "99",
                "reply_to_message_id": 0, "grouped_id": "",
                "text": "EURUSD BUY 5m", "media": []
            }
        })
        send(request_id, "event", "export.message", {
            "message": {
                "chat_id": "42", "chat_title": "Signals", "topic_id": "7",
                "message_id": 12, "date_ms": 1800000005000,
                "edit_date_ms": 1800000006000, "sender_id": "99",
                "reply_to_message_id": 11, "grouped_id": "album-1",
                "text": "WIN", "media": [{"kind": "photo"}]
            }
        })
        send(request_id, "response", operation, {"messages": 2, "truncated": False})
    elif operation == "messages.listen":
        send(request_id, "response", operation, {"listening": True})
        send(0, "event", "message.received", {
            "message": {"id": "7", "chat_id": "42", "text": "BUY EURUSD"}
        })
        send(0, "error", "session", {
            "code": "session_failed",
            "message": "session error delivered as an uncorrelated record",
        })
    elif operation == "messages.stop":
        send(request_id, "response", operation, {"listening": False})
    elif operation == "shutdown":
        send(request_id, "response", operation, {"stopped": True})
        break
)PY";

    tg_client_stdio::WorkerProcessConfig config;
    config.command = {
        TG_CLIENT_STDIO_TEST_PYTHON_EXECUTABLE,
        "-u",
        "-c",
        worker_script,
    };
    config.max_jsonl_record_bytes = 4096;
    config.startup_timeout = std::chrono::seconds(5);
    config.shutdown_timeout = std::chrono::seconds(5);

    std::mutex mutex;
    std::condition_variable condition;
    nlohmann::json received_event;
    nlohmann::json received_error;

    tg_client_stdio::WorkerClient client;
    expect(client.start(config, [&](const auto& event) {
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (event.value("message_type", "") == "error") {
                received_error = event;
            }
            else {
                received_event = event;
            }
        }
        condition.notify_all();
    }), "worker did not start");
    expect(client.is_running(), "worker is not running after handshake");

    const auto dialogs = client.dialogs();
    expect(dialogs.at("dialogs").at(0).at("chat_id") == "42",
           "dialogs response mismatch");
    const auto typed_dialogs = client.list_dialogs();
    expect(typed_dialogs.size() == 1 &&
               typed_dialogs.front().title == "Signals" &&
               typed_dialogs.front().kind == "channel",
           "typed dialog response mismatch");

    const auto auth = client.auth_status();
    expect(!auth.at("authorized").get<bool>(), "auth response mismatch");
    const auto typed_auth = client.get_auth_status();
    expect(!typed_auth.authorized && !typed_auth.password_required,
           "typed auth response mismatch");

    tg_client_stdio::ExportQuery export_query;
    export_query.chat = "42";
    export_query.topic_id = "7";
    std::vector<tg_client_stdio::RawMessage> messages;
    const auto summary = client.stream_messages(export_query, [&](const auto& message) {
        messages.push_back(message);
    });
    expect(summary.messages == 2 && !summary.truncated,
           "export summary mismatch");
    expect(messages.size() == 2, "export message count mismatch");
    expect(messages[0].message_identity() == "telegram:42:7:11",
           "raw message identity mismatch");
    expect(messages[1].reply_to_message_identity() == "telegram:42:7:11",
           "raw reply identity mismatch");

    expect(client.start_listening({"42"}), "listener did not start");
    {
        std::unique_lock<std::mutex> lock(mutex);
        expect(condition.wait_for(lock, std::chrono::seconds(2), [&] {
            return !received_event.is_null();
        }), "live event was not delivered");
    }
    expect(received_event.at("operation") == "message.received",
           "live event operation mismatch");
    expect(received_event.at("payload").at("message").at("id") == "7",
           "live event payload mismatch");
    {
        std::unique_lock<std::mutex> lock(mutex);
        expect(condition.wait_for(lock, std::chrono::seconds(2), [&] {
            return !received_error.is_null();
        }), "uncorrelated worker error was not delivered");
    }
    expect(received_error.at("payload").at("code") == "session_failed",
           "uncorrelated worker error payload mismatch");

    expect(client.stop_listening(), "listener did not stop");
    client.stop();
    expect(!client.is_running(), "worker is still running after stop");
    return 0;
}
