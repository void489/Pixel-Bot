"""Tests for utils/spawner_math.py — number parsing/formatting for the spawner panel.

These are the functions customers type prices with, so the cases mirror the
real inputs (and the documented refusals) from the module docstring and README.
"""
import pytest

from utils.spawner_math import (
    MAX_AMOUNT, MAX_PRICE, format_both, format_exact, format_short,
    parse_amount, parse_number, parse_price,
)


class TestParseNumber:
    @pytest.mark.parametrize("text,expected", [
        ("64", 64),
        ("1,152", 1152),
        ("100k", 100_000),
        ("1.5m", 1_500_000),
        ("2b", 2_000_000_000),
        ("$8.6M", 8_600_000),
        (" 1 000 ", 1000),
        ("1_000", 1000),
        ("0.5k", 500),
        ("10", 10),
    ])
    def test_accepted_inputs(self, text, expected):
        assert parse_number(text) == expected

    @pytest.mark.parametrize("text", [
        "", None, "   ",
        "0", "-5",
        "0.5",            # whole number required without a suffix
        "8.65",
        "1,5m",           # ambiguous grouping - must refuse, not read as 15m
        "1e10",           # scientific notation is not accepted
        "abc",
        "9" * 31,         # too large
    ])
    def test_refused_inputs(self, text):
        with pytest.raises(ValueError):
            parse_number(text)

    def test_error_messages_are_user_safe(self):
        with pytest.raises(ValueError, match="Enter a number"):
            parse_number(None)
        with pytest.raises(ValueError, match="thousands separators"):
            parse_number("1,5m")
        with pytest.raises(ValueError, match="whole number"):
            parse_number("8.65")


class TestLimits:
    def test_amount_cap(self):
        assert parse_amount(str(MAX_AMOUNT)) == MAX_AMOUNT
        with pytest.raises(ValueError, match="can't be more than"):
            parse_amount(str(MAX_AMOUNT + 1))

    def test_price_cap(self):
        assert parse_price(str(MAX_PRICE)) == MAX_PRICE
        with pytest.raises(ValueError, match="can't be more than"):
            parse_price(str(MAX_PRICE + 1))


class TestFormat:
    def test_format_short(self):
        assert format_short(999) == "999"
        assert format_short(1000) == "1K"
        assert format_short(8_600_000) == "8.6M"
        assert format_short(1_100_800_000) == "1.101B"
        assert format_short(10 ** 12) == "1T"
        assert format_short(-8_600_000) == "-8.6M"

    def test_format_short_rollover(self):
        # 999,999,999 rounds to 1000.000M, shown as 1B instead
        assert format_short(999_999_999) == "1B"

    def test_format_exact(self):
        assert format_exact(1_100_800_000) == "1,100,800,000"
        assert format_exact(42) == "42"

    def test_format_both(self):
        assert format_both(999) == "999"
        assert format_both(1_100_800_000) == "1.101B (1,100,800,000)"

    def test_no_float_rounding_surprises(self):
        # the case from the module docstring: 8.6M * 128 is exactly 1,100,800,000
        assert parse_number("8.6M") * 128 == 1_100_800_000
