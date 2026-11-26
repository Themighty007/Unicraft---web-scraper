"""Level 1 (Basic) extraction: Company Name, Website URL, Emails, and Phone Numbers."""

from __future__ import annotations

import re
from urllib.parse import unquote

from scraper.extractors import PageBundle, clean_text, dedupe_preserve_order
from scraper.models import ContactInfo

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24}\b"
)
_OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([a-zA-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\sat\s)\s*([a-zA-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*([a-zA-Z]{2,15})\b",
    re.IGNORECASE,
)

# Filter out asset filenames, tracking domains, and template placeholder emails
_IGNORED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "yourdomain.com", "domain.com",
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "w3.org", "schema.org",
    "creativecommons.org", "googleapis.com",
}
_IGNORED_EMAIL_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff", ".woff2",
)

# Phone regex prioritizing international formatting or clear separators to avoid dates/IDs
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{1,4})?(?!\d)"
)
_DATE_LIKE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")
_GENERIC_TITLE_NOISE = {
    "home", "homepage", "official site", "official website", "welcome", "index",
    "untitled", "loading", "just a moment...",
}


def _clean_title_to_name(raw_title: str) -> str:
    title = clean_text(raw_title)
    if not title:
        return ""
    for sep in (" | ", " — ", " – ", " - ", " :: ", ": "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            # Prefer the shortest concise brand segment that isn't generic noise
            for part in parts:
                if part.lower() not in _GENERIC_TITLE_NOISE and len(part) <= 45:
                    return part
            return parts[0]
    return title[:80]


def extract_company_name(bundles: list[PageBundle], domain: str) -> str:
    """Extract canonical company name across JSON-LD, OpenGraph, title, copyright, and domain."""
    if not bundles:
        return domain.split(".")[0].replace("-", " ").title()

    home = bundles[0]

    # 1. JSON-LD Organization / Corporation / WebSite
    for node in home.json_ld:
        node_type = str(node.get("@type", "")).lower()
        if any(t in node_type for t in ("organization", "corporation", "localbusiness", "website")):
            name = clean_text(str(node.get("name") or node.get("legalName") or ""))
            if name and name.lower() not in _GENERIC_TITLE_NOISE:
                return name

    # 2. OpenGraph / meta site_name
    for key in ("og:site_name", "application-name", "twitter:site"):
        val = home.meta.get(key)
        if val:
            cleaned = val.lstrip("@").strip()
            if cleaned and cleaned.lower() not in _GENERIC_TITLE_NOISE:
                return cleaned

    # 3. Title tag cleanup
    if home.soup.title and home.soup.title.string:
        candidate = _clean_title_to_name(home.soup.title.string)
        if candidate and candidate.lower() not in _GENERIC_TITLE_NOISE:
            return candidate

    # 4. Copyright notice fallback
    match = re.search(
        r"(?:©|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?([A-Z][A-Za-z0-9&.,' -]{1,45})",
        home.visible_text,
        re.IGNORECASE,
    )
    if match:
        candidate = clean_text(match.group(1)).rstrip(".,-")
        if candidate and candidate.lower() not in ("all rights reserved",):
            return candidate

    # 5. Domain stem fallback
    stem = domain.split(".")[0] if domain else "Unknown Company"
    return stem.replace("-", " ").replace("_", " ").title()


def _is_valid_email(email: str) -> bool:
    lower = email.lower().strip()
    if "@" not in lower:
        return False
    local, _, domain = lower.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if domain in _IGNORED_EMAIL_DOMAINS or any(domain.endswith(f".{d}") for d in _IGNORED_EMAIL_DOMAINS):
        return False
    if lower.endswith(_IGNORED_EMAIL_EXTENSIONS):
        return False
    if len(lower) > 90:
        return False
    return True


def extract_emails(bundles: list[PageBundle]) -> list[str]:
    """Extract valid email addresses from mailto links, JSON-LD, and visible page text."""
    raw_emails: list[str] = []

    for bundle in bundles:
        for node in bundle.json_ld:
            if isinstance(node.get("email"), str):
                raw_emails.append(node["email"].replace("mailto:", "").strip())

        for anchor in bundle.soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = unquote(href[7:].split("?")[0]).strip()
                raw_emails.append(addr)

        raw_emails.extend(_EMAIL_RE.findall(bundle.visible_text))
        for user, host, tld in _OBFUSCATED_EMAIL_RE.findall(bundle.visible_text):
            raw_emails.append(f"{user}@{host}.{tld}")

    valid = [email.lower() for email in raw_emails if _is_valid_email(email)]
    return dedupe_preserve_order(valid)


def _normalize_phone(raw: str) -> str | None:
    cleaned = clean_text(raw)
    if _DATE_LIKE_RE.match(cleaned):
        return None
    digits = re.sub(r"\D", "", cleaned)
    if not (7 <= len(digits) <= 15):
        return None
    # Reject repeated identical digits like 0000000 or 1111111111
    if len(set(digits)) <= 2:
        return None
    return cleaned


def extract_phone_numbers(bundles: list[PageBundle]) -> list[str]:
    """Extract phone numbers from tel: links, JSON-LD, and structured text blocks."""
    candidates: list[str] = []

    for bundle in bundles:
        for node in bundle.json_ld:
            tel = node.get("telephone")
            if isinstance(tel, str):
                norm = _normalize_phone(tel)
                if norm:
                    candidates.append(norm)

        for anchor in bundle.soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.lower().startswith("tel:"):
                norm = _normalize_phone(unquote(href[4:]))
                if norm:
                    candidates.append(norm)

        # Search high-signal contact containers first, then general text
        contact_containers = bundle.soup.find_all(["address", "footer"])
        for container in contact_containers:
            text = clean_text(container.get_text(" ", strip=True))
            for match in _PHONE_RE.findall(text):
                norm = _normalize_phone(match)
                if norm:
                    candidates.append(norm)

        if "/contact" in bundle.url.lower() or not candidates:
            for match in _PHONE_RE.findall(bundle.visible_text):
                norm = _normalize_phone(match)
                if norm:
                    candidates.append(norm)

    # Deduplicate by normalized digit string to avoid "+1 555-0100" and "+1-555-0100" duplicates
    seen_digits: set[str] = set()
    final: list[str] = []
    for phone in candidates:
        digit_key = re.sub(r"\D", "", phone)
        if digit_key not in seen_digits:
            seen_digits.add(digit_key)
            final.append(phone)
    return final[:8]


def extract_basic_info(bundles: list[PageBundle], domain: str) -> tuple[str, ContactInfo]:
    """Run Level 1 extraction and return `(company_name, ContactInfo)`."""
    name = extract_company_name(bundles, domain)
    contact = ContactInfo(
        emails=extract_emails(bundles),
        phone_numbers=extract_phone_numbers(bundles),
    )
    return name, contact
