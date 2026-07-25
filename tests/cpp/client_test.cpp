#include <tg_client_stdio/client.hpp>

#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>

static_assert(!std::is_copy_constructible<tg_client_stdio::Client>::value,
              "tg-client-stdio Client must not be copy-constructible");
static_assert(!std::is_copy_assignable<tg_client_stdio::Client>::value,
              "tg-client-stdio Client must not be copy-assignable");
static_assert(!std::is_move_constructible<tg_client_stdio::Client>::value,
              "tg-client-stdio Client must not be move-constructible");
static_assert(!std::is_move_assignable<tg_client_stdio::Client>::value,
              "tg-client-stdio Client must not be move-assignable");

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

void expect_send_request_allocates_ids() {
    std::istringstream input;
    std::ostringstream output;
    tg_client_stdio::Client client(input, output);

    const auto first = client.send_request("hello");
    const auto second = client.send_request(
        "messages.export",
        R"({"chat":"-100"})");

    if (first != 1 || second != 2) {
        throw std::runtime_error("request ids were not sequential");
    }

    expect_eq(
        output.str(),
        "{\"protocol_version\":1,\"message_type\":\"request\",\"request_id\":1,"
        "\"operation\":\"hello\",\"payload\":{}}\n"
        "{\"protocol_version\":1,\"message_type\":\"request\",\"request_id\":2,"
        "\"operation\":\"messages.export\",\"payload\":{\"chat\":\"-100\"}}\n",
        "encoded client output");
}

void expect_read_record_requires_lf() {
    std::istringstream input("{\"ok\":true}");

    bool threw = false;
    try {
        (void)tg_client_stdio::read_jsonl_record(input);
    } catch (const std::runtime_error&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("unterminated JSONL record was accepted");
    }
}

void expect_read_record_enforces_limit() {
    std::istringstream input("{\"padding\":\"" + std::string(512, 'x') + "\"}\n");

    bool threw = false;
    try {
        (void)tg_client_stdio::read_jsonl_record(
            input,
            tg_client_stdio::kMinMaxJsonlRecordBytes);
    } catch (const std::runtime_error&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("oversized inbound JSONL record was accepted");
    }
}

void expect_rejects_too_small_read_limit() {
    std::istringstream input;

    bool threw = false;
    try {
        (void)tg_client_stdio::read_jsonl_record(
            input,
            tg_client_stdio::kMinMaxJsonlRecordBytes - 1);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("too-small read limit was accepted");
    }
}

void expect_rejects_too_small_client_limit() {
    std::istringstream input;
    std::ostringstream output;

    bool threw = false;
    try {
        tg_client_stdio::Client client(
            input,
            output,
            tg_client_stdio::ClientConfig{
                tg_client_stdio::kMinMaxJsonlRecordBytes - 1});
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    if (!threw) {
        throw std::runtime_error("too-small Client limit was accepted");
    }
}

} // namespace

int main() {
    expect_send_request_allocates_ids();
    expect_read_record_requires_lf();
    expect_read_record_enforces_limit();
    expect_rejects_too_small_read_limit();
    expect_rejects_too_small_client_limit();
    return 0;
}
