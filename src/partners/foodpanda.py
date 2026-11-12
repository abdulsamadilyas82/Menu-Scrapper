"""Foodpanda extractor.

Foodpanda's web app talks to the Delivery Hero "disco" search API and the per-region
`fd-api.com` vendor API. The request and response shapes below were written from the
public web app's behaviour and are NOT validated against live traffic from this build
environment. If a call starts returning 4xx or unexpected JSON, open the Foodpanda site
in DevTools (Network tab), find the equivalent request, and adjust SEARCH_* / VENDOR_*.
Parsing is deliberately defensive (it walks the JSON rather than assuming exact paths).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from apify import Actor

from ..errors import PartnerError
from ..http import HttpFactory, request_json
from ..matching import Candidate, best_match
from ..models import MenuItem, MenuResult, Restaurant
from .base import Partner

SEARCH_URL = "https://disco.deliveryhero.io/search/api/v1/feed"
VENDOR_URL = "https://{api_host}/api/v5/vendors/{code}"


@dataclass(frozen=True, slots=True)
class Region:
    code: str
    api_host: str
    domain: str
    currency: str
    language_id: int = 1


REGIONS: dict[str, Region] = {
    r.code: r
    for r in (
        Region("pk", "pk.fd-api.com", "www.foodpanda.pk", "PKR"),
        Region("bd", "bd.fd-api.com", "www.foodpanda.com.bd", "BDT"),
        Region("sg", "sg.fd-api.com", "www.foodpanda.sg", "SGD"),
        Region("my", "my.fd-api.com", "www.foodpanda.my", "MYR"),
        Region("ph", "ph.fd-api.com", "www.foodpanda.ph", "PHP"),
        Region("th", "th.fd-api.com", "www.foodpanda.co.th", "THB"),
        Region("hk", "hk.fd-api.com", "www.foodpanda.hk", "HKD"),
        Region("tw", "tw.fd-api.com", "www.foodpanda.com.tw", "TWD"),
        Region("kh", "kh.fd-api.com", "www.foodpanda.com.kh", "USD"),
        Region("la", "la.fd-api.com", "www.foodpanda.la", "LAK"),
        Region("mm", "mm.fd-api.com", "www.foodpanda.com.mm", "MMK"),
    )
}


def _coord(vendor: dict, key: str) -> float | None:
    val = vendor.get(key)
    if val is None and isinstance(vendor.get("location"), dict):
        val = vendor["location"].get(key)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def iter_vendors(node: Any) -> Iterator[dict]:
    """Yield vendor-like dicts (string `code` + `name`, plus a location hint) anywhere in a payload."""
    if isinstance(node, dict):
        if (
            isinstance(node.get("code"), str)
            and isinstance(node.get("name"), str)
            and ("latitude" in node or "address" in node or "location" in node)
        ):
            yield node
            return
        for child in node.values():
            yield from iter_vendors(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_vendors(child)


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_menu(payload: dict, region: Region) -> list[MenuItem]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items: list[MenuItem] = []
    seen: set[tuple[str, str, float | None]] = set()
    for menu in data.get("menus") or []:
        for cat in menu.get("menu_categories") or []:
            cat_name = (cat.get("name") or "").strip()
            for prod in cat.get("products") or []:
                base = (prod.get("name") or "").strip()
                if not base:
                    continue
                variations = prod.get("product_variations") or [{}]
                images = prod.get("images") or [{}]
                image = prod.get("file_path") or (images[0] or {}).get("image_url") or ""
                for var in variations:
                    vname = (var.get("name") or "").strip()
                    name = f"{base} ({vname})" if vname and len(variations) > 1 else base
                    price = _to_float(var.get("price"))
                    key = (cat_name, name, price)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(
                        MenuItem(
                            name=name,
                            price=price,
                            currency=region.currency,
                            category=cat_name,
                            description=(prod.get("description") or "").strip(),
                            image_url=image,
                        )
                    )
    return items


class FoodpandaPartner(Partner):
    name = "foodpanda"

    def __init__(self, http: HttpFactory, *, threshold: float, max_distance_m: float) -> None:
        self._http = http
        self._threshold = threshold
        self._max_distance_m = max_distance_m

    def _headers(self, region: Region) -> dict[str, str]:
        return {
            "x-fp-api-key": "volo",
            "x-disco-client-id": "web",
            "Origin": f"https://{region.domain}",
            "Referer": f"https://{region.domain}/",
        }

    async def _search(self, r: Restaurant, region: Region) -> list[dict]:
        body = {
            "q": r.name,
            "location": {"point": {"latitude": r.latitude, "longitude": r.longitude}},
            "config": "Variant1",
            "vertical_types": ["restaurants"],
            "include_component_types": ["vendors"],
            "include_fields": ["feed"],
            "language_code": "en",
            "language_id": str(region.language_id),
            "country_code": region.code.upper(),
            "customer_type": "regular",
            "brand": "foodpanda",
            "opening_type": "delivery",
            "platform": "web",
            "dynamic_pricing": 0,
            "limit": 50,
            "offset": 0,
            "session_id": uuid.uuid4().hex,
        }
        payload = await request_json(
            self._http, "POST", SEARCH_URL, json=body, headers=self._headers(region)
        )
        vendors: dict[str, dict] = {}
        for v in iter_vendors(payload):
            vendors.setdefault(v["code"], v)
        return list(vendors.values())

    async def _fetch_vendor(self, code: str, r: Restaurant, region: Region) -> dict | None:
        params = {
            "include": "menus",
            "language_id": region.language_id,
            "opening_type": "delivery",
            "basket_currency": region.currency,
            "latitude": r.latitude,
            "longitude": r.longitude,
        }
        return await request_json(
            self._http,
            "GET",
            VENDOR_URL.format(api_host=region.api_host, code=code),
            params=params,
            headers=self._headers(region),
        )

    async def find_menu(self, r: Restaurant) -> MenuResult | None:
        region = REGIONS.get((r.country or "").lower())
        if region is None:
            Actor.log.info(f"[foodpanda] no regional API configured for country '{r.country}'")
            return None
        if not r.has_coords:
            Actor.log.info(f"[foodpanda] skipping {r.label()}: no coordinates")
            return None

        vendors = await self._search(r, region)
        candidates = [
            Candidate(
                name=v["name"],
                address=str(v.get("address") or ""),
                lat=_coord(v, "latitude"),
                lng=_coord(v, "longitude"),
                payload=v,
            )
            for v in vendors
        ]
        match = best_match(r, candidates, self._threshold, self._max_distance_m)
        if match is None:
            Actor.log.info(f"[foodpanda] no match for {r.label()} among {len(vendors)} vendors")
            return None
        cand, score = match
        code = cand.payload["code"]
        payload = await self._fetch_vendor(code, r, region)
        items = parse_menu(payload, region) if isinstance(payload, dict) else []
        if not items:
            Actor.log.info(f"[foodpanda] vendor {code} matched but has no menu items")
            return None
        return MenuResult(
            source=self.name,
            source_url=f"https://{region.domain}/restaurant/{code}",
            matched_name=cand.name,
            match_score=round(score, 1),
            items=items,
        )
