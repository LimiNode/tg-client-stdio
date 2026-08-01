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
        send(request_id, "response", operation, {"dialogs": [{"id": "42"}]})
    elif operation == "auth.status":
        send(request_id, "response", operation, {"authorized": False})
    elif operation == "messages.listen":
        send(request_id, "response", operation, {"listening": True})
        send(0, "event", "message.received", {
            "message": {"id": "7", "chat_id": "42", "text": "BUY EURUSD"}
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

    tg_client_stdio::WorkerClient client;
    expect(client.start(config, [&](const auto& event) {
        {
            std::lock_guard<std::mutex> lock(mutex);
            received_event = event;
        }
        condition.notify_all();
    }), "worker did not start");
    expect(client.is_running(), "worker is not running after handshake");

    const auto dialogs = client.dialogs();
    expect(dialogs.at("dialogs").at(0).at("id") == "42",
           "dialogs response mismatch");

    const auto auth = client.auth_status();
    expect(!auth.at("authorized").get<bool>(), "auth response mismatch");

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

    expect(client.stop_listening(), "listener did not stop");
    client.stop();
    expect(!client.is_running(), "worker is still running after stop");
    return 0;
}
