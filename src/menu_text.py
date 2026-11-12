"""Heuristic parser that turns the visible text of a Google Maps Menu panel into items.

Assumed layout (one block per item, price last):

    BURGERS                  <- optional all-caps / trailing-colon heading
    Zinger Burger            <- first line of a block is the name
    Crispy chicken, mayo     <- remaining lines are the description
    Rs 450                   <- a price line closes the block

This is the least certain part of the Actor because Google changes the Maps DOM often.
Run with debug=true to capture the raw panel text, then adjust here if the layout differs.
"""
from __future__ import annotations

import re

from .models import MenuItem
from .utils import parse_price

_NOISE = re.compile(
    r"^(overview|menu|reviews?|about|photos?|see (more|all)|more|popular|add photo|"
    r"suggest an edit|share|save|nearby|send to phone|[·•\-–—])$",
    re.I,
)
_INLINE = re.compile(
    r"^(?P<text>.+?)\s*[·•|:–—-]\s*"
    r"(?P<price>(?:[A-Za-z]{1,3}\.?\s?|[$£€₹৳฿₱¥₨])\s?\d[\d,]*(?:\.\d+)?)$"
)


def _split_inline(line: str) -> list[str]:
    """'Zinger Burger · Rs 450' -> ['Zinger Burger', 'Rs 450']."""
    m = _INLINE.match(line)
    if m and parse_price(m.group("price")) is not None and parse_price(line) is None:
        return [m.group("text").strip(), m.group("price").strip()]
    return [line]


def _is_heading(line: str) -> bool:
    if len(line) > 40:
        return False
    if line.endswith(":"):
        return True
    return line.isupper() and not any(ch.isdigit() for ch in line)


def parse_menu_text(text: str, default_currency: str | None = None) -> list[MenuItem]:
    lines: list[str] = []
    for raw in text.replace(" ", " ").splitlines():
        line = raw.strip()
        if line and not _NOISE.match(line):
            lines.extend(_split_inline(line))

    prices = [parse_price(ln, default_currency) for ln in lines]
    # If any line carries an explicit currency, bare numbers (ratings, counts) are not prices.
    require_marker = any(p is not None and p.explicit for p in prices)

    items: list[MenuItem] = []
    seen: set[tuple[str, str, float]] = set()
    block: list[str] = []
    category = ""
    for line, price in zip(lines, prices):
        is_price = price is not None and (price.explicit or not require_marker)
        if not is_price:
            if not block and _is_heading(line):
                category = line.rstrip(":").strip()
            else:
                block.append(line)
            continue
        if block:
            key = (category, block[0], price.value)  # type: ignore[union-attr]
            if key not in seen:
                seen.add(key)
                items.append(
                    MenuItem(
                        name=block[0],
                        description=" ".join(block[1:]),
                        price=price.value,  # type: ignore[union-attr]
                        currency=price.currency,  # type: ignore[union-attr]
                        category=category,
                    )
                )
        block = []
    return items
