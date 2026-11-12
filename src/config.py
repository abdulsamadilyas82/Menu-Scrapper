from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .geo import name_from_gmaps_url
from .models import Restaurant

PARTNER_ORDER = ("foodpanda", "ubereats")


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_restaurant(entry: Any, default_country: str) -> Restaurant:
    if isinstance(entry, str):
        text = entry.strip()
        if text.lower().startswith(("http://", "https://")):
            entry = {"googleMapsUrl": text}
        else:
            entry = {"name": text}
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid restaurant entry: {entry!r}")

    url = str(entry.get("googleMapsUrl") or "").strip()
    name = str(entry.get("name") or "").strip() or (name_from_gmaps_url(url) if url else "")
    if not name and not url:
        raise ValueError(f"Restaurant entry needs a name or googleMapsUrl: {entry!r}")

    lat, lng = _float(entry.get("latitude")), _float(entry.get("longitude"))
    r = Restaurant(
        name=name,
        address=str(entry.get("address") or "").strip(),
        city=str(entry.get("city") or "").strip(),
        country=str(entry.get("country") or default_country or "").strip().upper(),
        google_maps_url=url,
        latitude=lat,
        longitude=lng,
    )
    if r.has_coords:
        r.coords_accuracy = "exact"
    return r


@dataclass(slots=True)
class Config:
    restaurants: list[Restaurant]
    partners: list[str]
    google_maps_fallback: bool
    match_threshold: float
    max_distance_m: float
    max_concurrency: int
    gmaps_concurrency: int
    gmaps_sessions: list[dict[str, Any]]
    proxy_input: dict[str, Any] | None
    debug: bool

    @classmethod
    def from_input(cls, raw: dict[str, Any]) -> "Config":
        entries = raw.get("restaurants")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Input 'restaurants' must be a non-empty array.")
        default_country = str(raw.get("country") or "").strip().upper()
        selected = set(raw.get("partners") or PARTNER_ORDER)
        unknown = selected - set(PARTNER_ORDER)
        if unknown:
            raise ValueError(f"Unknown partners: {sorted(unknown)}")
        sessions = raw.get("gmapsSessions") or []
        if not isinstance(sessions, list):
            raise ValueError("Input 'gmapsSessions' must be an array of {id, cookies} objects.")
        return cls(
            restaurants=[parse_restaurant(e, default_country) for e in entries],
            partners=[p for p in PARTNER_ORDER if p in selected],
            google_maps_fallback=bool(raw.get("googleMapsFallback", True)),
            match_threshold=float(raw.get("matchThreshold", 80)),
            max_distance_m=float(raw.get("maxDistanceMeters", 500)),
            max_concurrency=int(raw.get("maxConcurrency", 5)),
            gmaps_concurrency=int(raw.get("maxGoogleMapsConcurrency", 2)),
            gmaps_sessions=sessions,
            proxy_input=raw.get("proxyConfiguration"),
            debug=bool(raw.get("debug", False)),
        )
