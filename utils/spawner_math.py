"""Number parsing + formatting for the spawner system.

Accepts what people actually type: `64`, `1,152`, `100k`, `1.5m`, `2b`, `$8.6M`.
Everything is done with Decimal / int so there are no float rounding surprises
(8.6M * 128 is exactly 1,100,800,000).

Pure functions, no Discord or Mongo imports, so they can be tested on their own.
"""
import re
from decimal import Decimal, ROUND_HALF_UP

SUFFIXES = {"k": 10**3, "m": 10**6, "b": 10**9}

# Hard caps. Mongo stores ints as 64-bit, and MAX_PRICE * MAX_AMOUNT (1e17) fits.
MAX_AMOUNT = 100_000        # spawners in a single order
MAX_PRICE = 10**12          # per spawner (1T)

_PLAIN = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)([kmb])?$")
_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?[kmb]?$")

_EXAMPLES = "`64`, `1k`, `2.5m`, `1b`"


def parse_number(text) -> int:
    """Parse a user-typed number into a positive whole int.

    Raises ValueError with a message that is safe to show to the user.
    """
    s = re.sub(r"\s+", "", str(text or "")).lower()
    if s.startswith("$"):
        s = s[1:]
    s = s.replace("_", "")

    if not s:
        raise ValueError(f"Enter a number, e.g. {_EXAMPLES}.")
    if len(s) > 30:
        raise ValueError("That number is too large.")

    if "," in s:
        # "1,152" is thousands grouping. "1,5m" is ambiguous (1.5m in much of Europe),
        # and silently reading it as 15m would be a costly mistake, so refuse it.
        if not _GROUPED.match(s):
            raise ValueError(
                "Use commas only as thousands separators (`1,000`) "
                "and a dot for decimals (`1.5m`)."
            )
        s = s.replace(",", "")

    m = _PLAIN.match(s)
    if not m:
        raise ValueError(f"I couldn't read that as a number. Try something like {_EXAMPLES}.")

    value = Decimal(m.group(1)) * SUFFIXES.get(m.group(2), 1)
    if value != value.to_integral_value():
        raise ValueError(
            "That isn't a whole number. Decimals are fine with a suffix "
            "(`1.5k` = 1,500), but `0.5` or `8.65` on its own is not."
        )
    n = int(value)
    if n < 1:
        raise ValueError("The number has to be at least 1.")
    return n


def parse_amount(text) -> int:
    """How many spawners. Whole number, 1..MAX_AMOUNT."""
    n = parse_number(text)
    if n > MAX_AMOUNT:
        raise ValueError(f"The amount can't be more than {MAX_AMOUNT:,} per ticket.")
    return n


def parse_price(text) -> int:
    """Price per spawner. Whole number, 1..MAX_PRICE."""
    n = parse_number(text)
    if n > MAX_PRICE:
        raise ValueError(f"The price can't be more than {format_short(MAX_PRICE)} per spawner.")
    return n


# Largest first. T is only here so huge totals don't show as "1000000B".
_UNITS = [(10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")]
_Q = Decimal("0.001")


def format_short(n) -> str:
    """Compact form with up to 3 decimals: 8600000 -> 8.6M, 1100800000 -> 1.101B."""
    n = int(n)
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a < 1000:
        return f"{sign}{a}"

    idx = next(i for i, (div, _) in enumerate(_UNITS) if a >= div)
    div, suffix = _UNITS[idx]
    q = (Decimal(a) / div).quantize(_Q, rounding=ROUND_HALF_UP)
    if q >= 1000 and idx > 0:
        # 999,999,999 rounds to 1000.000M - show it as 1B instead
        div, suffix = _UNITS[idx - 1]
        q = (Decimal(a) / div).quantize(_Q, rounding=ROUND_HALF_UP)
    return f"{sign}{format(q.normalize(), 'f')}{suffix}"


def format_exact(n) -> str:
    """Full number with thousands separators: 1,100,800,000."""
    return f"{int(n):,}"


def format_both(n) -> str:
    """`1.101B (1,100,800,000)`; just the plain number below 1,000."""
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    return f"{format_short(n)} ({format_exact(n)})"
