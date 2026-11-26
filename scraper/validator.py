"""URL format validation, normalization, and live reachability verification."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from urllib.parse import urlparse, urlunparse

import httpx

from scraper.config import USER_AGENTS
from scraper.logging_utils import get_logger
from scraper.models import ValidationResult

logger = get_logger("validator")

_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
)


def normalize_and_validate_url(raw_url: str) -> ValidationResult:
    """Validate URL syntax and return a normalized canonical URL."""
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error="URL cannot be empty.",
        )

    if any(ch.isspace() for ch in cleaned):
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error="URL contains whitespace.",
        )

    # Prepend https:// if user entered a bare domain such as "cloudflare.com"
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error=f"Malformed URL: {exc}",
        )

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error=f"Unsupported URL scheme '{scheme}'. Only http and https are allowed.",
        )

    host = (parsed.hostname or "").lower()
    if not host:
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error="URL is missing a hostname.",
        )

    # Allow localhost / explicit IPs for local testing servers, or standard FQDN domains
    is_valid_host = False
    if host == "localhost":
        is_valid_host = True
    else:
        try:
            ipaddress.ip_address(host)
            is_valid_host = True
        except ValueError:
            is_valid_host = bool(_DOMAIN_RE.match(host))

    if not is_valid_host:
        return ValidationResult(
            raw_input=raw_url,
            is_valid_format=False,
            error=f"Invalid hostname '{host}'.",
        )

    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    normalized = urlunparse((scheme, netloc, path, "", parsed.query, ""))

    return ValidationResult(
        raw_input=raw_url,
        normalized_url=normalized,
        is_valid_format=True,
    )


async def check_url_reachability(
    url: str,
    timeout: float = 10.0,
    proxy: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ValidationResult:
    """Validate format and verify live HTTP reachability (HEAD with GET fallback)."""
    result = normalize_and_validate_url(url)
    if not result.is_valid_format or not result.normalized_url:
        return result

    target = result.normalized_url
    headers = {"User-Agent": USER_AGENTS[0], "Accept": "text/html,application/xhtml+xml"}
    client_kwargs: dict = {
        "timeout": timeout,
        "follow_redirects": True,
        "headers": headers,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.head(target)
            # Many sites reject HEAD (405/403/501) while allowing GET
            if response.status_code in (403, 405, 501):
                response = await client.get(target)

            result.status_code = response.status_code
            result.final_url = str(response.url)
            # 2xx-3xx and 403/429 mean host exists and is reachable (even if anti-bot challenges GET)
            if response.status_code < 400 or response.status_code in (401, 403, 429):
                result.is_reachable = True
            else:
                result.is_reachable = False
                result.error = f"HTTP {response.status_code} returned by server."
    except httpx.TimeoutException:
        result.is_reachable = False
        result.error = f"Connection timed out after {timeout:.1f}s."
    except httpx.ConnectError as exc:
        result.is_reachable = False
        result.error = f"Could not connect to host: {exc}"
    except httpx.HTTPError as exc:
        result.is_reachable = False
        result.error = f"HTTP error: {exc}"

    return result


async def validate_seed_urls(
    urls: list[str],
    verify_reachability: bool = True,
    timeout: float = 10.0,
    proxy: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ValidationResult]:
    """Validate a batch of seed URLs concurrently."""
    if not verify_reachability:
        return [normalize_and_validate_url(url) for url in urls]

    tasks = [
        check_url_reachability(url, timeout=timeout, proxy=proxy, transport=transport)
        for url in urls
    ]
    return await asyncio.gather(*tasks)


def extract_root_domain(url: str) -> str:
    """Return lowercase hostname without leading `www.`."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host
