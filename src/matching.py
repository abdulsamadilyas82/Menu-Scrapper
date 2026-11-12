from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from .models import Restaurant
from .utils import haversine_m

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_STOP = frozenset(
    "the restaurant restaurants resturant cafe cafes and bar grill kitchen pvt ltd llc co branch".split()
)
_GATE_FACTOR = {"exact": 1.0, "approx": 4.0}  # "coarse"/"none" disable the distance gate


@dataclass(slots=True)
class Candidate:
    name: str
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    payload: Any = None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = _NON_ALNUM.sub(" ", text.replace("&", " and "))
    words = text.split()
    kept = [w for w in words if w not in _STOP]
    return " ".join(kept or words)


def name_score(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    # token_set_ratio treats "kfc" vs "kfc gulberg" as a perfect match, which is right for
    # branch suffixes but slightly over-generous, so it is discounted.
    return max(fuzz.token_sort_ratio(na, nb), 0.92 * fuzz.token_set_ratio(na, nb))


def within_distance(r: Restaurant, lat: float | None, lng: float | None, max_distance_m: float) -> bool:
    factor = _GATE_FACTOR.get(r.coords_accuracy)
    if factor is None or not r.has_coords or lat is None or lng is None:
        return True
    return haversine_m(r.latitude, r.longitude, lat, lng) <= max_distance_m * factor  # type: ignore[arg-type]


def score_candidate(r: Restaurant, c: Candidate, max_distance_m: float) -> float:
    score = name_score(r.name, c.name)
    if score == 0.0:
        return 0.0
    if r.address and c.address:
        addr = fuzz.token_set_ratio(normalize(r.address), normalize(c.address))
        score = 0.75 * score + 0.25 * addr
    if not within_distance(r, c.lat, c.lng, max_distance_m):
        return 0.0
    return score


def rank_candidates(
    r: Restaurant, candidates: list[Candidate], threshold: float, max_distance_m: float
) -> list[tuple[Candidate, float]]:
    scored = [(c, score_candidate(r, c, max_distance_m)) for c in candidates]
    return sorted(((c, s) for c, s in scored if s >= threshold), key=lambda cs: -cs[1])


def best_match(
    r: Restaurant, candidates: list[Candidate], threshold: float, max_distance_m: float
) -> tuple[Candidate, float] | None:
    ranked = rank_candidates(r, candidates, threshold, max_distance_m)
    return ranked[0] if ranked else None
