"""Shared HTML parsing bundles and utilities for Level 1, 2, 3, and custom extractors."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from bs4 import BeautifulSoup
from lxml import html as lxml_html


@dataclass
class PageBundle:
    """Pre-parsed page representation shared across all extraction stages."""

    url: str
    html: str
    headers: dict[str, str]
    soup: BeautifulSoup
    lxml_tree: Any
    visible_text: str
    meta: dict[str, str] = field(default_factory=dict)
    json_ld: list[dict[str, Any]] = field(default_factory=list)


def clean_text(value: str | None) -> str:
    """Collapse internal whitespace and strip surrounding whitespace."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    """Deduplicate strings case-insensitively while preserving first-seen casing and order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = clean_text(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _flatten_json_ld(data: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(data, dict):
        out.append(data)
        if isinstance(data.get("@graph"), list):
            for node in data["@graph"]:
                _flatten_json_ld(node, out)
    elif isinstance(data, list):
        for item in data:
            _flatten_json_ld(item, out)


def parse_page_bundle(
    url: str,
    html: str,
    headers: dict[str, str] | None = None,
) -> PageBundle:
    """Parse raw HTML once into BeautifulSoup, lxml, metadata, JSON-LD, and clean visible text."""
    soup = BeautifulSoup(html or "", "lxml")

    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        content = tag.get("content")
        if key and content:
            meta.setdefault(str(key).strip().lower(), clean_text(str(content)))

    json_ld: list[dict[str, Any]] = []
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
            _flatten_json_ld(parsed, json_ld)
        except Exception:
            continue

    # Build clean visible text from a light copy without script/style noise
    text_soup = BeautifulSoup(html or "", "lxml")
    for noise in text_soup(["script", "style", "noscript", "svg"]):
        noise.decompose()
    visible_text = clean_text(text_soup.get_text(" ", strip=True))

    try:
        lxml_tree = lxml_html.fromstring(html or "<html></html>")
    except Exception:
        lxml_tree = lxml_html.fromstring("<html></html>")

    return PageBundle(
        url=url,
        html=html or "",
        headers={k.lower(): v for k, v in (headers or {}).items()},
        soup=soup,
        lxml_tree=lxml_tree,
        visible_text=visible_text,
        meta=meta,
        json_ld=json_ld,
    )
