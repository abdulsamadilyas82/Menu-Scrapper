from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from apify import Actor

from .config import Config
from .geo import Geocoder
from .gmaps import GoogleMapsMenuScraper
from .http import HttpFactory
from .models import MenuResult, Restaurant
from .partners import FoodpandaPartner, Partner, UberEatsPartner
from .partners.foodpanda import REGIONS
from .sessions import SessionStore

PUSH_CHUNK = 500


@dataclass(slots=True)
class Outcome:
    restaurant: Restaurant
    result: MenuResult | None = None
    tried: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_partners(cfg: Config, proxy_cfg: Any | None) -> list[Partner]:
    http = HttpFactory(proxy_cfg)
    kwargs = {"threshold": cfg.match_threshold, "max_distance_m": cfg.max_distance_m}
    partners: list[Partner] = []
    if "foodpanda" in cfg.partners:
        partners.append(FoodpandaPartner(http, **kwargs))
    if "ubereats" in cfg.partners:
        partners.append(UberEatsPartner(http, **kwargs))
    return partners


async def process_restaurant(
    r: Restaurant,
    *,
    partners: list[Partner],
    gmaps: GoogleMapsMenuScraper | None,
    geocoder: Geocoder,
) -> Outcome:
    out = Outcome(r)
    await geocoder.resolve(r)
    if not r.has_coords:
        out.notes.append("no coordinates (partner lookups need them; add a Google Maps URL or city)")

    # Each source is isolated: one failing (blocked, changed payload) must not stop the next.
    for partner in partners:
        out.tried.append(partner.name)
        try:
            result = await partner.find_menu(r)
        except Exception as exc:  # noqa: BLE001 - boundary: log and fall through to next source
            out.notes.append(f"{partner.name}: {type(exc).__name__}: {exc}")
            Actor.log.warning(f"[{partner.name}] {r.label()}: {type(exc).__name__}: {exc}")
            continue
        if result and result.items:
            out.result = result
            return out

    if gmaps is not None:
        out.tried.append("google_maps")
        try:
            result = await gmaps.extract(r)
        except Exception as exc:  # noqa: BLE001 - same boundary as above
            out.notes.append(f"google_maps: {type(exc).__name__}: {exc}")
            Actor.log.warning(f"[google_maps] {r.label()}: {type(exc).__name__}: {exc}")
        else:
            if result and result.items:
                out.result = result
    return out


def rows_for(outcome: Outcome, scraped_at: str) -> list[dict[str, Any]]:
    res, r = outcome.result, outcome.restaurant
    if res is None:
        return []
    return [
        {
            "restaurant": r.name,
            "address": r.address,
            "city": r.city,
            "country": r.country,
            "source": res.source,
            "sourceUrl": res.source_url,
            "matchedName": res.matched_name,
            "matchScore": res.match_score,
            "category": it.category,
            "name": it.name,
            "description": it.description,
            "price": it.price,
            "currency": it.currency,
            "imageUrl": it.image_url,
            "scrapedAt": scraped_at,
        }
        for it in res.items
    ]


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        try:
            cfg = Config.from_input(raw)
        except ValueError as exc:
            Actor.log.error(str(exc))
            await Actor.fail(status_message=str(exc))
            return

        proxy_cfg = (
            await Actor.create_proxy_configuration(actor_proxy_input=cfg.proxy_input)
            if cfg.proxy_input
            else None
        )
        if proxy_cfg is None:
            Actor.log.warning("No proxy configured: delivery platforms and Google will likely block this run.")

        unsupported = {r.country.lower() for r in cfg.restaurants if r.country} - set(REGIONS)
        if "foodpanda" in cfg.partners and unsupported:
            Actor.log.warning(f"Foodpanda has no regional config for: {sorted(unsupported)} (it will be skipped for those)")

        geocoder = Geocoder()
        partners = build_partners(cfg, proxy_cfg)
        sessions = await SessionStore.load(cfg.gmaps_sessions)
        gmaps = (
            GoogleMapsMenuScraper(
                sessions,
                proxy_cfg,
                concurrency=cfg.gmaps_concurrency,
                debug=cfg.debug,
                headless=os.environ.get("MENU_SCRAPPER_HEADED") != "1",
            )
            if cfg.google_maps_fallback
            else None
        )

        total = len(cfg.restaurants)
        done = 0
        summary: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(cfg.max_concurrency)

        async def handle(r: Restaurant) -> None:
            nonlocal done
            async with sem:
                outcome = await process_restaurant(r, partners=partners, gmaps=gmaps, geocoder=geocoder)
            rows = rows_for(outcome, datetime.now(timezone.utc).isoformat())
            for i in range(0, len(rows), PUSH_CHUNK):
                await Actor.push_data(rows[i : i + PUSH_CHUNK])
            summary.append(
                {
                    "restaurant": r.name,
                    "found": outcome.result is not None,
                    "source": outcome.result.source if outcome.result else None,
                    "matchedName": outcome.result.matched_name if outcome.result else None,
                    "items": len(rows),
                    "tried": outcome.tried,
                    "notes": outcome.notes,
                }
            )
            done += 1
            Actor.log.info(
                f"[{done}/{total}] {r.label()}: "
                + (f"{len(rows)} items via {outcome.result.source}" if outcome.result else "no menu found")
            )
            await Actor.set_status_message(f"{done}/{total} restaurants processed")

        try:
            await asyncio.gather(*(handle(r) for r in cfg.restaurants))
        finally:
            if gmaps is not None:
                await gmaps.close()
            await sessions.save()

        await Actor.set_value("SUMMARY", summary)
        found = sum(1 for s in summary if s["found"])
        await Actor.set_status_message(f"Done: menus found for {found}/{total} restaurants")
