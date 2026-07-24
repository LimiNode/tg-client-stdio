from __future__ import annotations

import unittest

from tg_client_stdio_worker.backend import RawMessage
from tg_client_stdio_worker.parsing import RegexSignalParser


def raw(text: str, *, reply_to_message_id: int = 0) -> RawMessage:
    return RawMessage(
        chat_id="-10042",
        chat_title="VIP Signals",
        topic_id="0",
        message_id=77,
        date_ms=1784830000000,
        reply_to_message_id=reply_to_message_id,
        text=text,
    )


class RegexSignalParserTest(unittest.TestCase):
    def test_parses_symbol_direction_and_expiry(self) -> None:
        parser = RegexSignalParser.default()

        signal = parser.parse_signal(raw("EUR/USD BUY expiry 5 min"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.symbol, "EURUSD")
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.expiry_seconds, 300)
        self.assertEqual(signal.name, "VIP Signals")
        self.assertEqual(signal.source_message_identity, "telegram:-10042:0:77")

    def test_parses_direction_before_symbol(self) -> None:
        parser = RegexSignalParser.default()

        signal = parser.parse_signal(raw("PUT GBPJPY 60 sec"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.symbol, "GBPJPY")
        self.assertEqual(signal.direction, "SELL")
        self.assertEqual(signal.expiry_seconds, 60)

    def test_parses_multiline_signal(self) -> None:
        parser = RegexSignalParser.default()

        signal = parser.parse_signal(raw("EURUSD\nBUY\n5m"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.symbol, "EURUSD")
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.expiry_seconds, 300)

    def test_does_not_treat_generic_signal_word_as_symbol(self) -> None:
        parser = RegexSignalParser.default()

        self.assertIsNone(parser.parse_signal(raw("SIGNAL BUY 5m")))

    def test_parses_simple_outcome(self) -> None:
        parser = RegexSignalParser.default()

        outcome = parser.parse_outcome(raw("WIN EURUSD"))

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.result, "WIN")
        self.assertEqual(outcome.symbol, "EURUSD")
        self.assertEqual(outcome.source_revision_identity, "telegram:-10042:0:77:0")

    def test_outcome_preserves_reply_correlation(self) -> None:
        parser = RegexSignalParser.default()

        outcome = parser.parse_outcome(raw("WIN EURUSD", reply_to_message_id=76))

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.reply_to_message_id, 76)
        self.assertEqual(
            outcome.reply_to_message_identity,
            "telegram:-10042:0:76",
        )

    def test_outcome_symbol_ignores_leading_status_words(self) -> None:
        parser = RegexSignalParser.default()

        outcome = parser.parse_outcome(raw("RESULT=LOSS EURUSD"))

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.result, "LOSS")
        self.assertEqual(outcome.symbol, "EURUSD")

    def test_outcome_symbol_fallback_is_case_insensitive(self) -> None:
        parser = RegexSignalParser.default()

        outcome = parser.parse_outcome(raw("closed in win eurusd"))

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.result, "WIN")
        self.assertEqual(outcome.symbol, "EURUSD")

    def test_returns_none_for_unmatched_message(self) -> None:
        parser = RegexSignalParser.default()

        self.assertIsNone(parser.parse_signal(raw("hello chat")))
        self.assertIsNone(parser.parse_outcome(raw("hello chat")))

    def test_builds_parser_from_payload(self) -> None:
        parser = RegexSignalParser.from_payload(
            {
                "signal_rules": [
                    {
                        "name": "pair-arrow-expiry",
                        "pattern": (
                            r"PAIR=(?P<symbol>[A-Z]{6})\s+"
                            r"DIR=(?P<direction>CALL|PUT)\s+"
                            r"EXP=(?P<expiry>\d+)(?P<expiry_unit>m)"
                        ),
                        "flags": ["ignorecase"],
                    }
                ],
                "outcome_rules": [
                    {
                        "name": "result-tag",
                        "pattern": r"RESULT=(?P<result>WIN|LOSS)",
                    }
                ],
            }
        )

        signal = parser.parse_signal(raw("pair=EURUSD dir=CALL exp=15m"))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.symbol, "EURUSD")
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.expiry_seconds, 900)
        self.assertEqual(signal.parser_rule, "pair-arrow-expiry")

        outcome = parser.parse_outcome(raw("RESULT=LOSS EURUSD"))
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.result, "LOSS")
        self.assertEqual(outcome.parser_rule, "result-tag")

    def test_rejects_invalid_parser_payload(self) -> None:
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload({})
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload({"signal_rules": [{"name": "bad", "pattern": "["}]})
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload(
                {
                    "signal_rules": [
                        {
                            "name": "bad-flag",
                            "pattern": r"(?P<symbol>EURUSD)",
                            "flags": ["unknown"],
                        }
                    ]
                }
            )
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload(
                {
                    "signal_rules": [
                        {
                            "name": "missing-direction",
                            "pattern": r"(?P<symbol>EURUSD)\s+BUY",
                        }
                    ]
                }
            )
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload(
                {
                    "outcome_rules": [
                        {
                            "name": "missing-result",
                            "pattern": r"WIN",
                        }
                    ]
                }
            )
        with self.assertRaises(ValueError):
            RegexSignalParser.from_payload(
                {
                    "outcome_rules": [
                        {
                            "name": "bad-integer-flags",
                            "pattern": r"(?P<result>WIN)",
                            "flags": 0x40000000,
                        }
                    ]
                }
            )

    def test_optional_unmatched_signal_groups_do_not_crash(self) -> None:
        parser = RegexSignalParser.from_payload(
            {
                "signal_rules": [
                    {
                        "name": "optional-symbol",
                        "pattern": r"(?P<symbol>EURUSD)?\s*(?P<direction>BUY)",
                    },
                    {
                        "name": "optional-direction",
                        "pattern": r"(?P<symbol>EURUSD)\s*(?P<direction>BUY)?",
                    },
                ]
            }
        )

        self.assertIsNone(parser.parse_signal(raw("BUY")))
        self.assertIsNone(parser.parse_signal(raw("EURUSD")))

    def test_optional_unmatched_outcome_group_does_not_crash(self) -> None:
        parser = RegexSignalParser.from_payload(
            {
                "outcome_rules": [
                    {
                        "name": "optional-result",
                        "pattern": r"(?P<result>WIN)?\s*EURUSD",
                    }
                ]
            }
        )

        self.assertIsNone(parser.parse_outcome(raw("EURUSD")))

    def test_invalid_custom_expiry_does_not_crash(self) -> None:
        parser = RegexSignalParser.from_payload(
            {
                "signal_rules": [
                    {
                        "name": "bad-expiry",
                        "pattern": (
                            r"(?P<symbol>EURUSD)\s+"
                            r"(?P<direction>BUY)\s+"
                            r"(?P<expiry>[A-Z]+)"
                        ),
                    },
                    {
                        "name": "bad-expiry-unit",
                        "pattern": (
                            r"(?P<symbol>GBPUSD)\s+"
                            r"(?P<direction>SELL)\s+"
                            r"(?P<expiry>\d+)(?P<expiry_unit>weeks)"
                        ),
                    },
                    {
                        "name": "unit-without-expiry",
                        "pattern": (
                            r"(?P<symbol>USDJPY)\s+"
                            r"(?P<direction>BUY)\s+"
                            r"(?P<expiry>\d+)?"
                            r"(?P<expiry_unit>weeks)"
                        ),
                    },
                ]
            }
        )

        self.assertIsNone(parser.parse_signal(raw("EURUSD BUY soon")))
        self.assertIsNone(parser.parse_signal(raw("GBPUSD SELL 2weeks")))
        self.assertIsNone(parser.parse_signal(raw("USDJPY BUY weeks")))


if __name__ == "__main__":
    unittest.main()
