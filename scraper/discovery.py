"""Search query execution, paginated listing traversal, and internal subpage crawling."""

from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from scraper.fetcher import PageFetcher
from scraper.logging_utils import get_logger
from scraper.validator import extract_root_domain, normalize_and_validate_url

logger = get_logger("discovery")

# Aggregator, social, and generic news domains skipped when selecting direct company sites
_NOISE_DOMAINS = {
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "wikipedia.org", "en.wikipedia.org", "wikidata.org",
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "quora.com",
    "github.com", "medium.com", "substack.com",
    "crunchbase.com", "pitchbook.com", "tracxn.com", "cbinsights.com",
    "g2.com", "capterra.com", "trustpilot.com", "clutch.co", "goodfirms.co",
    "glassdoor.com", "indeed.com", "wellfound.com", "angel.co",
    "ycombinator.com", "news.ycombinator.com", "techcrunch.com", "forbes.com",
    "bloomberg.com", "reuters.com", "businessinsider.com", "wsj.com",
    "eu-startups.com", "sifted.eu", "seedtable.com", "builtin.com",
    "dealroom.co", "failory.com", "explodingtopics.com", "f6s.com",
    "amazon.com", "apple.com", "microsoft.com", "alibaba.com", "aliexpress.com",
    "ebay.com", "walmart.com", "etsy.com", "target.com", "baidu.com", "zhihu.com",
    "doubleclick.net", "googleadservices.com", "cookiebot.com", "onetrust.com",
    "typeform.com", "calendly.com", "apps.apple.com", "play.google.com",
    "w3.org", "schema.org", "gravatar.com", "wordpress.org", "cloudflare.com",
    "archive.org", "geeksforgeeks.org", "icloud.com",
}

_AD_PATH_MARKERS = (
    "/aclick", "/ck/a", "/pagead/", "googleadservices",
    "utm_source=bing", "utm_medium=banner",
)
_BLOCKED_LISTICLE_DOMAINS = (
    "crunchbase.com", "dealroom.co", "seedtable.com", "pitchbook.com",
    "tracxn.com", "cbinsights.com", "gartner.com", "linkedin.com",
    "wellfound.com", "glassdoor.com",
)

# Subpage priority weights for company intelligence extraction
_SUBPAGE_PRIORITIES: tuple[tuple[str, int], ...] = (
    ("/about", 100),
    ("/company", 95),
    ("/contact", 95),
    ("/products", 90),
    ("/platform", 90),
    ("/solutions", 88),
    ("/services", 88),
    ("/features", 82),
    ("/technology", 80),
    ("/engineering", 80),
    ("/careers", 78),
    ("/jobs", 75),
    ("/customers", 74),
    ("/case-studies", 72),
    ("/pricing", 70),
    ("/vs", 70),
    ("/compare", 68),
    ("/alternatives", 68),
    ("/blog", 65),
    ("/news", 62),
    ("/projects", 65),
    ("/research", 65),
)

_SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".zip", ".tar", ".gz", ".mp4", ".mp3", ".woff", ".woff2",
)


def _is_company_homepage_candidate(url: str) -> bool:
    lower = url.lower()
    if any(marker in lower for marker in _AD_PATH_MARKERS):
        return False
    domain = extract_root_domain(url)
    if not domain:
        return False
    if any(domain == noise or domain.endswith(f".{noise}") for noise in _NOISE_DOMAINS):
        return False
    return True


def _root_homepage(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _bing_http_search(query: str, max_results: int) -> list[str]:
    """Direct HTTP search fallback using Bing HTML when ddgs encounters TLS/H2 limits."""
    import httpx

    urls: list[str] = []
    seen: set[str] = set()
    try:
        resp = httpx.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=10.0,
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a")
            if not a or not a.get("href"):
                continue
            href = a["href"].strip()
            if "bing.com/ck/a" in href:
                u = parse_qs(urlparse(href).query).get("u", [""])[0]
                if u.startswith("a1"):
                    b64 = u[2:] + "=" * (-len(u[2:]) % 4)
                    try:
                        href = base64.urlsafe_b64decode(b64).decode("utf-8")
                    except Exception:
                        continue
            if not href.startswith(("http://", "https://")):
                continue
            if any(marker in href.lower() for marker in _AD_PATH_MARKERS):
                continue
            if href not in seen:
                seen.add(href)
                urls.append(href)
            if len(urls) >= max_results:
                break
    except Exception:
        return []
    return urls


def _run_ddgs_search(query: str, max_results: int) -> list[str]:
    """Resilient multi-backend wrapper around ddgs with direct HTTP fallback."""
    from ddgs import DDGS

    urls: list[str] = []
    seen: set[str] = set()
    backends_to_try = ("yahoo", "bing", "google", "duckduckgo", "brave")

    try:
        with DDGS() as ddgs:
            for backend in backends_to_try:
                if len(urls) >= max_results:
                    break
                try:
                    results = ddgs.text(
                        query,
                        max_results=max_results,
                        backend=backend,
                    )
                    for item in results or []:
                        href = item.get("href") or item.get("link") or item.get("url")
                        if not href:
                            continue
                        href_str = str(href)
                        if any(marker in href_str.lower() for marker in _AD_PATH_MARKERS):
                            continue
                        if href_str not in seen:
                            seen.add(href_str)
                            urls.append(href_str)
                except Exception:
                    continue
    except Exception:
        pass

    if len(urls) < max_results:
        for href_str in _bing_http_search(query, max_results - len(urls)):
            if href_str not in seen:
                seen.add(href_str)
                urls.append(href_str)

    return urls[:max_results]


async def _extract_companies_from_listicle(
    fetcher: PageFetcher,
    listicle_url: str,
    limit: int = 10,
) -> list[str]:
    """Extract outbound company homepage URLs (or resolve listed startup entities) from a directory/listicle page."""
    page = await fetcher.fetch(listicle_url)
    if not page.ok:
        return []

    soup = BeautifulSoup(page.html, "lxml")
    source_domain = extract_root_domain(listicle_url)
    found: list[str] = []
    seen_domains: set[str] = {source_domain}

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        anchor_text = anchor.get_text(" ", strip=True).lower()
        if any(
            skip in anchor_text
            for skip in ("privacy", "cookie", "terms", "gdpr", "advertise", "subscribe", "sign in", "log in")
        ):
            continue
        domain = extract_root_domain(href)
        if not domain or domain in seen_domains:
            continue
        if domain.startswith(("cdn.", "static.", "assets.", "fonts.", "docs.", "support.")):
            continue
        if not _is_company_homepage_candidate(href):
            continue
        path = urlparse(href).path.strip("/")
        if len(path.split("/")) > 2:
            continue
        seen_domains.add(domain)
        found.append(_root_homepage(href))
        if len(found) >= limit:
            return found

    # If the directory links internally (/startup/<name>, /company/<name>), resolve entity names via search
    entity_names: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip().lower()
        if any(seg in href for seg in ("/startup/", "/company/", "/companies/")):
            raw_name = re.sub(r"[↗→»]+", "", anchor.get_text(" ", strip=True)).strip()
            if 2 <= len(raw_name) <= 40 and raw_name.lower() not in ("startups", "companies"):
                if raw_name not in entity_names:
                    entity_names.append(raw_name)
        if len(entity_names) >= limit * 2:
            break

    # Also ensure entity resolution checks domain/title relevance for single-entity queries
    for entity in entity_names:
        if len(found) >= limit:
            break
        try:
            hits = await asyncio.to_thread(_run_ddgs_search, f"{entity} cloud startup official website", 4)
        except Exception:
            hits = []
        entity_token = re.sub(r"[^a-z0-9]", "", entity.lower())
        for hit in hits:
            val = normalize_and_validate_url(hit)
            if not val.is_valid_format or not val.normalized_url:
                continue
            u = val.normalized_url
            dom = extract_root_domain(u)
            if not dom or dom in seen_domains:
                continue
            dom_clean = re.sub(r"[^a-z0-9]", "", dom.split(".")[0].lower())
            if _is_company_homepage_candidate(u) and not _is_listicle_path(u):
                if entity_token and (entity_token[:4] in dom_clean or dom_clean[:4] in entity_token):
                    seen_domains.add(dom)
                    found.append(_root_homepage(u))
                    break

    return found


_LISTICLE_SLUG_HINTS = (
    "top-", "best-", "startup", "startups", "companies", "list",
    "alternative", "alternatives", "blog", "article", "news", "guide", "directory",
)


def _is_listicle_path(url: str) -> bool:
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return False
    if "/" in path:
        return True
    return any(hint in path for hint in _LISTICLE_SLUG_HINTS)


async def discover_companies_from_query(
    query: str,
    max_companies: int,
    fetcher: PageFetcher | None = None,
) -> list[str]:
    """Discover unique company website URLs from a natural-language search query."""
    logger.info("Executing company discovery query: %r (target=%d)", query, max_companies)
    search_fetch_count = max(max_companies * 4, 15)

    raw_urls: list[str] = []
    try:
        raw_urls = await asyncio.to_thread(_run_ddgs_search, query, search_fetch_count)
    except Exception as exc:
        logger.warning("Primary search backend failed (%s); falling back gracefully", exc)

    direct_candidates: list[str] = []
    listicle_candidates: list[str] = []
    seen_domains: set[str] = set()

    def _classify_results(urls_batch: list[str]) -> None:
        for raw in urls_batch:
            validation = normalize_and_validate_url(raw)
            if not validation.is_valid_format or not validation.normalized_url:
                continue
            url = validation.normalized_url
            if any(marker in url.lower() for marker in _AD_PATH_MARKERS):
                continue
            domain = extract_root_domain(url)
            if not domain or domain in seen_domains:
                continue

            if _is_company_homepage_candidate(url) and not _is_listicle_path(url):
                seen_domains.add(domain)
                direct_candidates.append(_root_homepage(url))
            elif not any(
                domain == blk or domain.endswith(f".{blk}")
                for blk in _BLOCKED_LISTICLE_DOMAINS
            ):
                listicle_candidates.append(url)

    _classify_results(raw_urls)

    # If initial query mostly returned paywalled directories, run a targeted company-homepage query
    if len(direct_candidates) < max_companies:
        refined_query = (
            f"{query} pricing OR platform "
            "-site:crunchbase.com -site:dealroom.co -site:seedtable.com -site:linkedin.com"
        )
        try:
            extra_urls = await asyncio.to_thread(_run_ddgs_search, refined_query, search_fetch_count)
            _classify_results(extra_urls)
        except Exception:
            pass

    discovered = direct_candidates[:max_companies]

    # Mine accessible editorial listicles if we still need more company websites
    if len(discovered) < max_companies and fetcher is not None and listicle_candidates:
        logger.info(
            "Found %d direct sites; extracting outbound company links from %d accessible listing page(s)",
            len(discovered),
            min(4, len(listicle_candidates)),
        )
        for article_url in listicle_candidates[:4]:
            if len(discovered) >= max_companies:
                break
            mined = await _extract_companies_from_listicle(
                fetcher, article_url, limit=max_companies - len(discovered)
            )
            for company_url in mined:
                domain = extract_root_domain(company_url)
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    discovered.append(company_url)
                    if len(discovered) >= max_companies:
                        break

    logger.info("Discovered %d unique company URLs for query %r", len(discovered), query)
    return discovered[:max_companies]


def find_next_pagination_url(html: str, current_url: str) -> str | None:
    """Locate the next page URL from pagination controls (`rel=next`, Next links, `?page=`)."""
    soup = BeautifulSoup(html, "lxml")

    rel_next = soup.find("link", rel=lambda v: v and "next" in [x.lower() for x in (v if isinstance(v, list) else [v])])
    if rel_next and rel_next.get("href"):
        return urljoin(current_url, rel_next["href"])

    a_rel_next = soup.find("a", rel=lambda v: v and "next" in [x.lower() for x in (v if isinstance(v, list) else [v])])
    if a_rel_next and a_rel_next.get("href"):
        return urljoin(current_url, a_rel_next["href"])

    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).lower()
        aria = (anchor.get("aria-label") or "").lower()
        classes = " ".join(anchor.get("class") or []).lower()
        if text in ("next", "next page", "›", "»", "→") or "next" in aria or "next" in classes:
            href = anchor["href"].strip()
            if href and not href.startswith(("#", "javascript:")):
                candidate = urljoin(current_url, href)
                if candidate != current_url:
                    return candidate

    return None


async def discover_paginated_urls(
    fetcher: PageFetcher,
    start_url: str,
    first_html: str,
    max_pages: int = 3,
) -> list[tuple[str, str]]:
    """Follow pagination links up to `max_pages`, returning list of `(url, html)` pairs."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = {start_url}
    current_url = start_url
    current_html = first_html

    for _ in range(max_pages - 1):
        next_url = find_next_pagination_url(current_html, current_url)
        if not next_url or next_url in seen:
            break
        if extract_root_domain(next_url) != extract_root_domain(start_url):
            break
        seen.add(next_url)
        fetched = await fetcher.fetch(next_url)
        if not fetched.ok:
            break
        pages.append((fetched.final_url, fetched.html))
        current_url = fetched.final_url
        current_html = fetched.html

    return pages


def _score_subpage_url(url: str, anchor_text: str = "") -> int:
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return -100
    score = 0
    combined = f"{path} {anchor_text.lower()}"
    for keyword, weight in _SUBPAGE_PRIORITIES:
        if keyword in path or keyword.lstrip("/") in combined:
            score = max(score, weight)
    # Prefer shallow URLs over deep archive paths
    depth = len([seg for seg in path.split("/") if seg])
    if depth > 3:
        score -= (depth - 3) * 15
    return score


async def discover_company_subpages(
    fetcher: PageFetcher,
    homepage_url: str,
    homepage_html: str,
    max_subpages: int = 4,
    follow_sitemap: bool = True,
) -> list[str]:
    """Discover high-value internal company pages (About, Contact, Products, Careers, Blog, etc.)."""
    if max_subpages <= 0:
        return []

    root_domain = extract_root_domain(homepage_url)
    candidates: dict[str, int] = {}

    soup = BeautifulSoup(homepage_html, "lxml")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(homepage_url, href).split("#")[0].rstrip("/")
        if extract_root_domain(full_url) != root_domain:
            continue
        if full_url.rstrip("/") == homepage_url.rstrip("/"):
            continue
        score = _score_subpage_url(full_url, anchor.get_text(" ", strip=True))
        if score > 0:
            candidates[full_url] = max(candidates.get(full_url, 0), score)

    if follow_sitemap and len(candidates) < max_subpages * 2:
        parsed = urlparse(homepage_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        try:
            sm_page = await fetcher.fetch(sitemap_url)
            if sm_page.ok and "<loc>" in sm_page.html.lower():
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm_page.html, re.IGNORECASE)
                for loc in locs[:150]:
                    loc_clean = loc.strip().rstrip("/")
                    if extract_root_domain(loc_clean) != root_domain:
                        continue
                    if loc_clean == homepage_url.rstrip("/"):
                        continue
                    score = _score_subpage_url(loc_clean)
                    if score > 0:
                        candidates[loc_clean] = max(candidates.get(loc_clean, 0), score)
        except Exception:
            pass

    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    return [url for url, _ in ranked[:max_subpages]]
