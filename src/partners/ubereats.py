"""Uber Eats extractor.

Uses the web app's internal `/_p/api/*` endpoints. Like the Foodpanda extractor, the
request bodies and the `uev2.loc` location cookie were written from the public web app's
behaviour and are NOT validated against live traffic from this build environment. Uber
Eats is also bot-protected, so expect to need residential proxies; failures raise
PartnerError and the Actor simply moves on to the next source.
"""
from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import quote

from apify import Actor

from ..matching import Candidate, rank_candidates, within_distance
from ..models import MenuItem, MenuResult, Restaurant
from ..utils import ZERO_DECIMAL
from .base import Partner
from ..http import HttpFactory, request_json

BASE = "https://www.ubereats.com/_p/api"
MAX_STORES_TO_VERIFY = 3


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def iter_stores(node: Any) -> Iterator[dict]:
    """Yield dicts that carry a `storeUuid` and a title anywhere in a feed payload."""
    if isinstance(node, dict):
        if isinstance(node.get("storeUuid"), str) and node.get("title"):
            yield node
            return
        for child in node.values():
            yield from iter_stores(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_stores(child)


def parse_store(data: dict) -> list[MenuItem]:
    currency = data.get("currencyCode")
    divisor = 1 if currency in ZERO_DECIMAL else 100
    items: list[MenuItem] = []
    seen: set[tuple[str, str, float | None]] = set()
    for section in (data.get("catalogSectionsMap") or {}).values():
        for entry in section or []:
            payload = (entry.get("payload") or {}).get("standardItemsPayload") or {}
            category = _text(payload.get("title"))
            for it in payload.get("catalogItems") or []:
                name = _text(it.get("title"))
                if not name:
                    continue
                raw_price = it.get("price")
                price = raw_price / divisor if isinstance(raw_price, (int, float)) else None
                key = (category, name, price)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    MenuItem(
                        name=name,
                        price=price,
                        currency=currency,
                        category=category,
                        description=_text(it.get("itemDescription")),
                        image_url=str(it.get("imageUrl") or ""),
                    )
                )
    return items


def location_cookie(r: Restaurant) -> str:
    loc = {
        "address": {
            "address1": r.address or r.name,
            "city": r.city,
            "country": (r.country or "").upper(),
            "title": r.address or r.name,
        },
        "latitude": r.latitude,
        "longitude": r.longitude,
        "reference": "",
        "referenceType": "google_places",
        "type": "google_places",
        "addressComponents": {"city": r.city, "countryCode": (r.country or "").upper()},
        "originType": "user_autocomplete",
    }
    return quote(json.dumps(loc, separators=(",", ":")))


class UberEatsPartner(Partner):
    name = "ubereats"

    def __init__(self, http: HttpFactory, *, threshold: float, max_distance_m: float) -> None:
        self._http = http
        self._threshold = threshold
        self._max_distance_m = max_distance_m

    async def find_menu(self, r: Restaurant) -> MenuResult | None:
        if not r.has_coords:
            Actor.log.info(f"[ubereats] skipping {r.label()}: no coordinates")
            return None

        headers = {
            "x-csrf-token": "x",
            "Content-Type": "application/json",
            "Origin": "https://www.ubereats.com",
            "Referer": "https://www.ubereats.com/",
        }
        client_kwargs = {"cookies": {"uev2.loc": location_cookie(r)}}

        feed = await request_json(
            self._http,
            "POST",
            f"{BASE}/getSearchFeedV1?localeCode=en",
            json={"userQuery": r.name, "date": "", "startTime": 0, "endTime": 0, "vertical": "ALL"},
            headers=headers,
            client_kwargs=client_kwargs,
        )
        stores: dict[str, dict] = {}
        for s in iter_stores(feed):
            stores.setdefault(s["storeUuid"], s)
        candidates = [Candidate(name=_text(s["title"]), payload=s) for s in stores.values()]
        ranked = rank_candidates(r, candidates, self._threshold, self._max_distance_m)
        if not ranked:
            Actor.log.info(f"[ubereats] no match for {r.label()} among {len(stores)} stores")
            return None

        # The search feed has no coordinates, so verify distance on the store payload.
        for cand, score in ranked[:MAX_STORES_TO_VERIFY]:
            uuid = cand.payload["storeUuid"]
            store = await request_json(
                self._http,
                "POST",
                f"{BASE}/getStoreV1?localeCode=en",
                json={
                    "storeUuid": uuid,
                    "diningMode": "DELIVERY",
                    "time": {"asap": True},
                    "cbType": "EATER_ENDORSED",
                },
                headers=headers,
                client_kwargs=client_kwargs,
            )
            data = (store or {}).get("data") if isinstance(store, dict) else None
            if not isinstance(data, dict):
                continue
            loc = data.get("location") or {}
            if not within_distance(r, loc.get("latitude"), loc.get("longitude"), self._max_distance_m):
                continue
            items = parse_store(data)
            if not items:
                continue
            slug = data.get("slug")
            url = f"https://www.ubereats.com/store/{slug}/{uuid}" if slug else f"https://www.ubereats.com/store/{uuid}"
            return MenuResult(
                source=self.name,
                source_url=url,
                matched_name=cand.name,
                match_score=round(score, 1),
                items=items,
            )
        Actor.log.info(f"[ubereats] candidates for {r.label()} failed distance/menu checks")
        return None
