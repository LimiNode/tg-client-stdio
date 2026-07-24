#pragma once

/// \file client.hpp
/// \brief C++ client-side JSONL helpers for tg-client-stdio worker processes.

#include "protocol.hpp"

#include <cstddef>
#include <cstdint>
#include <istream>
#include <ostream>
#include <stdexcept>
#include <string>
#include <utility>

namespace tg_client_stdio {

struct ClientConfig {
    std::size_t max_jsonl_record_bytes = kDefaultMaxJsonlRecordBytes;
};

/// \brief Reads one bounded LF-terminated JSONL record.
///
/// The returned string includes the terminal LF. EOF before any byte returns an
/// empty string. EOF after a partial record is a protocol error because one
/// envelope must be exactly one LF-terminated JSON object.
inline std::string read_jsonl_record(
        std::istream& input,
        const std::size_t max_jsonl_record_bytes = kDefaultMaxJsonlRecordBytes) {
    if (max_jsonl_record_bytes < kMinMaxJsonlRecordBytes) {
        throw std::invalid_argument(
            "max_jsonl_record_bytes is below tg-client-stdio protocol minimum");
    }

    std::string line;
    line.reserve(max_jsonl_record_bytes < 4096 ? max_jsonl_record_bytes : 4096);

    char ch = '\0';
    while (input.get(ch)) {
        line.push_back(ch);
        if (line.size() > max_jsonl_record_bytes) {
            throw std::runtime_error("tg-client-stdio inbound JSONL record is too large");
        }
        if (ch == '\n') {
            return line;
        }
    }

    if (line.empty()) {
        return {};
    }
    throw std::runtime_error("tg-client-stdio inbound JSONL record is not LF-terminated");
}

class Client {
public:
    Client(std::istream& input,
           std::ostream& output,
           ClientConfig config = {})
        : m_input(&input),
          m_output(&output),
          m_config(config) {
        if (m_config.max_jsonl_record_bytes < kMinMaxJsonlRecordBytes) {
            throw std::invalid_argument(
                "max_jsonl_record_bytes is below tg-client-stdio protocol minimum");
        }
    }

    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;
    Client(Client&&) = delete;
    Client& operator=(Client&&) = delete;

    std::uint64_t next_request_id() {
        if (m_next_request_id == 0) {
            throw std::overflow_error("tg-client-stdio request_id exhausted");
        }
        return m_next_request_id++;
    }

    std::uint64_t send_request(std::string operation,
                               std::string payload_json = "{}") {
        const auto request_id = next_request_id();
        send(make_request(request_id, std::move(operation), std::move(payload_json)));
        return request_id;
    }

    void send(const Envelope& envelope) {
        *m_output << encode_envelope_jsonl(
            envelope,
            m_config.max_jsonl_record_bytes);
        m_output->flush();
        if (!*m_output) {
            throw std::runtime_error("failed to write tg-client-stdio JSONL record");
        }
    }

    std::string read_record() {
        return read_jsonl_record(*m_input, m_config.max_jsonl_record_bytes);
    }

private:
    std::istream* m_input;
    std::ostream* m_output;
    ClientConfig m_config;
    std::uint64_t m_next_request_id = 1;
};

} // namespace tg_client_stdio
