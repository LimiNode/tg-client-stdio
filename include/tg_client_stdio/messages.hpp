#pragma once

/// \file messages.hpp
/// \brief Typed C++ DTOs for tg-client-stdio message export records.

#include <nlohmann/json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace tg_client_stdio {

/// \struct Dialog
/// \brief Normalized Telegram dialog metadata for account/channel selection.
struct Dialog {
    std::string chat_id;
    std::string title;
    std::string username;
    std::string kind;

    /// \brief Parses one dialog returned by `dialogs.list`.
    static Dialog from_json(const nlohmann::json& value) {
        if (!value.is_object()) {
            throw std::invalid_argument("Telegram dialog must be an object");
        }
        Dialog dialog;
        dialog.chat_id = value.at("chat_id").get<std::string>();
        dialog.title = value.at("title").get<std::string>();
        dialog.username = value.value("username", "");
        dialog.kind = value.value("kind", "");
        if (dialog.chat_id.empty() || dialog.title.empty()) {
            throw std::invalid_argument("Telegram dialog identity is incomplete");
        }
        return dialog;
    }
};

/// \struct AuthStatus
/// \brief Typed authorization state returned by the worker.
struct AuthStatus {
    bool authorized = false;
    bool password_required = false;

    /// \brief Parses an `auth.status` response payload.
    static AuthStatus from_json(const nlohmann::json& value) {
        if (!value.is_object() ||
            !value.contains("authorized") ||
            !value.contains("password_required") ||
            value.at("authorized").type() != nlohmann::json::value_t::boolean ||
            value.at("password_required").type() != nlohmann::json::value_t::boolean) {
            throw std::invalid_argument("Telegram auth status is incomplete");
        }
        return {
            value.at("authorized").get<bool>(),
            value.at("password_required").get<bool>(),
        };
    }
};

/// \struct ExportQuery
/// \brief Bounded historical message export request.
struct ExportQuery {
    std::string chat;
    std::string topic_id;
    std::int64_t from_date_ms = 0;
    std::int64_t to_date_ms = 0;
    std::int64_t limit = 0;
    std::string order = "oldest_first";
    bool include_media = false;

    /// \brief Converts the query to the worker protocol payload.
    nlohmann::json to_json() const {
        if (chat.empty()) {
            throw std::invalid_argument("messages.export chat must not be empty");
        }
        if (order != "oldest_first" && order != "newest_first") {
            throw std::invalid_argument("messages.export order is invalid");
        }
        if (from_date_ms < 0 || to_date_ms < 0 || limit < 0) {
            throw std::invalid_argument("messages.export numeric fields must be non-negative");
        }

        nlohmann::json payload{
            {"chat", chat},
            {"order", order},
            {"include_media", include_media},
        };
        if (!topic_id.empty()) {
            payload["topic_id"] = topic_id;
        }
        if (from_date_ms != 0) {
            payload["from_date_ms"] = from_date_ms;
        }
        if (to_date_ms != 0) {
            payload["to_date_ms"] = to_date_ms;
        }
        if (limit != 0) {
            payload["limit"] = limit;
        }
        return payload;
    }
};

/// \struct RawMessage
/// \brief Stable transport-level representation of one Telegram message.
struct RawMessage {
    std::string chat_id;
    std::string chat_title;
    std::string topic_id;
    std::int64_t message_id = 0;
    std::int64_t date_ms = 0;
    std::int64_t edit_date_ms = 0;
    std::string sender_id;
    std::int64_t reply_to_message_id = 0;
    std::string grouped_id;
    std::string text;
    nlohmann::json media = nlohmann::json::array();

    /// \brief Parses the worker's `export.message` message object.
    static RawMessage from_json(const nlohmann::json& value) {
        if (!value.is_object()) {
            throw std::invalid_argument("Telegram raw message must be an object");
        }
        RawMessage message;
        message.chat_id = value.at("chat_id").get<std::string>();
        message.chat_title = value.value("chat_title", "");
        message.topic_id = value.value("topic_id", "");
        message.message_id = value.at("message_id").get<std::int64_t>();
        message.date_ms = value.at("date_ms").get<std::int64_t>();
        message.edit_date_ms = value.value("edit_date_ms", 0ll);
        message.sender_id = value.value("sender_id", "");
        message.reply_to_message_id = value.value("reply_to_message_id", 0ll);
        message.grouped_id = value.value("grouped_id", "");
        message.text = value.value("text", "");
        message.media = value.value("media", nlohmann::json::array());
        if (!message.media.is_array()) {
            throw std::invalid_argument("Telegram raw message media must be an array");
        }
        if (message.message_id <= 0 || message.date_ms < 0 ||
            message.edit_date_ms < 0 || message.reply_to_message_id < 0) {
            throw std::invalid_argument("Telegram raw message contains invalid numeric fields");
        }
        return message;
    }

    /// \brief Returns the stable message identity used for transport dedupe.
    std::string message_identity() const {
        return "telegram:" + chat_id + ":" +
            (topic_id.empty() ? "0" : topic_id) + ":" +
            std::to_string(message_id);
    }

    /// \brief Returns the revision identity used for replay and diagnostics.
    std::string revision_identity() const {
        return message_identity() + ":" + std::to_string(edit_date_ms);
    }

    /// \brief Returns the identity of the replied-to message, if any.
    std::string reply_to_message_identity() const {
        if (reply_to_message_id <= 0) {
            return {};
        }
        return "telegram:" + chat_id + ":" +
            (topic_id.empty() ? "0" : topic_id) + ":" +
            std::to_string(reply_to_message_id);
    }

    /// \brief Serializes the DTO using the worker protocol field names.
    nlohmann::json to_json() const {
        return nlohmann::json{
            {"chat_id", chat_id},
            {"chat_title", chat_title},
            {"topic_id", topic_id},
            {"message_id", message_id},
            {"date_ms", date_ms},
            {"edit_date_ms", edit_date_ms},
            {"sender_id", sender_id},
            {"reply_to_message_id", reply_to_message_id},
            {"grouped_id", grouped_id},
            {"text", text},
            {"media", media},
            {"message_identity", message_identity()},
            {"revision_identity", revision_identity()},
            {"reply_to_message_identity", reply_to_message_identity()},
        };
    }
};

/// \struct ExportSummary
/// \brief Terminal result of a streaming historical export.
struct ExportSummary {
    std::int64_t messages = 0;
    bool truncated = false;

    static ExportSummary from_json(const nlohmann::json& value) {
        if (!value.is_object() || !value.contains("messages") ||
            !value.contains("truncated")) {
            throw std::invalid_argument("messages.export response is incomplete");
        }
        ExportSummary summary;
        summary.messages = value.at("messages").get<std::int64_t>();
        summary.truncated = value.at("truncated").get<bool>();
        if (summary.messages < 0) {
            throw std::invalid_argument("messages.export count is negative");
        }
        return summary;
    }
};

} // namespace tg_client_stdio
