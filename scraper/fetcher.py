"""Polite async HTTP & headless Playwright fetcher with rate limiting, retries, proxy rotation, and robots.txt awareness."""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from itertools import cycle
from typing import Iterator
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from scraper.config import USER_AGENTS
from scraper.logging_utils import get_logger
from scraper.models import RenderMode, ScraperConfig

logger = get_logger("fetcher")

_SPA_MARKERS = (
    'id="root"',
    "id='root'",
    'id="app"',
    "id='app'",
    'id="__next"',
    "enable javascript",
    "you need to enable javascript",
    "please enable javascript",
    "just a moment...",
    "checking your browser",
)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchedPage:
    """Result of fetching a single URL."""

    url: str
    final_url: str
    status_code: int | None
    html: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    render_method: str = "httpx"
    error: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.html) and (
            self.status_code is None or self.status_code < 400
        )


def needs_dynamic_render(html: str, status_code: int | None) -> bool:
    """Heuristic check for SPA shells or JS-gated responses that require a real browser."""
    if status_code in (403, 503):
        return True
    if not html:
        return True

    stripped = html.lstrip()
    if stripped.startswith("<?xml") or "<urlset" in stripped[:300].lower():
        return False

    lower_html = html.lower()
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    if len(visible_text) < 220 and any(marker in lower_html for marker in _SPA_MARKERS):
        return True
    if len(visible_text) < 80 and "<script" in lower_html:
        return True
    return False


class DomainRateLimiter:
    """Thread/async-safe per-domain rate limiter with human-like random jitter."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str) -> None:
        domain = (urlparse(url).hostname or "").lower()
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last_hit.get(domain)
            if last is not None and self.max_delay > 0:
                delay = random.uniform(self.min_delay, self.max_delay)
                elapsed = now - last
                if elapsed < delay:
                    await asyncio.sleep(delay - elapsed)
            self._last_hit[domain] = time.monotonic()


class PageFetcher:
    """Async context-managed fetcher combining httpx and lazy Playwright rendering."""

    def __init__(
        self,
        config: ScraperConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._rate_limiter = DomainRateLimiter(
            config.min_delay_seconds, config.max_delay_seconds
        )
        self._ua_cycle: Iterator[str] = cycle(USER_AGENTS)
        self._proxy_cycle: Iterator[str] | None = (
            cycle(config.proxies) if config.proxies else None
        )
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None
        self._playwright = None
        self._browser = None
        self._playwright_unavailable = False
        self._pw_lock = asyncio.Lock()

    async def __aenter__(self) -> PageFetcher:
        client_kwargs: dict = {
            "timeout": httpx.Timeout(self.config.timeout_seconds),
            "follow_redirects": True,
            "limits": httpx.Limits(max_connections=30, max_keepalive_connections=15),
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        elif self.config.proxies and len(self.config.proxies) == 1:
            client_kwargs["proxy"] = self.config.proxies[0]
        self._client = httpx.AsyncClient(**client_kwargs)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _next_headers(self) -> dict[str, str]:
        return {
            "User-Agent": next(self._ua_cycle),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
        }

    async def _is_allowed_by_robots(self, url: str) -> bool:
        if not self.config.respect_robots or self._client is None:
            return True

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots_cache:
                robots_url = f"{origin}/robots.txt"
                parser = RobotFileParser()
                parser.set_url(robots_url)
                try:
                    resp = await self._client.get(
                        robots_url,
                        headers={"User-Agent": USER_AGENTS[0]},
                        timeout=min(5.0, self.config.timeout_seconds),
                    )
                    if resp.status_code == 200 and resp.text:
                        parser.parse(resp.text.splitlines())
                        self._robots_cache[origin] = parser
                    else:
                        self._robots_cache[origin] = None
                except Exception:
                    self._robots_cache[origin] = None

            cached = self._robots_cache[origin]
            if cached is None:
                return True
            return cached.can_fetch("*", url)

    async def _ensure_browser(self):
        if self._playwright_unavailable:
            return None
        async with self._pw_lock:
            if self._browser is not None:
                return self._browser
            if self._playwright_unavailable:
                return None
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                launch_attempts = [
                    {"headless": True},
                    {"headless": True, "channel": "chrome"},
                    {"headless": True, "channel": "msedge"},
                ]
                for kwargs in launch_attempts:
                    try:
                        self._browser = await self._playwright.chromium.launch(**kwargs)
                        logger.info("Initialized headless Playwright browser (%s)", kwargs)
                        return self._browser
                    except Exception:
                        continue
                self._playwright_unavailable = True
                logger.warning(
                    "Playwright browser binary unavailable; falling back to static HTTP."
                )
            except Exception as exc:
                self._playwright_unavailable = True
                logger.warning("Playwright initialization skipped: %s", exc)
            return None

    async def _fetch_static(self, url: str) -> FetchedPage:
        assert self._client is not None, "PageFetcher must be used inside `async with`"
        last_error: str | None = None
        last_error_type: str | None = None

        for attempt in range(self.config.max_retries + 1):
            await self._rate_limiter.wait(url)
            try:
                if (
                    self._transport is None
                    and self._proxy_cycle is not None
                    and len(self.config.proxies) > 1
                ):
                    proxy_url = next(self._proxy_cycle)
                    async with httpx.AsyncClient(
                        proxy=proxy_url,
                        timeout=httpx.Timeout(self.config.timeout_seconds),
                        follow_redirects=True,
                    ) as proxied_client:
                        response = await proxied_client.get(url, headers=self._next_headers())
                else:
                    response = await self._client.get(url, headers=self._next_headers())

                headers = {k.lower(): v for k, v in response.headers.items()}
                content_type = headers.get("content-type", "").lower()

                if response.status_code in _RETRYABLE_STATUSES and attempt < self.config.max_retries:
                    backoff = (2**attempt) * 0.75 + random.uniform(0.1, 0.4)
                    logger.debug(
                        "Retryable HTTP %s for %s; sleeping %.2fs",
                        response.status_code,
                        url,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                if response.status_code >= 400:
                    return FetchedPage(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        html=response.text if "html" in content_type else "",
                        headers=headers,
                        render_method="httpx",
                        error=f"HTTP {response.status_code}",
                        error_type="HTTPError",
                    )

                if content_type and not any(
                    allowed in content_type
                    for allowed in ("text/html", "application/xhtml", "text/plain", "xml")
                ):
                    return FetchedPage(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        headers=headers,
                        render_method="httpx",
                        error=f"Skipped non-HTML content-type: {content_type}",
                        error_type="UnsupportedContentType",
                    )

                return FetchedPage(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    html=response.text,
                    headers=headers,
                    render_method="httpx",
                )

            except httpx.TimeoutException:
                last_error = f"Request timed out after {self.config.timeout_seconds:.1f}s"
                last_error_type = "TimeoutError"
            except httpx.ConnectError as exc:
                last_error = f"Connection failed: {exc}"
                last_error_type = "ConnectionError"
            except httpx.HTTPError as exc:
                last_error = f"HTTP client error: {exc}"
                last_error_type = "HTTPError"

            if attempt < self.config.max_retries:
                await asyncio.sleep((2**attempt) * 0.6)

        return FetchedPage(
            url=url,
            final_url=url,
            status_code=None,
            render_method="httpx",
            error=last_error or "Unknown fetch error",
            error_type=last_error_type or "FetchError",
        )

    async def _fetch_dynamic(self, url: str) -> FetchedPage:
        browser = await self._ensure_browser()
        if browser is None:
            return FetchedPage(
                url=url,
                final_url=url,
                status_code=None,
                render_method="playwright",
                error="Playwright browser unavailable",
                error_type="BrowserUnavailable",
            )

        await self._rate_limiter.wait(url)
        context_kwargs: dict = {"user_agent": next(self._ua_cycle)}
        if self._proxy_cycle is not None:
            context_kwargs["proxy"] = {"server": next(self._proxy_cycle)}

        context = None
        try:
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            timeout_ms = int(self.config.timeout_seconds * 1000)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms))
            except Exception:
                # Some analytics-heavy sites keep open long-polling sockets
                pass
            await page.wait_for_timeout(450)
            html = await page.content()
            status = response.status if response else None
            headers = {k.lower(): v for k, v in (response.headers if response else {}).items()}
            if status is not None and status >= 400 and len(html) < 400:
                return FetchedPage(
                    url=url,
                    final_url=page.url,
                    status_code=status,
                    html=html,
                    headers=headers,
                    render_method="playwright",
                    error=f"HTTP {status}",
                    error_type="HTTPError",
                )
            return FetchedPage(
                url=url,
                final_url=page.url,
                status_code=status,
                html=html,
                headers=headers,
                render_method="playwright",
            )
        except Exception as exc:
            return FetchedPage(
                url=url,
                final_url=url,
                status_code=None,
                render_method="playwright",
                error=f"Playwright error: {exc}",
                error_type="PlaywrightError",
            )
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    async def fetch(self, url: str) -> FetchedPage:
        """Fetch a URL using configured RenderMode (static, auto, or dynamic)."""
        if not await self._is_allowed_by_robots(url):
            return FetchedPage(
                url=url,
                final_url=url,
                status_code=403,
                render_method="robots",
                error="Disallowed by site robots.txt policy",
                error_type="RobotsDisallowed",
            )

        if self.config.render_mode == RenderMode.DYNAMIC:
            dyn_page = await self._fetch_dynamic(url)
            if dyn_page.ok:
                return dyn_page
            # Fall back to static if browser unavailable
            if dyn_page.error_type == "BrowserUnavailable":
                return await self._fetch_static(url)
            return dyn_page

        static_page = await self._fetch_static(url)
        if self.config.render_mode == RenderMode.STATIC:
            return static_page

        # RenderMode.AUTO: escalate to Playwright when SPA shell or soft block detected
        if needs_dynamic_render(static_page.html, static_page.status_code):
            logger.debug("Escalating %s to Playwright dynamic render", url)
            dyn_page = await self._fetch_dynamic(url)
            if dyn_page.ok:
                return dyn_page
        return static_page
