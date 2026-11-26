"""Level 2 (Medium) extraction: Social profiles, addresses, company overview, products, and industry."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from scraper.config import INDUSTRY_KEYWORDS
from scraper.extractors import PageBundle, clean_text, dedupe_preserve_order
from scraper.models import ExtendedCompanyInfo, SocialProfiles

_SOCIAL_PATTERNS: dict[str, tuple[re.Pattern[str], tuple[str, ...]]] = {
    "linkedin": (
        re.compile(r"https?://(?:[\w.-]+\.)?linkedin\.com/(?:company|in|school)/[A-Za-z0-9._%-]+/?", re.I),
        ("/share", "/sharing"),
    ),
    "twitter": (
        re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[A-Za-z0-9_]{1,30}/?", re.I),
        ("/intent", "/share", "/home", "/search", "/explore", "/i/", "/hashtag"),
    ),
    "github": (
        re.compile(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?/?", re.I),
        ("/login", "/signup", "/features", "/pricing", "/sponsors", "/marketplace", "/topics"),
    ),
    "youtube": (
        re.compile(r"https?://(?:www\.)?youtube\.com/(?:@[\w.-]+|c/[\w.-]+|channel/[\w.-]+|user/[\w.-]+)/?", re.I),
        ("/watch", "/embed", "/results"),
    ),
    "instagram": (
        re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]{1,35}/?", re.I),
        ("/p/", "/reel/", "/explore/"),
    ),
    "facebook": (
        re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.-]+/?", re.I),
        ("/sharer", "/share.php", "/dialog/"),
    ),
}

_FOUNDED_RE = re.compile(
    r"(?:founded(?:\s+in)?|established(?:\s+in)?|est\.?|since|incorporated(?:\s+in)?)\s*[:\-]?\s*((?:18|19|20)\d{2})\b",
    re.IGNORECASE,
)
_COPYRIGHT_RANGE_RE = re.compile(r"(?:©|copyright)\s*((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})", re.IGNORECASE)
_CITY_COUNTRY_RE = re.compile(
    r"\b([A-Z][a-zA-Z.-]+(?:\s+[A-Z][a-zA-Z.-]+){0,2},\s*(?:[A-Z]{2}|United States|USA|UK|United Kingdom|Germany|France|Netherlands|Sweden|Switzerland|Canada|India|Singapore|Ireland|Spain|Italy|Poland|Estonia|Finland|Norway|Denmark|Australia|Japan))\b"
)
_PRODUCT_NOISE = {
    "learn more", "read more", "get started", "sign up", "log in", "login",
    "contact us", "book a demo", "request a demo", "watch demo", "pricing",
    "overview", "home", "about", "careers", "blog", "privacy policy", "terms",
    "cookie policy", "see all", "view all",
}


def _pick_best_social(
    candidates: list[str],
    platform: str,
    domain_stem: str = "",
) -> str | None:
    if not candidates:
        return None
    if domain_stem:
        for url in candidates:
            if domain_stem in url.lower():
                if platform == "github":
                    path_parts = urlparse(url).path.strip("/").split("/")
                    if len(path_parts) == 1:
                        return url.rstrip("/")
                else:
                    return url.rstrip("/")
    if platform == "linkedin":
        # Prefer /company/ links over personal /in/ profiles
        for url in candidates:
            if "/company/" in url.lower():
                return url.rstrip("/")
    if platform == "github":
        # Prefer top-level organization profile URL over deep repo subpaths
        sorted_urls = sorted(candidates, key=lambda u: len(urlparse(u).path.strip("/").split("/")))
        return sorted_urls[0].rstrip("/")
    return candidates[0].rstrip("/")


def extract_social_profiles(bundles: list[PageBundle], domain: str = "") -> SocialProfiles:
    """Extract canonical social media URLs, prioritizing official header/footer/JSON-LD links."""
    collected: dict[str, list[str]] = {key: [] for key in _SOCIAL_PATTERNS}
    domain_stem = domain.split(".")[0].lower() if domain else ""

    for bundle in bundles:
        raw_links: list[str] = []
        # 1. JSON-LD sameAs (highest confidence)
        for node in bundle.json_ld:
            same_as = node.get("sameAs")
            if isinstance(same_as, str):
                raw_links.append(same_as)
            elif isinstance(same_as, list):
                raw_links.extend(str(item) for item in same_as if item)

        # 2. Header / Footer links (official brand accounts)
        for zone in bundle.soup.find_all(["footer", "header", "nav"]):
            for anchor in zone.find_all("a", href=True):
                raw_links.append(anchor["href"].strip())

        # 3. Remaining anchors on page
        for anchor in bundle.soup.find_all("a", href=True):
            raw_links.append(anchor["href"].strip())

        for link in raw_links:
            for platform, (pattern, ignored_parts) in _SOCIAL_PATTERNS.items():
                match = pattern.match(link)
                if not match:
                    continue
                matched_url = match.group(0)
                if any(bad in matched_url.lower() for bad in ignored_parts):
                    continue
                collected[platform].append(matched_url)

    return SocialProfiles(
        linkedin=_pick_best_social(dedupe_preserve_order(collected["linkedin"]), "linkedin", domain_stem),
        twitter=_pick_best_social(dedupe_preserve_order(collected["twitter"]), "twitter", domain_stem),
        github=_pick_best_social(dedupe_preserve_order(collected["github"]), "github", domain_stem),
        youtube=_pick_best_social(dedupe_preserve_order(collected["youtube"]), "youtube", domain_stem),
        instagram=_pick_best_social(dedupe_preserve_order(collected["instagram"]), "instagram", domain_stem),
        facebook=_pick_best_social(dedupe_preserve_order(collected["facebook"]), "facebook", domain_stem),
    )


def extract_address_and_location(bundles: list[PageBundle]) -> tuple[str | None, str | None]:
    """Extract full physical address and high-level location (City, Country/State)."""
    address: str | None = None
    location: str | None = None

    for bundle in bundles:
        # 1. Structured Schema.org PostalAddress
        for node in bundle.json_ld:
            addr_obj = node.get("address")
            if isinstance(addr_obj, dict):
                parts = [
                    clean_text(str(addr_obj.get(k) or ""))
                    for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                ]
                parts = [p for p in parts if p]
                if parts and not address:
                    address = ", ".join(parts)
                loc_parts = [
                    clean_text(str(addr_obj.get(k) or ""))
                    for k in ("addressLocality", "addressRegion", "addressCountry")
                ]
                loc_parts = [p for p in loc_parts if p]
                if loc_parts and not location:
                    location = ", ".join(loc_parts)
            elif isinstance(addr_obj, str) and len(addr_obj) > 5 and not address:
                address = clean_text(addr_obj)

        # 2. HTML <address> tag
        if not address:
            for addr_tag in bundle.soup.find_all("address"):
                txt = clean_text(addr_tag.get_text(" ", strip=True))
                if 10 <= len(txt) <= 220:
                    address = txt
                    break

        # 3. City, State/Country heuristic on contact/about/footer
        if not location:
            search_zones = [
                clean_text(el.get_text(" ", strip=True))
                for el in bundle.soup.find_all(["address", "footer"])
            ]
            if "/contact" in bundle.url.lower() or "/about" in bundle.url.lower():
                search_zones.append(bundle.visible_text[:6000])
            for zone in search_zones:
                loc_match = _CITY_COUNTRY_RE.search(zone)
                if loc_match:
                    location = clean_text(loc_match.group(1))
                    break

    if not location and address:
        loc_match = _CITY_COUNTRY_RE.search(address)
        if loc_match:
            location = clean_text(loc_match.group(1))
        elif len(address) <= 80:
            location = address

    return address, location


def extract_overview(bundles: list[PageBundle]) -> tuple[str | None, str | None, int | None]:
    """Extract tagline, concise description, and founding year."""
    if not bundles:
        return None, None, None

    home = bundles[0]
    tagline: str | None = None
    description: str | None = None
    year_founded: int | None = None
    current_year = datetime.now(timezone.utc).year

    # Description priority: JSON-LD -> meta description -> og:description -> first strong paragraph
    for node in home.json_ld:
        desc = clean_text(str(node.get("description") or ""))
        if len(desc) >= 25:
            description = desc
            break

    if not description:
        for key in ("description", "og:description", "twitter:description"):
            val = home.meta.get(key)
            if val and len(val) >= 20:
                description = val
                break

    if not description:
        for p in home.soup.find_all("p"):
            txt = clean_text(p.get_text(" ", strip=True))
            if 50 <= len(txt) <= 350:
                description = txt
                break

    # Tagline: Hero H1 or clean title subtitle
    h1 = home.soup.find("h1")
    if h1:
        h1_text = clean_text(h1.get_text(" ", strip=True))
        if 8 <= len(h1_text) <= 130:
            tagline = h1_text
    if not tagline and home.soup.title and home.soup.title.string:
        raw_title = clean_text(home.soup.title.string)
        for sep in (" | ", " — ", " – ", " - "):
            if sep in raw_title:
                parts = [p.strip() for p in raw_title.split(sep) if len(p.strip()) > 8]
                if len(parts) > 1:
                    tagline = parts[-1]
                    break
    if not tagline and description:
        tagline = description.split(".")[0][:120]

    # Year founded: JSON-LD foundingDate -> explicit text -> copyright range start
    for bundle in bundles:
        for node in bundle.json_ld:
            fd = str(node.get("foundingDate") or "")
            year_match = re.search(r"\b((?:18|19|20)\d{2})\b", fd)
            if year_match:
                yr = int(year_match.group(1))
                if 1850 <= yr <= current_year:
                    year_founded = yr
                    break
        if year_founded:
            break

    if not year_founded:
        for bundle in bundles:
            match = _FOUNDED_RE.search(bundle.visible_text)
            if match:
                yr = int(match.group(1))
                if 1850 <= yr <= current_year:
                    year_founded = yr
                    break

    if not year_founded:
        for bundle in bundles:
            match = _COPYRIGHT_RANGE_RE.search(bundle.visible_text)
            if match:
                start_yr = int(match.group(1))
                end_yr = int(match.group(2))
                if 1990 <= start_yr < end_yr <= current_year + 1:
                    year_founded = start_yr
                    break

    return tagline, description, year_founded


def extract_products_services(bundles: list[PageBundle]) -> list[str]:
    """Extract primary products, platforms, or service offerings."""
    items: list[str] = []

    for bundle in bundles:
        # 1. JSON-LD Offer / Service / Product declarations
        for node in bundle.json_ld:
            for field in ("makesOffer", "hasOfferCatalog"):
                val = node.get(field)
                if isinstance(val, list):
                    for entry in val:
                        if isinstance(entry, dict) and entry.get("name"):
                            items.append(str(entry["name"]))
                elif isinstance(val, dict) and val.get("name"):
                    items.append(str(val["name"]))
            if "product" in str(node.get("@type", "")).lower() and node.get("name"):
                items.append(str(node["name"]))

        # 2. Navigation / Product / Solution pages
        url_lower = bundle.url.lower()
        is_product_page = any(
            seg in url_lower for seg in ("/product", "/solution", "/platform", "/service", "/feature")
        )
        if is_product_page:
            for heading in bundle.soup.find_all(["h2", "h3"]):
                txt = clean_text(heading.get_text(" ", strip=True))
                if 3 <= len(txt) <= 65 and txt.lower() not in _PRODUCT_NOISE:
                    items.append(txt)

        # 3. Top-level nav items under Products / Solutions menus
        for nav in bundle.soup.find_all(["nav", "header"]):
            for anchor in nav.find_all("a", href=True):
                href = anchor["href"].lower()
                txt = clean_text(anchor.get_text(" ", strip=True))
                if any(
                    seg in href for seg in ("/product", "/platform", "/solution", "/service")
                ) and 3 <= len(txt) <= 45:
                    if txt.lower() not in _PRODUCT_NOISE:
                        items.append(txt)

    return dedupe_preserve_order(items)[:12]


def classify_industry(bundles: list[PageBundle]) -> str | None:
    """Infer company industry/market sector with higher weight on homepage and metadata."""
    for bundle in bundles:
        for node in bundle.json_ld:
            ind = node.get("industry")
            if isinstance(ind, str) and len(ind.strip()) >= 3:
                return clean_text(ind)

    home_text = bundles[0].visible_text[:8000].lower() if bundles else ""
    meta_text = " ".join(bundles[0].meta.values()).lower() if bundles else ""
    sub_text = " ".join(bundle.visible_text[:4000] for bundle in bundles[1:3]).lower()

    best_industry: str | None = None
    best_score = 0
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        score = (
            sum(meta_text.count(kw) * 4 for kw in keywords)
            + sum(home_text.count(kw) * 2 for kw in keywords)
            + sum(sub_text.count(kw) for kw in keywords)
        )
        if score > best_score:
            best_score = score
            best_industry = industry

    return best_industry if best_score >= 2 else None


def extract_medium_info(bundles: list[PageBundle], domain: str = "") -> ExtendedCompanyInfo:
    """Run Level 2 extraction and return ExtendedCompanyInfo."""
    socials = extract_social_profiles(bundles, domain=domain)
    address, location = extract_address_and_location(bundles)
    tagline, description, year_founded = extract_overview(bundles)
    products = extract_products_services(bundles)
    industry = classify_industry(bundles)

    return ExtendedCompanyInfo(
        social_profiles=socials,
        address=address,
        location=location,
        tagline=tagline,
        description=description,
        year_founded=year_founded,
        operational_status="Active",
        products_services=products,
        industry=industry,
    )
