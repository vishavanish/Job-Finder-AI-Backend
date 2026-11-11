"""
app/live_browser/session.py
-----------------------------
One live Chromium for one apply run (QA11 A).

- Streams the page with CDP `Page.startScreencast` (JPEG, ~LIVE_FPS) — only
  the newest frame is kept, so a slow phone never builds a backlog.
- Takes the user's input as Playwright mouse / keyboard calls in page (CSS)
  pixels. Nothing typed is logged: input is counted, not recorded.
- Guards where the browser goes: top-level pages only on the job's own site(s)
  or a known sign-in provider; no request at all to localhost / private
  addresses (the server is not an open proxy). File pages only in tests.
- Watches the page: `ready` turns true once a password / code / captcha box
  has been seen and then is gone — the user looks signed in.
- `finish()` returns the context's storage state (cookies + local storage);
  the caller seals it for the apply engine and closes the session.

We don't disguise the browser: same Chromium, user agent and locale as the
apply engine.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import socket
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

logger = logging.getLogger("job_finder_api.live_browser")

WIDTH, HEIGHT = 1280, 860

# Top-level pages may also go to these sign-in providers (SSO buttons on career sites).
IDENTITY_SITES = {
    "google.com", "accounts.google.com", "microsoftonline.com", "live.com", "microsoft.com", "apple.com",
    "linkedin.com", "okta.com", "auth0.com", "onelogin.com", "pingidentity.com", "myworkdayjobs.com",
    "myworkdaysite.com", "workday.com", "oraclecloud.com", "successfactors.com", "successfactors.eu",
    "icims.com", "taleo.net",
}

STATE_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const any = sel => [...document.querySelectorAll(sel)].some(vis);
  return {
    password: any('input[type=password]'),
    code: any('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], ' +
              'input[name*="verificationcode" i], input[id*="verificationcode" i], input[name*="verify_code" i]'),
    captcha: any('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], ' +
                 'iframe[src*="turnstile"], iframe[title*="captcha" i], .g-recaptcha, .h-captcha'),
  };
}"""


def site_of(host: str) -> str:
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or \
        ip.is_unspecified


class Guard:
    """Which URLs the live browser may load."""

    def __init__(self, allowed_sites: set[str], *, allow_private: bool = False):
        self.allowed = {site_of(s) for s in allowed_sites if s} | IDENTITY_SITES
        self.allow_private = allow_private
        self._dns: dict[str, bool] = {}

    def site_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in ("about", "data", "blob"):
            return True
        if parsed.scheme == "file":
            return self.allow_private
        host = (parsed.hostname or "").lower()
        if not host or parsed.scheme not in ("http", "https"):
            return False
        if self.allow_private and (host in ("localhost",) or _private_ip(host)):
            return True
        site = site_of(host)
        return site in self.allowed or any(host == s or host.endswith("." + s) for s in self.allowed)

    async def private_host(self, url: str) -> bool:
        """True when the URL points at this server or a private network."""
        parsed = urlparse(url)
        if parsed.scheme in ("about", "data", "blob"):
            return False
        if parsed.scheme == "file":
            return not self.allow_private
        host = (parsed.hostname or "").lower()
        if self.allow_private or not host:
            return False
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
            return True
        if _private_ip(host):
            return True
        if host not in self._dns:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
                self._dns[host] = any(_private_ip(i[4][0]) for i in infos)
            except OSError:
                self._dns[host] = False  # unresolvable: the request fails by itself
        return self._dns[host]


Sender = Callable[[dict | bytes], Awaitable[None]]


class LiveSession:
    def __init__(self, session_id: str, url: str, allowed_sites: set[str], *, storage_state: dict | None = None,
                 allow_private: bool = False, fps: int = 8):
        self.id = session_id
        self.start_url = url
        self.guard = Guard(allowed_sites | {urlparse(url).hostname or ""}, allow_private=allow_private)
        self.storage_state = storage_state
        self.fps = max(1, min(fps, 15))
        self.started = time.monotonic()
        self.last_input = time.monotonic()
        self.inputs = 0
        self.seen_block = False
        self.ready = False
        self.blocked_hosts: list[str] = []
        self._pw = self._browser = self._context = self.page = self._cdp = None
        self._frame: bytes | None = None
        self._frame_event = asyncio.Event()
        self._sender: Sender | None = None
        self._closed = False
        self._last_sent_state: dict | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        kwargs = {"viewport": {"width": WIDTH, "height": HEIGHT}}
        if self.storage_state:
            kwargs["storage_state"] = self.storage_state
        self._context = await self._browser.new_context(**kwargs)
        await self._context.route("**/*", self._route)
        self._context.on("page", lambda p: asyncio.ensure_future(self._adopt(p)))
        page = await self._context.new_page()
        await self._adopt(page)
        try:
            await page.goto(self.start_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:  # noqa: BLE001 — the user still sees whatever loaded
            logger.info("live session %s: first page didn't finish loading (%s)", self.id, type(e).__name__)
        await self.check_state()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._frame_event.set()
        for closer in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            with contextlib.suppress(Exception):
                result = closer()
                if result is not None:
                    await result

    @property
    def closed(self) -> bool:
        return self._closed

    async def finish(self) -> dict:
        state = await self._context.storage_state()
        await self.close()
        return state

    # ------------------------------------------------------------ guard

    async def _route(self, route) -> None:
        request = route.request
        url = request.url
        if await self.guard.private_host(url):
            await route.abort("blockedbyclient")
            return
        if request.is_navigation_request() and request.frame == request.frame.page.main_frame \
                and not self.guard.site_allowed(url):
            host = urlparse(url).hostname or url[:60]
            self.blocked_hosts.append(host)
            await self._emit({"type": "blocked", "host": host,
                              "message": f"{host} isn't part of this application, so it was not opened."})
            # 204 on a navigation keeps the current page (an abort would show Chrome's error page).
            await route.fulfill(status=204, body="")
            return
        await route.continue_()

    # ------------------------------------------------------------ pages and frames

    async def _adopt(self, page) -> None:
        """Show this page (the first one, or a sign-in pop-up)."""
        if self._closed:
            return
        old = self._cdp
        self.page = page
        page.on("framenavigated", lambda f: asyncio.ensure_future(self._navigated(f)))
        page.on("load", lambda _p: asyncio.ensure_future(self.check_state()))
        page.on("close", lambda _p: asyncio.ensure_future(self._page_closed(page)))
        if old is not None:
            with contextlib.suppress(Exception):
                await old.send("Page.stopScreencast")
        self._cdp = await self._context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", lambda e: asyncio.ensure_future(self._on_frame(e)))
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": WIDTH,
                                                      "maxHeight": HEIGHT, "everyNthFrame": 1})
        await self._emit({"type": "page", "url": page.url})

    async def _page_closed(self, page) -> None:
        if self._closed or page is not self.page:
            return
        pages = [p for p in self._context.pages if not p.is_closed()]
        if pages:
            await self._adopt(pages[-1])

    async def _navigated(self, frame) -> None:
        if self.page is not None and frame == self.page.main_frame:
            await self._emit({"type": "page", "url": frame.url})

    async def _on_frame(self, event: dict) -> None:
        with contextlib.suppress(Exception):
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        self._frame = base64.b64decode(event["data"])
        self._frame_event.set()

    async def check_state(self) -> dict:
        if self._closed or self.page is None:
            return {}
        try:
            s = await self.page.evaluate(STATE_JS)
        except Exception:  # noqa: BLE001 — mid-navigation
            return {}
        blocking = bool(s.get("password") or s.get("code") or s.get("captcha"))
        self.seen_block = self.seen_block or blocking
        self.ready = self.seen_block and not blocking
        state = {"type": "state", "ready": self.ready, "asks": [k for k, v in s.items() if v],
                 "url": self.page.url}
        if state != self._last_sent_state:
            self._last_sent_state = state
            await self._emit(state)
        return state

    # ------------------------------------------------------------ streaming

    async def _emit(self, msg: dict | bytes) -> None:
        if self._sender is not None:
            with contextlib.suppress(Exception):
                await self._sender(msg)

    async def stream(self, send: Sender, receive: Callable[[], Awaitable[dict | None]]) -> None:
        """Pump frames out and input in until the client goes away or the session closes."""
        self._sender = send
        self._last_sent_state = None
        await self._emit({"type": "hello", "width": WIDTH, "height": HEIGHT, "url": self.page.url if self.page else ""})
        await self.check_state()
        if self._frame:
            self._frame_event.set()

        async def frames() -> None:
            gap = 1 / self.fps
            while not self._closed:
                await self._frame_event.wait()
                self._frame_event.clear()
                if self._closed:
                    break
                if self._frame:
                    await send(self._frame)
                await asyncio.sleep(gap)

        async def watch() -> None:
            while not self._closed:
                await asyncio.sleep(2)
                await self.check_state()

        async def inputs() -> None:
            while not self._closed:
                msg = await receive()
                if msg is None:
                    return
                await self.handle(msg)

        tasks = [asyncio.ensure_future(t()) for t in (frames, watch, inputs)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._sender is send:
                self._sender = None

    # ------------------------------------------------------------ input

    async def handle(self, msg: dict) -> None:
        """Mouse / keys in page pixels. Unknown messages are ignored."""
        page = self.page
        if page is None or self._closed:
            return
        kind = msg.get("type")
        self.last_input = time.monotonic()
        self.inputs += 1

        def xy() -> tuple[float, float]:
            return (max(0.0, min(float(msg.get("x", 0)), WIDTH)), max(0.0, min(float(msg.get("y", 0)), HEIGHT)))

        try:
            if kind == "click":
                x, y = xy()
                await page.mouse.click(x, y, button="right" if msg.get("button") == "right" else "left",
                                       click_count=2 if msg.get("double") else 1)
            elif kind == "move":
                await page.mouse.move(*xy())
            elif kind == "wheel":
                x, y = xy()
                await page.mouse.move(x, y)
                await page.mouse.wheel(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
            elif kind == "key":
                key = str(msg.get("key", ""))[:40]
                if key:
                    await page.keyboard.press(key)
            elif kind == "text":
                text = str(msg.get("text", ""))[:500]
                if text:
                    await page.keyboard.insert_text(text)
            elif kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=15_000)
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
            else:
                return
        except Exception as e:  # noqa: BLE001 — a bad key name or a closed page must not end the session
            logger.info("live session %s: input %s failed (%s)", self.id, kind, type(e).__name__)
        await asyncio.sleep(0.15)
        await self.check_state()
