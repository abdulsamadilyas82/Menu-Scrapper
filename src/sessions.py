"""Google session pool backed by a named key-value store.

Cookies you supply in the Actor input are persisted in the private named store
`menu-scrapper-sessions`. After each run the refreshed cookies from the browser are saved
back, so a session keeps itself alive between runs. Supplying *different* cookies for the
same session id in the input replaces the stored ones. Cookie values are never logged.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from apify import Actor

STORE_NAME = "menu-scrapper-sessions"
STORE_KEY = "SESSIONS"
BAD_COOLDOWN_S = 6 * 3600

_SAMESITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


def normalize_cookie(c: dict[str, Any]) -> dict[str, Any] | None:
    """Accept Playwright-style and browser-extension-style cookie JSON; emit Playwright format."""
    name, value = c.get("name"), c.get("value")
    if not name or value is None:
        return None
    out: dict[str, Any] = {"name": str(name), "value": str(value)}
    if c.get("url"):
        out["url"] = c["url"]
    elif c.get("domain"):
        out["domain"] = c["domain"]
        out["path"] = c.get("path") or "/"
    else:
        return None
    expires = c.get("expires", c.get("expirationDate"))
    if isinstance(expires, (int, float)) and expires > 0:
        out["expires"] = float(expires)
    same_site = _SAMESITE.get(str(c.get("sameSite", "")).lower(), "Lax")
    out["sameSite"] = same_site
    out["httpOnly"] = bool(c.get("httpOnly", False))
    out["secure"] = bool(c.get("secure", False)) or same_site == "None"
    return out


def only_google_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cookies if "google" in str(c.get("domain", ""))]


@dataclass(slots=True)
class GoogleSession:
    id: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    input_hash: str = ""
    bad_until: float = 0.0
    bad_reason: str = ""
    uses: int = 0

    @property
    def healthy(self) -> bool:
        return time.time() >= self.bad_until

    @property
    def proxy_session_id(self) -> str:
        # Apify proxy session ids: [A-Za-z0-9_.~-], max 50 chars.
        return re.sub(r"[^A-Za-z0-9_.~-]", "_", self.id)[:50]


class SessionStore:
    def __init__(self, kvs: Any, sessions: list[GoogleSession]) -> None:
        self._kvs = kvs
        self._sessions = sessions
        self._cursor = 0

    @classmethod
    async def load(cls, input_sessions: list[dict[str, Any]] | None) -> "SessionStore":
        kvs = await Actor.open_key_value_store(name=STORE_NAME)
        persisted = await kvs.get_value(STORE_KEY) or {}
        known = {f.name for f in fields(GoogleSession)}
        sessions: list[GoogleSession] = []
        for idx, raw in enumerate(input_sessions or []):
            sid = str(raw.get("id") or f"session-{idx + 1}")
            raw_cookies = raw.get("cookies") or []
            digest = hashlib.sha256(
                json.dumps(raw_cookies, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            saved = persisted.get(sid)
            if saved and saved.get("input_hash") == digest:
                # Same cookies as last run: keep the refreshed copy and the health state.
                sessions.append(GoogleSession(**{k: v for k, v in saved.items() if k in known}))
                continue
            cookies = [n for n in (normalize_cookie(c) for c in raw_cookies) if n]
            if not cookies:
                Actor.log.warning(f"Google session '{sid}' has no usable cookies; ignoring it")
                continue
            sessions.append(GoogleSession(id=sid, cookies=cookies, input_hash=digest))
        if not sessions:
            Actor.log.warning(
                "No Google sessions supplied: the Google Maps fallback will run anonymously "
                "and will most likely hit a login wall on the Menu tab."
            )
            sessions.append(GoogleSession(id="anonymous"))
        return cls(kvs, sessions)

    def acquire(self) -> GoogleSession | None:
        n = len(self._sessions)
        for _ in range(n):
            session = self._sessions[self._cursor % n]
            self._cursor += 1
            if session.healthy:
                session.uses += 1
                return session
        return None

    def mark_bad(self, session: GoogleSession, reason: str) -> None:
        session.bad_until = time.time() + BAD_COOLDOWN_S
        session.bad_reason = reason
        Actor.log.warning(f"Google session '{session.id}' resting for 6h: {reason}")

    def update_cookies(self, session: GoogleSession, cookies: list[dict[str, Any]]) -> None:
        fresh = only_google_cookies(cookies)
        if fresh:
            session.cookies = fresh

    async def save(self) -> None:
        await self._kvs.set_value(STORE_KEY, {s.id: asdict(s) for s in self._sessions})
