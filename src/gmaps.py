"""Google Maps menu fallback (Playwright + stored Google sessions).

The Maps UI changes often and this flow was NOT validated against the live site from the
build environment. Selectors are role/text based to be as stable as possible; run with
`debug: true` to save a screenshot and the raw panel text for each step so they can be
tuned quickly.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

from apify import Actor
from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .errors import LoginWallError
from .matching import name_score
from .menu_text import parse_menu_text
from .models import MenuResult, Restaurant
from .sessions import GoogleSession, SessionStore, only_google_cookies

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
MAX_SCROLLS = 40

_SCROLL_JS = """
() => {
  let best = null, bestH = 0;
  for (const el of document.querySelectorAll('div')) {
    if (el.clientHeight < 200) continue;
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20
        && el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
  }
  if (!best) return 0;
  best.scrollTo(0, best.scrollHeight);
  return best.scrollHeight;
}
"""


def _playwright_proxy(url: str) -> dict[str, str]:
    p = urlparse(url)
    proxy = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = unquote(p.username)
    if p.password:
        proxy["password"] = unquote(p.password)
    return proxy


def _with_english(url: str) -> str:
    if "hl=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "hl=en"


async def _block_heavy(route: Any) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


class GoogleMapsMenuScraper:
    def __init__(
        self,
        sessions: SessionStore,
        proxy_cfg: Any | None,
        *,
        concurrency: int = 2,
        debug: bool = False,
        default_currency: str | None = None,
        name_threshold: float = 70.0,
        max_attempts: int = 3,
        headless: bool = True,
    ) -> None:
        self._sessions = sessions
        self._proxy_cfg = proxy_cfg
        self._sem = asyncio.Semaphore(concurrency)
        self._debug = debug
        self._default_currency = default_currency
        self._name_threshold = name_threshold
        self._max_attempts = max_attempts
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        async with self._start_lock:
            if self._browser is None:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=self._headless)
        return self._browser

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def extract(self, r: Restaurant) -> MenuResult | None:
        async with self._sem:
            for attempt in range(1, self._max_attempts + 1):
                session = self._sessions.acquire()
                if session is None:
                    Actor.log.warning("[gmaps] no healthy Google session available")
                    return None
                try:
                    # None = the place has no menu / no match: final, don't burn more sessions.
                    return await self._extract_with(r, session)
                except LoginWallError:
                    self._sessions.mark_bad(session, "login wall")
                except PlaywrightError as exc:
                    Actor.log.warning(
                        f"[gmaps] {r.label()} attempt {attempt}/{self._max_attempts}: "
                        f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
                    )
            return None

    async def _extract_with(self, r: Restaurant, session: GoogleSession) -> MenuResult | None:
        browser = await self._ensure_browser()
        proxy = None
        if self._proxy_cfg:
            url = await self._proxy_cfg.new_url(session_id=session.proxy_session_id)
            proxy = _playwright_proxy(url)

        context = await browser.new_context(
            proxy=proxy, locale="en-US", viewport={"width": 1366, "height": 900}
        )
        try:
            if session.cookies:
                await context.add_cookies(session.cookies)  # type: ignore[arg-type]
            page = await context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            if not self._debug:
                await page.route("**/*", _block_heavy)

            from_url = bool(r.google_maps_url)
            start = r.google_maps_url or f"https://www.google.com/maps/search/{quote_plus(r.search_text)}"
            await page.goto(_with_english(start), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await self._handle_consent(page)
            self._raise_on_login(page)

            if "/maps/place/" not in page.url:
                if not await self._open_first_result(page):
                    Actor.log.info(f"[gmaps] no search results for {r.label()}")
                    await self._dump(page, r, "no-results")
                    return None

            title = await self._place_title(page)
            if not title:
                await self._dump(page, r, "no-title")
                return None
            score = 100.0 if from_url else name_score(r.name, title)
            if score < self._name_threshold:
                Actor.log.info(f"[gmaps] top result '{title}' doesn't match '{r.name}' ({score:.0f})")
                return None

            if not await self._open_menu_tab(page):
                Actor.log.info(f"[gmaps] '{title}' has no Menu tab")
                await self._dump(page, r, "no-menu-tab")
                return None
            await self._scroll_panel(page)
            self._raise_on_login(page)
            await self._dump(page, r, "menu")

            text = await self._panel_text(page)
            items = parse_menu_text(text, self._default_currency)
            if not items:
                Actor.log.info(f"[gmaps] '{title}': Menu tab opened but no items parsed")
                return None

            self._sessions.update_cookies(session, only_google_cookies(await context.cookies()))
            return MenuResult(
                source="google_maps",
                source_url=page.url,
                matched_name=title,
                match_score=round(score, 1),
                items=items,
            )
        finally:
            await context.close()

    # --- page helpers -------------------------------------------------------------

    @staticmethod
    def _raise_on_login(page: Page) -> None:
        if "accounts.google.com" in page.url:
            raise LoginWallError(page.url)

    async def _handle_consent(self, page: Page) -> None:
        if "consent.google.com" not in page.url:
            return
        for pattern in (r"reject all", r"accept all", r"i agree"):
            button = page.get_by_role("button", name=re.compile(pattern, re.I))
            if await button.count():
                await button.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return

    async def _open_first_result(self, page: Page) -> bool:
        link = page.locator('a[href*="/maps/place/"]').first
        try:
            await link.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            return "/maps/place/" in page.url
        await link.click()
        await page.wait_for_url(re.compile(r"/maps/place/"), timeout=ACTION_TIMEOUT_MS)
        return True

    async def _place_title(self, page: Page) -> str:
        try:
            heading = page.locator("h1").first
            await heading.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
            return (await heading.inner_text()).strip()
        except PlaywrightError:
            return ""

    async def _open_menu_tab(self, page: Page) -> bool:
        locators = (
            page.get_by_role("tab", name=re.compile(r"^menu$", re.I)),
            page.locator('button[role="tab"][aria-label*="Menu" i]'),
            page.get_by_role("button", name=re.compile(r"^menu$", re.I)),
        )
        for locator in locators:
            if await locator.count():
                await locator.first.click()
                await page.wait_for_timeout(1500)
                return True
        return False

    async def _scroll_panel(self, page: Page) -> None:
        previous = -1
        for _ in range(MAX_SCROLLS):
            height = await page.evaluate(_SCROLL_JS)
            if height == previous:
                break
            previous = height
            await page.wait_for_timeout(700)

    async def _panel_text(self, page: Page) -> str:
        main = page.locator('div[role="main"]')
        if await main.count():
            return await main.last.inner_text()
        return await page.locator("body").inner_text()

    async def _dump(self, page: Page, r: Restaurant, tag: str) -> None:
        if not self._debug:
            return
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{r.name}-{tag}")[:80]
        try:
            await Actor.set_value(f"debug-{slug}.png", await page.screenshot(), content_type="image/png")
            await Actor.set_value(
                f"debug-{slug}.txt",
                await page.locator("body").inner_text(),
                content_type="text/plain; charset=utf-8",
            )
        except PlaywrightError:
            pass
