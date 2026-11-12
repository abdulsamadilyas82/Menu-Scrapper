from __future__ import annotations

import math
import re
from typing import NamedTuple


class Price(NamedTuple):
    value: float
    currency: str | None
    explicit: bool  # True when a currency symbol/code appeared in the text


_CODES = frozenset(
    "PKR INR BDT LKR NPR USD GBP EUR AED SAR QAR KWD BHD OMR SGD MYR THB PHP HKD "
    "TWD JPY CNY KRW IDR VND AUD CAD NZD TRY EGP ZAR NGN KES LAK MMK KHR".split()
)
_MARKERS = {
    "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "৳": "BDT", "฿": "THB",
    "₱": "PHP", "¥": "JPY", "₨": "PKR", "S$": "SGD", "HK$": "HKD", "NT$": "TWD",
    "A$": "AUD", "C$": "CAD", "US$": "USD", "RM": "MYR", "TK": "BDT",
}
_DOLLAR_LIKE = frozenset({"USD", "SGD", "HKD", "TWD", "AUD", "CAD", "NZD"})
_RUPEE_LIKE = frozenset({"PKR", "INR", "LKR", "NPR"})
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Currencies without minor units (Uber Eats sends prices in minor units).
ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "IDR", "LAK", "MMK", "KHR"})


def _resolve_marker(marker: str, default: str | None) -> tuple[bool, str | None]:
    """Return (is_valid_marker, currency)."""
    m = marker.strip()
    if not m:
        return True, default
    key = m.upper()
    if m == "$" and default in _DOLLAR_LIKE:
        return True, default
    if m in _MARKERS:
        return True, _MARKERS[m]
    if key in _MARKERS:
        return True, _MARKERS[key]
    if key in _CODES:
        return True, key
    if key in {"RS", "RE"}:
        return True, default if default in _RUPEE_LIKE else "Rs"
    return False, None


def parse_price(text: str, default_currency: str | None = None) -> Price | None:
    """Parse short strings such as 'Rs 1,250', '$12.50', 'S$8', '450'."""
    t = (text or "").replace(" ", " ").strip()
    if not t or len(t) > 20:
        return None
    m = _NUM_RE.search(t)
    if not m:
        return None
    rest = (t[: m.start()] + " " + t[m.end():]).replace(".", "").strip()
    if len(rest) > 5:
        return None
    ok, currency = _resolve_marker(rest, default_currency)
    if not ok:
        return None
    num = m.group()
    if "." not in num and re.fullmatch(r"\d+,\d{1,2}", num):
        num = num.replace(",", ".")  # decimal comma
    else:
        num = num.replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return Price(value, currency, explicit=bool(rest))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
