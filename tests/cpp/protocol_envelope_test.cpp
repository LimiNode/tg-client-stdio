#include <tg_client_stdio/protocol.hpp>

#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void expect_eq(const std::string& actual,
               const std::string& expected,
               const char* name) {
    if (actual != expected) {
        std::cerr << name << " mismatch\nexpected: " << expected
                  << "\nactual:   " << actual << '\n';
        throw std::runtime_error(name);
    }
}

void expect_throws_zero_request_id() {
    bool threw = false;
    try {
        (void)tg_client_stdio::make_request(0, "hello");
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("zero request_id was accepted");
    }
}

void expect_throws_multiline_payload() {
    bool threw = false;
    try {
        const auto request = tg_client_stdio::make_request(
            3,
            "messages.export",
            "{\n\"chat\":\"-100\"\n}");
        (void)tg_client_stdio::encode_envelope_jsonl(request);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("multiline payload_json was accepted");
    }
}

void expect_throws_oversized_jsonl() {
    bool threw = false;
    try {
        const auto request = tg_client_stdio::make_request(
            4,
            "messages.export",
            "{\"padding\":\"" + std::string(512, 'x') + "\"}");
        (void)tg_client_stdio::encode_envelope_jsonl(
            request,
            tg_client_stdio::kMinMaxJsonlRecordBytes);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("oversized JSONL envelope was accepted");
    }
}

void expect_throws_too_small_jsonl_limit() {
    bool threw = false;
    try {
        const auto request = tg_client_stdio::make_request(5, "hello");
        (void)tg_client_stdio::encode_envelope_jsonl(
            request,
            tg_client_stdio::kMinMaxJsonlRecordBytes - 1);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("too-small JSONL limit was accepted");
    }
}

} // namespace

int main() {
    const auto hello = tg_client_stdio::make_request(
        1,
        "hello",
        R"({"client_name":"unit-test"})");

    expect_eq(
        tg_client_stdio::encode_envelope_json(hello),
        R"({"protocol_version":1,"message_type":"request","request_id":1,"operation":"hello","payload":{"client_name":"unit-test"}})",
        "hello envelope");

    const auto escaped = tg_client_stdio::make_request(
        2,
        "messages.export",
        R"({"chat":"-100"})");
    expect_eq(
        tg_client_stdio::encode_envelope_jsonl(escaped),
        "{\"protocol_version\":1,\"message_type\":\"request\",\"request_id\":2,"
        "\"operation\":\"messages.export\",\"payload\":{\"chat\":\"-100\"}}\n",
        "jsonl envelope");

    expect_throws_zero_request_id();
    expect_throws_multiline_payload();
    expect_throws_oversized_jsonl();
    expect_throws_too_small_jsonl_limit();
    return 0;
}
