from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern

from .backend import RawMessage

_ASSET_CODE = r"(?:EUR|USD|GBP|JPY|AUD|CAD|CHF|NZD|XAU|XAG|BTC|ETH)"
_SYMBOL_PATTERN = rf"{_ASSET_CODE}(?:[/_-]?{_ASSET_CODE})"
_LOOSE_SEPARATOR = r"[\s\S]{0,80}?"


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

    def compile(self) -> Pattern[str]:
        return re.compile(self.pattern, self.flags)


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

    def parse_signal(self, message: RawMessage) -> ParsedSignal | None:
        for rule in self.signal_rules:
            match = rule.compile().search(message.text)
            if not match:
                continue
            symbol = _normalize_symbol(match.group("symbol"))
            direction = _normalize_direction(match.group("direction"))
            expiry = _parse_expiry_seconds(
                match.groupdict().get("expiry"),
                match.groupdict().get("expiry_unit"),
            )
            if not symbol or not direction:
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
            result = _normalize_result(match.group("result"))
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


def _parse_expiry_seconds(value: str | None, unit: str | None) -> int | None:
    if not value:
        return None
    amount = int(value)
    normalized_unit = (unit or "m").strip().lower()
    if normalized_unit in {"s", "sec"}:
        return amount
    return amount * 60


def _normalize_result(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"WIN", "TP", "PROFIT"}:
        return "WIN"
    if normalized in {"LOSS", "LOSE", "SL"}:
        return "LOSS"
    return ""
