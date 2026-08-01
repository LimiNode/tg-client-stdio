#pragma once

/// \file worker_client.hpp
/// \brief C++17 process supervisor and JSONL client for tg-client-stdio.

#include "protocol.hpp"

#include <nlohmann/json.hpp>
#include <process.hpp>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace tg_client_stdio {

/// \struct WorkerProcessConfig
/// \brief Process and queue limits for one Telegram worker session.
struct WorkerProcessConfig {
    std::vector<std::string> command;
    std::string working_directory;
    std::size_t max_jsonl_record_bytes = kDefaultMaxJsonlRecordBytes;
    std::size_t max_queued_records = 4096;
    std::size_t max_queued_bytes = 16u * 1024u * 1024u;
    std::chrono::milliseconds startup_timeout{5000};
    std::chrono::milliseconds shutdown_timeout{5000};
    std::function<void(const std::string&)> on_stderr;
};

/// \class WorkerClient
/// \brief Supervises one tg-client-stdio worker process.
///
/// The client owns one worker process and its stdin/stdout pipes. stdout is
/// parsed as bounded JSONL; stderr is diagnostics only. Requests are
/// correlated by request ID, while request ID zero events and session errors
/// are delivered to the event callback from the client's dispatcher thread.
///
/// Lifecycle calls should be serialized by the owner. Calling stop() from an
/// event callback sends a graceful shutdown request and returns; the owner
/// must call stop() again from a non-dispatcher thread to join the process.
class WorkerClient final {
public:
    using json = nlohmann::json;
    using event_handler_t = std::function<void(const json&)>;

    WorkerClient() = default;

    ~WorkerClient() {
        stop();
    }

    WorkerClient(const WorkerClient&) = delete;
    WorkerClient& operator=(const WorkerClient&) = delete;

    /// \brief Starts the worker and completes the hello handshake.
    bool start(WorkerProcessConfig config, event_handler_t event_handler = {}) {
        if (config.command.empty() ||
            config.max_jsonl_record_bytes < kMinMaxJsonlRecordBytes ||
            config.max_queued_records == 0 ||
            config.max_queued_bytes == 0 ||
            config.startup_timeout <= std::chrono::milliseconds::zero() ||
            config.shutdown_timeout <= std::chrono::milliseconds::zero()) {
            return false;
        }

        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (process_ != nullptr || dispatcher_.joinable()) {
                return false;
            }
            config_ = std::move(config);
            event_handler_ = std::move(event_handler);
            reset_state_locked();
        }

        try {
            auto process = std::make_unique<TinyProcessLib::Process>(
                config_.command,
                config_.working_directory,
                [this](const char* bytes, std::size_t size) {
                    on_stdout(bytes, size);
                },
                [this](const char* bytes, std::size_t size) {
                    on_stderr(bytes, size);
                },
                true);
            std::lock_guard<std::mutex> lock(mutex_);
            process_ = std::move(process);
        }
        catch (...) {
            mark_failure("failed to create worker process");
            stop();
            return false;
        }

        if (!has_live_process()) {
            mark_failure("worker process failed to start");
            stop();
            return false;
        }

        try {
            dispatcher_ = std::thread(&WorkerClient::dispatch_loop, this);
            const auto handshake = request(
                "hello",
                json{{"client_name", "tg-client-stdio-cpp"}},
                config_.startup_timeout);
            if (!handshake.is_object() || !handshake.contains("capabilities")) {
                throw std::runtime_error("worker hello response has no capabilities");
            }
            return true;
        }
        catch (...) {
            stop();
            return false;
        }
    }

    /// \brief Sends a correlated request and waits for its terminal response.
    json request(
            std::string operation,
            json payload = json::object(),
            std::chrono::milliseconds timeout = std::chrono::milliseconds::zero(),
            event_handler_t request_event_handler = {}) {
        if (operation.empty()) {
            throw std::invalid_argument("worker operation must not be empty");
        }
        const auto request_id = send_request(
            std::move(operation), std::move(payload),
            std::move(request_event_handler));
        const auto effective_timeout = timeout <= std::chrono::milliseconds::zero()
            ? config_snapshot().startup_timeout
            : timeout;
        const auto record = wait_for_response(request_id, effective_timeout);
        const auto type = record.value("message_type", "");
        const auto response_payload = record.value("payload", json::object());
        if (type == "error") {
            throw std::runtime_error(response_payload.value(
                "message", "worker returned an error"));
        }
        if (type != "response") {
            throw std::runtime_error("worker returned a non-terminal record");
        }
        return response_payload;
    }

    /// \brief Starts the worker live listener.
    bool start_listening(
            const std::vector<std::string>& chats,
            event_handler_t event_handler = {},
            const std::vector<std::string>& topic_ids = {}) {
        event_handler_t previous_handler;
        if (event_handler) {
            std::lock_guard<std::mutex> lock(mutex_);
            previous_handler = event_handler_;
            event_handler_ = event_handler;
        }
        try {
            request("messages.listen", json{
                {"chats", chats},
                {"topic_ids", topic_ids},
            }, config_snapshot().startup_timeout);
            return true;
        }
        catch (...) {
            if (event_handler) {
                std::lock_guard<std::mutex> lock(mutex_);
                event_handler_ = std::move(previous_handler);
            }
            throw;
        }
    }

    /// \brief Stops the active worker live listener.
    bool stop_listening() {
        request("messages.stop", json::object(), config_snapshot().shutdown_timeout);
        return true;
    }

    /// \brief Returns the worker dialog list.
    json dialogs() {
        return request("dialogs.list", json::object());
    }

    /// \brief Returns the worker authorization status.
    json auth_status() {
        return request("auth.status", json::object());
    }

    /// \brief Sends a Telegram login code request.
    json auth_send_code(std::string phone) {
        return request("auth.send_code", json{{"phone", std::move(phone)}});
    }

    /// \brief Submits a Telegram login code.
    json auth_submit_code(std::string code) {
        return request("auth.submit_code", json{{"code", std::move(code)}});
    }

    /// \brief Submits a Telegram two-factor password.
    json auth_submit_password(std::string password) {
        return request("auth.submit_password", json{{"password", std::move(password)}});
    }

    /// \brief Stops the worker gracefully, then kills it after the timeout.
    void stop() noexcept {
        const bool on_dispatcher = is_dispatcher_thread();
        if (on_dispatcher) {
            try {
                mark_stopping();
                send_request("shutdown", json::object(), {});
            }
            catch (...) {
            }
            return;
        }

        const auto config = config_snapshot();
        if (has_live_process()) {
            try {
                bool should_request = false;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (!stopping_) {
                        stopping_ = true;
                        should_request = true;
                    }
                }
                if (should_request) {
                    request("shutdown", json::object(), config.shutdown_timeout);
                }
            }
            catch (...) {
            }

            {
                std::lock_guard<std::mutex> write_lock(write_mutex_);
                std::lock_guard<std::mutex> lock(mutex_);
                if (process_ != nullptr) {
                    process_->close_stdin();
                }
            }

            int exit_status = 0;
            const auto deadline = std::chrono::steady_clock::now() +
                config.shutdown_timeout;
            while (!process_exited(exit_status) &&
                   std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (!process_exited(exit_status)) {
                std::lock_guard<std::mutex> lock(mutex_);
                if (process_ != nullptr) {
                    process_->kill();
                }
            }
        }

        {
            std::lock_guard<std::mutex> lock(mutex_);
            dispatcher_stop_ = true;
        }
        condition_.notify_all();
        if (dispatcher_.joinable()) {
            dispatcher_.join();
        }

        std::unique_ptr<TinyProcessLib::Process> process;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            process = std::move(process_);
            reset_state_locked();
        }
        process.reset();
    }

    /// \brief Returns whether the worker is running and handshaken.
    bool is_running() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return process_ != nullptr && !failed_ && !stopping_;
    }

private:
    struct QueuedRecord {
        json value;
        std::size_t bytes = 0;
    };

    WorkerProcessConfig config_snapshot() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return config_;
    }

    bool has_live_process() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return process_ != nullptr && process_->get_id() != 0;
    }

    bool process_exited(int& exit_status) const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return process_ == nullptr || process_->try_get_exit_status(exit_status);
    }

    bool is_dispatcher_thread() const noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        return dispatcher_.joinable() &&
            dispatcher_.get_id() == std::this_thread::get_id();
    }

    void mark_stopping() {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
    }

    std::uint64_t send_request(
            std::string operation,
            json payload,
            event_handler_t request_event_handler) {
        std::uint64_t request_id = 0;
        WorkerProcessConfig config;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (process_ == nullptr || failed_) {
                throw std::runtime_error(failure_message_.empty()
                    ? "worker is not running"
                    : failure_message_);
            }
            request_id = next_request_id_++;
            if (request_id == 0) {
                throw std::overflow_error("worker request_id exhausted");
            }
            config = config_;
            if (request_event_handler) {
                request_event_handlers_.emplace(
                    request_id, std::move(request_event_handler));
            }
        }

        const json envelope{
            {"protocol_version", kProtocolVersion},
            {"message_type", "request"},
            {"request_id", request_id},
            {"operation", operation},
            {"payload", std::move(payload)},
        };
        auto line = envelope.dump();
        line.push_back('\n');
        if (line.size() > config.max_jsonl_record_bytes) {
            std::lock_guard<std::mutex> lock(mutex_);
            request_event_handlers_.erase(request_id);
            throw std::runtime_error("outbound JSONL record is too large");
        }
        {
            std::lock_guard<std::mutex> write_lock(write_mutex_);
            std::lock_guard<std::mutex> lock(mutex_);
            if (process_ == nullptr || !process_->write(line)) {
                request_event_handlers_.erase(request_id);
                mark_failure_locked("failed to write worker request");
                throw std::runtime_error("failed to write worker request");
            }
        }
        return request_id;
    }

    json wait_for_response(
            std::uint64_t request_id,
            std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        const auto ready = [this, request_id] {
            return responses_.find(request_id) != responses_.end() ||
                   failed_ || dispatcher_stop_;
        };
        if (!condition_.wait_for(lock, timeout, ready)) {
            request_event_handlers_.erase(request_id);
            throw std::runtime_error("worker response timed out");
        }
        const auto response = responses_.find(request_id);
        if (response == responses_.end()) {
            throw std::runtime_error(failure_message_.empty()
                ? "worker session stopped"
                : failure_message_);
        }
        auto value = std::move(response->second.value);
        response_bytes_ -= response->second.bytes;
        responses_.erase(response);
        request_event_handlers_.erase(request_id);
        return value;
    }

    void dispatch_loop() {
        for (;;) {
            json record;
            event_handler_t handler;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                condition_.wait(lock, [this] {
                    return dispatcher_stop_ || !records_.empty();
                });
                if (records_.empty() && dispatcher_stop_) {
                    return;
                }
                const auto bytes = records_.front().bytes;
                record = std::move(records_.front().value);
                records_.pop_front();
                queued_bytes_ -= bytes;

                const auto type = record.value("message_type", "");
                const auto request_id = record.value("request_id", 0ull);
                if (type == "event" || (type == "error" && request_id == 0)) {
                    if (request_id == 0) {
                        handler = event_handler_;
                    }
                    else {
                        const auto it = request_event_handlers_.find(request_id);
                        if (it != request_event_handlers_.end()) {
                            handler = it->second;
                        }
                    }
                }
                else {
                    if (responses_.find(request_id) != responses_.end() ||
                        response_bytes_ + bytes > config_.max_queued_bytes) {
                        mark_failure_locked("worker response queue limit exceeded");
                    }
                    else {
                        responses_.emplace(
                            request_id, QueuedRecord{std::move(record), bytes});
                        response_bytes_ += bytes;
                        condition_.notify_all();
                    }
                    continue;
                }
            }
            if (handler) {
                try {
                    handler(record);
                }
                catch (...) {
                    mark_failure("worker event callback threw");
                }
            }
        }
    }

    void on_stdout(const char* bytes, std::size_t size) {
        std::lock_guard<std::mutex> lock(mutex_);
        stdout_buffer_.append(bytes, size);
        if (stdout_buffer_.size() > config_.max_jsonl_record_bytes &&
            stdout_buffer_.find('\n') == std::string::npos) {
            mark_failure_locked("inbound JSONL record is too large");
            return;
        }
        for (;;) {
            const auto newline = stdout_buffer_.find('\n');
            if (newline == std::string::npos) {
                return;
            }
            const auto line_size = newline + 1;
            if (line_size > config_.max_jsonl_record_bytes) {
                mark_failure_locked("inbound JSONL record is too large");
                return;
            }
            auto line = stdout_buffer_.substr(0, line_size);
            stdout_buffer_.erase(0, line_size);
            try {
                auto record = json::parse(line);
                if (records_.size() >= config_.max_queued_records ||
                    queued_bytes_ + line_size > config_.max_queued_bytes) {
                    mark_failure_locked("worker event queue limit exceeded");
                    return;
                }
                records_.push_back(QueuedRecord{std::move(record), line_size});
                queued_bytes_ += line_size;
                condition_.notify_all();
            }
            catch (...) {
                mark_failure_locked("worker emitted invalid JSONL");
                return;
            }
        }
    }

    void on_stderr(const char* bytes, std::size_t size) {
        std::function<void(const std::string&)> callback;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            callback = config_.on_stderr;
        }
        if (callback) {
            try {
                callback(std::string(bytes, size));
            }
            catch (...) {
                mark_failure("worker stderr callback threw");
            }
        }
    }

    void mark_failure(const std::string& message) {
        std::lock_guard<std::mutex> lock(mutex_);
        mark_failure_locked(message);
    }

    void mark_failure_locked(const std::string& message) {
        failed_ = true;
        if (failure_message_.empty()) {
            failure_message_ = message;
        }
        condition_.notify_all();
    }

    void reset_state_locked() {
        records_.clear();
        responses_.clear();
        response_bytes_ = 0;
        request_event_handlers_.clear();
        stdout_buffer_.clear();
        queued_bytes_ = 0;
        next_request_id_ = 1;
        dispatcher_stop_ = false;
        stopping_ = false;
        failed_ = false;
        failure_message_.clear();
    }

    mutable std::mutex mutex_;
    std::mutex write_mutex_;
    std::condition_variable condition_;
    WorkerProcessConfig config_;
    std::unique_ptr<TinyProcessLib::Process> process_;
    std::thread dispatcher_;
    std::string stdout_buffer_;
    std::deque<QueuedRecord> records_;
    std::unordered_map<std::uint64_t, QueuedRecord> responses_;
    std::unordered_map<std::uint64_t, event_handler_t> request_event_handlers_;
    event_handler_t event_handler_;
    std::size_t queued_bytes_ = 0;
    std::size_t response_bytes_ = 0;
    std::uint64_t next_request_id_ = 1;
    bool dispatcher_stop_ = false;
    bool stopping_ = false;
    bool failed_ = false;
    std::string failure_message_;
};

} // namespace tg_client_stdio
