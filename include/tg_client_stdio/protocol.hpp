#pragma once

/// \file protocol.hpp
/// \brief C++ helpers for tg-client-stdio protocol envelopes.

#include <cstdint>
#include <cstddef>
#include <stdexcept>
#include <string>
#include <utility>

namespace tg_client_stdio {

inline constexpr int kProtocolVersion = 1;
inline constexpr std::size_t kDefaultMaxJsonlRecordBytes = 1024 * 1024;
inline constexpr std::size_t kMinMaxJsonlRecordBytes = 512;

enum class MessageType {
    Request,
    Response,
    Event,
    Error
};

struct Envelope {
    int protocol_version = kProtocolVersion;
    MessageType message_type = MessageType::Request;
    std::uint64_t request_id = 0;
    std::string operation;
    std::string payload_json = "{}";
};

/// \brief Returns whether a payload is safe for direct insertion in one JSONL envelope.
///
/// This helper intentionally does not parse JSON. Callers must pass a compact,
/// already validated JSON object. Raw CR/LF bytes are rejected because they
/// would split one logical envelope across multiple JSONL records.
inline bool is_compact_json_object_payload(const std::string& value) {
    return !value.empty() &&
           value.front() == '{' &&
           value.back() == '}' &&
           value.find('\n') == std::string::npos &&
           value.find('\r') == std::string::npos;
}

inline const char* to_string(const MessageType type) {
    switch (type) {
    case MessageType::Request:
        return "request";
    case MessageType::Response:
        return "response";
    case MessageType::Event:
        return "event";
    case MessageType::Error:
        return "error";
    }
    return "error";
}

inline std::string json_escape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (const unsigned char ch : value) {
        switch (ch) {
        case '"':
            out += "\\\"";
            break;
        case '\\':
            out += "\\\\";
            break;
        case '\b':
            out += "\\b";
            break;
        case '\f':
            out += "\\f";
            break;
        case '\n':
            out += "\\n";
            break;
        case '\r':
            out += "\\r";
            break;
        case '\t':
            out += "\\t";
            break;
        default:
            if (ch < 0x20) {
                static constexpr char hex[] = "0123456789abcdef";
                out += "\\u00";
                out += hex[(ch >> 4) & 0x0f];
                out += hex[ch & 0x0f];
            } else {
                out.push_back(static_cast<char>(ch));
            }
            break;
        }
    }
    return out;
}

inline std::string encode_envelope_json(const Envelope& envelope) {
    if (envelope.protocol_version != kProtocolVersion) {
        throw std::invalid_argument("unsupported tg-client-stdio protocol version");
    }
    if (envelope.operation.empty()) {
        throw std::invalid_argument("tg-client-stdio operation must not be empty");
    }
    if (!is_compact_json_object_payload(envelope.payload_json)) {
        throw std::invalid_argument(
            "tg-client-stdio payload_json must be a compact validated JSON object");
    }

    std::string json;
    json.reserve(128 + envelope.operation.size() + envelope.payload_json.size());
    json += "{\"protocol_version\":";
    json += std::to_string(envelope.protocol_version);
    json += ",\"message_type\":\"";
    json += to_string(envelope.message_type);
    json += "\",\"request_id\":";
    json += std::to_string(envelope.request_id);
    json += ",\"operation\":\"";
    json += json_escape(envelope.operation);
    json += "\",\"payload\":";
    json += envelope.payload_json;
    json += "}";
    return json;
}

inline std::string encode_envelope_jsonl(
        const Envelope& envelope,
        const std::size_t max_jsonl_record_bytes = kDefaultMaxJsonlRecordBytes) {
    if (max_jsonl_record_bytes < kMinMaxJsonlRecordBytes) {
        throw std::invalid_argument(
            "tg-client-stdio max_jsonl_record_bytes is below protocol minimum");
    }

    auto line = encode_envelope_json(envelope);
    line.push_back('\n');
    if (line.size() > max_jsonl_record_bytes) {
        throw std::invalid_argument(
            "tg-client-stdio JSONL envelope exceeds max_jsonl_record_bytes");
    }
    return line;
}

inline Envelope make_request(
        const std::uint64_t request_id,
        std::string operation,
        std::string payload_json = "{}") {
    if (request_id == 0) {
        throw std::invalid_argument("tg-client-stdio request_id must be non-zero");
    }

    Envelope envelope;
    envelope.message_type = MessageType::Request;
    envelope.request_id = request_id;
    envelope.operation = std::move(operation);
    envelope.payload_json = std::move(payload_json);
    return envelope;
}

} // namespace tg_client_stdio
