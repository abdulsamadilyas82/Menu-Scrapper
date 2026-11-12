from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

import httpx

from .models import Restaurant

_BANG = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
_AT = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
_PLACE = re.compile(r"/maps/place/([^/@?]+)")
_SHORT_HOSTS = frozenset({"maps.app.goo.gl", "goo.gl"})


def coords_from_gmaps_url(url: str) -> tuple[float, float, bool] | None:
    """Return (lat, lng, is_exact). `!3d..!4d..` is the place pin itself; `@..` is the
    map centre, which is the place only on /maps/place/ URLs."""
    m = _BANG.search(url)
    if m:
        return float(m.group(1)), float(m.group(2)), True
    m = _AT.search(url)
    if m:
        return float(m.group(1)), float(m.group(2)), "/maps/place/" in url
    return None


def name_from_gmaps_url(url: str) -> str:
    m = _PLACE.search(url)
    return unquote_plus(m.group(1)).strip() if m else ""


def is_short_gmaps_url(url: str) -> bool:
    return (urlparse(url).hostname or "") in _SHORT_HOSTS


class Geocoder:
    """Resolves a restaurant's coordinates: explicit > Google Maps URL > Nominatim.

    Nominatim's usage policy allows at most 1 request/second, so lookups are serialised.
    """

    USER_AGENT = "menu-scrapper-actor/0.1 (Apify Actor)"
    MIN_INTERVAL_S = 1.1

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache: dict[tuple[str, str], tuple[float, float] | None] = {}

    async def expand(self, url: str) -> str:
        """Follow a short link (maps.app.goo.gl) to the full Google Maps URL."""
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
                final = str((await client.get(url)).url)
        except httpx.HTTPError:
            return url
        if "consent.google.com" in final:
            target = parse_qs(urlparse(final).query).get("continue")
            if target:
                return unquote(target[0])
        return final

    async def _nominatim(self, query: str, country: str) -> tuple[float, float] | None:
        key = (query.lower(), country.lower())
        if key in self._cache:
            return self._cache[key]
        async with self._lock:
            wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            params = {"q": query, "format": "jsonv2", "limit": "1"}
            if country:
                params["countrycodes"] = country.lower()
            try:
                async with httpx.AsyncClient(
                    timeout=20, headers={"User-Agent": self.USER_AGENT}
                ) as client:
                    resp = await client.get(
                        "https://nominatim.openstreetmap.org/search", params=params
                    )
                    resp.raise_for_status()
                    hits = resp.json()
            except (httpx.HTTPError, ValueError):
                hits = []
            finally:
                self._last_call = time.monotonic()
        result = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
        self._cache[key] = result
        return result

    async def resolve(self, r: Restaurant) -> None:
        if r.google_maps_url and is_short_gmaps_url(r.google_maps_url):
            r.google_maps_url = await self.expand(r.google_maps_url)
        if r.has_coords:
            r.coords_accuracy = r.coords_accuracy if r.coords_accuracy != "none" else "exact"
            return
        if r.google_maps_url:
            found = coords_from_gmaps_url(r.google_maps_url)
            if found:
                r.latitude, r.longitude = found[0], found[1]
                r.coords_accuracy = "exact" if found[2] else "approx"
                return
        attempts = [
            (", ".join(p for p in (r.name, r.address, r.city) if p), "approx"),
        ]
        if r.address:
            attempts.append((", ".join(p for p in (r.address, r.city) if p), "approx"))
        if r.city:
            attempts.append((r.city, "coarse"))
        for query, accuracy in attempts:
            hit = await self._nominatim(query, r.country)
            if hit:
                r.latitude, r.longitude = hit
                r.coords_accuracy = accuracy
                return
