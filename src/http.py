from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from .errors import PartnerError

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Retried with a fresh proxy IP: blocks and throttling usually clear on a new exit.
RETRYABLE = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class HttpFactory:
    """Creates httpx clients, each routed through a fresh Apify proxy URL."""

    def __init__(self, proxy_cfg: Any | None, *, headers: dict[str, str] | None = None,
                 timeout: float = 30.0) -> None:
        self._proxy_cfg = proxy_cfg
        self._headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._timeout = timeout

    async def client(self, **kwargs: Any) -> httpx.AsyncClient:
        proxy_url = await self._proxy_cfg.new_url() if self._proxy_cfg else None
        return httpx.AsyncClient(
            headers=self._headers,
            proxy=proxy_url,
            timeout=self._timeout,
            follow_redirects=True,
            **kwargs,
        )


async def request_json(
    factory: HttpFactory,
    method: str,
    url: str,
    *,
    client_kwargs: dict[str, Any] | None = None,
    retries: int = 3,
    **kwargs: Any,
) -> Any | None:
    """Return parsed JSON, None on 404, or raise PartnerError after retries."""
    last = "no attempts made"
    for attempt in range(1, retries + 1):
        try:
            async with await factory.client(**(client_kwargs or {})) as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            code = resp.status_code
            if code == 404:
                return None
            if code in RETRYABLE:
                last = f"HTTP {code}"
            elif code >= 400:
                raise PartnerError(f"HTTP {code} from {url}")
            else:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise PartnerError(f"non-JSON response from {url}") from exc
        if attempt < retries:
            await asyncio.sleep(min(2**attempt, 10) + random.random())
    raise PartnerError(f"{method} {url} failed after {retries} attempts ({last})")
