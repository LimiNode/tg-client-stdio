from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Pattern

from .backend import RawMessage

_ASSET_CODE = r"(?:EUR|USD|GBP|JPY|AUD|CAD|CHF|NZD|XAU|XAG|BTC|ETH)"
_SYMBOL_PATTERN = rf"{_ASSET_CODE}(?:[/_-]?{_ASSET_CODE})"
_LOOSE_SEPARATOR = r"[\s\S]{0,80}?"
_ALLOWED_REGEX_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL | re.ASCII


@dataclass(frozen=True)
class ParsedSignal:
    symbol: str
    direction: str
    expiry_seconds: int | None = None
    name: str = ""
    source_message_identity: str = ""
    source_revision_identity: str = ""
    parser_rule: str = ""


@dataclass(frozen=True)
class ParsedOutcome:
    result: str
    symbol: str = ""
    reply_to_message_id: int = 0
    reply_to_message_identity: str = ""
    source_message_identity: str = ""
    source_revision_identity: str = ""
    parser_rule: str = ""


@dataclass(frozen=True)
class RegexRule:
    name: str
    pattern: str
    flags: int = re.IGNORECASE | re.MULTILINE
    compiled: Pattern[str] | None = field(default=None, compare=False, repr=False)

    def compile(self) -> Pattern[str]:
        return self.compiled or re.compile(self.pattern, self.flags)

    @classmethod
    def from_payload(
            cls,
            payload: dict[str, Any],
            required_groups: set[str]) -> "RegexRule":
        name = _required_string(payload.get("name"), "name")
        pattern = _required_string(payload.get("pattern"), "pattern")
        flags = _parse_flags(payload.get("flags", ["ignorecase", "multiline"]))
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex rule {name}: {exc}") from exc
        missing = required_groups.difference(compiled.groupindex)
        if missing:
            groups = ", ".join(sorted(missing))
            raise ValueError(f"regex rule {name} is missing required groups: {groups}")
        return cls(name=name, pattern=pattern, flags=flags, compiled=compiled)


@dataclass
class RegexSignalParser:
    signal_rules: list[RegexRule] = field(default_factory=list)
    outcome_rules: list[RegexRule] = field(default_factory=list)

    @classmethod
    def default(cls) -> "RegexSignalParser":
        return cls(
            signal_rules=[
                RegexRule(
                    name="symbol-direction-expiry",
                    pattern=(
                        rf"\b(?P<symbol>{_SYMBOL_PATTERN})\b"
                        rf"{_LOOSE_SEPARATOR}"
                        r"\b(?P<direction>BUY|SELL|CALL|PUT|UP|DOWN)\b"
                        rf"{_LOOSE_SEPARATOR}"
                        r"\b(?P<expiry>\d{1,3})\s*(?P<expiry_unit>s|sec|m|min)?\b"
                    ),
                ),
                RegexRule(
                    name="direction-symbol-expiry",
                    pattern=(
                        r"\b(?P<direction>BUY|SELL|CALL|PUT|UP|DOWN)\b"
                        rf"{_LOOSE_SEPARATOR}"
                        rf"\b(?P<symbol>{_SYMBOL_PATTERN})\b"
                        rf"{_LOOSE_SEPARATOR}"
                        r"\b(?P<expiry>\d{1,3})\s*(?P<expiry_unit>s|sec|m|min)?\b"
                    ),
                ),
            ],
            outcome_rules=[
                RegexRule(
                    name="simple-result",
                    pattern=(
                        r"\b(?P<result>WIN|LOSS|LOSE|TP|SL|PROFIT)\b"
                        rf"(?:{_LOOSE_SEPARATOR}\b(?P<symbol>{_SYMBOL_PATTERN})\b)?"
                    ),
                ),
            ],
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RegexSignalParser":
        signal_rules = _rules_from_payload(payload.get("signal_rules", []), "signal_rules")
        outcome_rules = _rules_from_payload(payload.get("outcome_rules", []), "outcome_rules")
        if not signal_rules and not outcome_rules:
            raise ValueError("parser config must contain at least one rule")
        return cls(signal_rules=signal_rules, outcome_rules=outcome_rules)

    def parse_signal(self, message: RawMessage) -> ParsedSignal | None:
        for rule in self.signal_rules:
            match = rule.compile().search(message.text)
            if not match:
                continue
            symbol = _normalize_symbol(_match_text(match, "symbol"))
            direction = _normalize_direction(_match_text(match, "direction"))
            expiry_ok, expiry = _parse_expiry_seconds(
                match.groupdict().get("expiry"),
                match.groupdict().get("expiry_unit"),
            )
            if not symbol or not direction or not expiry_ok:
                continue
            return ParsedSignal(
                symbol=symbol,
                direction=direction,
                expiry_seconds=expiry,
                name=message.chat_title,
                source_message_identity=message.message_identity,
                source_revision_identity=message.revision_identity,
                parser_rule=rule.name,
            )
        return None

    def parse_outcome(self, message: RawMessage) -> ParsedOutcome | None:
        for rule in self.outcome_rules:
            match = rule.compile().search(message.text)
            if not match:
                continue
            result = _normalize_result(_match_text(match, "result"))
            if not result:
                continue
            symbol = _normalize_symbol(match.groupdict().get("symbol") or "")
            if not symbol:
                symbol = _find_symbol(message.text)
            return ParsedOutcome(
                result=result,
                symbol=symbol,
                reply_to_message_id=message.reply_to_message_id,
                reply_to_message_identity=message.reply_to_message_identity,
                source_message_identity=message.message_identity,
                source_revision_identity=message.revision_identity,
                parser_rule=rule.name,
            )
        return None


def _match_text(match: re.Match[str], group_name: str) -> str:
    value = match.groupdict().get(group_name)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalize_symbol(value: str) -> str:
    return value.upper().replace("/", "").replace("_", "").replace("-", "")


def _find_symbol(value: str) -> str:
    for symbol_match in re.finditer(rf"\b({_SYMBOL_PATTERN})\b", value, re.IGNORECASE):
        candidate = _normalize_symbol(symbol_match.group(1))
        if candidate:
            return candidate
    return ""


def _normalize_direction(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"BUY", "CALL", "UP"}:
        return "BUY"
    if normalized in {"SELL", "PUT", "DOWN"}:
        return "SELL"
    return ""


def _parse_expiry_seconds(value: str | None, unit: str | None) -> tuple[bool, int | None]:
    normalized_unit = (unit or "").strip().lower()
    if value is None:
        return (not normalized_unit), None
    text = value.strip()
    if not text:
        return False, None
    if not normalized_unit:
        normalized_unit = "m"
    if not text.isdecimal():
        return False, None
    amount = int(text)
    if normalized_unit in {"s", "sec"}:
        return True, amount
    if normalized_unit in {"m", "min"}:
        return True, amount * 60
    return False, None


def _normalize_result(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"WIN", "TP", "PROFIT"}:
        return "WIN"
    if normalized in {"LOSS", "LOSE", "SL"}:
        return "LOSS"
    return ""


def _rules_from_payload(value: Any, name: str) -> list[RegexRule]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    rules: list[RegexRule] = []
    required_groups = {"symbol", "direction"} if name == "signal_rules" else {"result"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{name}[{index}] must be an object")
        rules.append(RegexRule.from_payload(item, required_groups))
    return rules


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def _parse_flags(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        flags = value
        if flags < 0 or flags & ~int(_ALLOWED_REGEX_FLAGS):
            raise ValueError("flags contain unsupported regex bits")
        return flags
    if not isinstance(value, list):
        raise ValueError("flags must be an array or integer")

    flags = 0
    for item in value:
        normalized = _required_string(item, "flag").lower()
        if normalized == "ignorecase":
            flags |= re.IGNORECASE
        elif normalized == "multiline":
            flags |= re.MULTILINE
        elif normalized == "dotall":
            flags |= re.DOTALL
        elif normalized == "ascii":
            flags |= re.ASCII
        else:
            raise ValueError(f"unsupported regex flag: {item}")
    return flags
