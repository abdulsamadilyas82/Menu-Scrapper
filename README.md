# menu-scrapper

Apify Actor (Python) that extracts restaurant menus.

1. Resolve the restaurant's coordinates (explicit input → Google Maps URL → Nominatim geocoding).
2. Look for it on delivery partners, **Foodpanda first, then Uber Eats**. A listing only counts if its
   name fuzzy-matches (`matchThreshold`) **and** it sits within `maxDistanceMeters` of the restaurant,
   so chain restaurants don't match the wrong branch.
3. If a partner has it, the menu comes from the partner's JSON.
4. Otherwise open the place on Google Maps with Playwright, using stored Google sessions (cookies) to get
   past the login wall, open the **Menu** tab, scroll it fully, and parse the items.

## Input

```json
{
  "restaurants": [
    "https://maps.app.goo.gl/XXXXXXXX",
    { "name": "Example Restaurant", "address": "Main Street", "city": "Example City" }
  ],
  "country": "PK",
  "partners": ["foodpanda", "ubereats"],
  "googleMapsFallback": true,
  "gmapsSessions": [{ "id": "account-1", "cookies": [ { "name": "SID", "value": "...", "domain": ".google.com" } ] }],
  "proxyConfiguration": { "useApifyProxy": true, "apifyProxyGroups": ["RESIDENTIAL"] }
}
```

A Google Maps URL is the best input: it gives exact coordinates for the distance gate and lets the
fallback open the exact place instead of searching.

## Output

Dataset: one row per menu item: `restaurant, source, sourceUrl, matchedName, matchScore, category, name,
description, price, currency, imageUrl, scrapedAt`. Key-value store record `SUMMARY`: per restaurant,
whether a menu was found, which sources were tried, and failure notes.

## Google sessions

* Export cookies (JSON) from a browser where you are signed in to a Google account **you own** and paste them in `gmapsSessions`. Several sessions are rotated round-robin.
* Sessions are kept in the private named key-value store `menu-scrapper-sessions`; refreshed cookies are saved back after each run. Supplying different cookies for the same `id` replaces the stored ones.
* A session that hits a login wall rests for 6 hours. Cookie values are never logged.
* Automated access to Google Maps can conflict with Google's Terms of Service and can get the account restricted. Use a dedicated account and keep `maxGoogleMapsConcurrency` low.

## What is verified and what is not

Unit tests cover the pure logic (price parsing, fuzzy matching and distance gating, menu-text parsing,
payload parsers, input parsing). The request shapes for Foodpanda, Uber Eats and the Google Maps DOM
flow were written from the public web apps' known behaviour and have **not** been run against the live
sites. Expect to adjust them on first real run:

| Piece | Where to adjust | How to debug |
|---|---|---|
| Foodpanda search/vendor calls | `src/partners/foodpanda.py` (`SEARCH_URL`, `_search`, `_fetch_vendor`) | Compare with the Network tab on the Foodpanda site |
| Uber Eats calls + location cookie | `src/partners/ubereats.py` | Same; often needs residential proxies |
| Google Maps selectors | `src/gmaps.py` (`_open_menu_tab`, `_scroll_panel`, `_panel_text`) | Run with `debug: true`; screenshots + page text land in the key-value store |
| Menu text layout | `src/menu_text.py` | Feed the saved `debug-*.txt` to `parse_menu_text` |

## Develop

```bash
pip install -r requirements.txt pytest && playwright install chromium
python -m pytest tests            # unit tests
apify run                         # needs storage/key_value_stores/default/INPUT.json
MENU_SCRAPPER_HEADED=1 apify run  # watch the Google Maps browser locally
apify push                        # deploy to the Apify platform
```
