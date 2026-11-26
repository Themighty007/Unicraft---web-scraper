"""Level 3 (Advanced) extraction: Tech stack, current projects, competitors, positioning, and enrichment."""

from __future__ import annotations

import re
from collections import Counter

import httpx

from scraper.config import TECH_SIGNATURES
from scraper.extractors import PageBundle, clean_text, dedupe_preserve_order
from scraper.models import (
    AdvancedInsights,
    CompetitiveLandscape,
    EnrichmentData,
    SocialProfiles,
    TechStackDetail,
)

_PROJECT_VERBS = (
    "introducing", "announcing", "launching", "released", "release",
    "open-source", "open source", "project", "initiative", "roadmap",
    "beta", "preview", "research", "lab", "accelerator",
)

_COMPETITOR_PHRASES = (
    re.compile(r"(?:alternative\s+to|compared\s+to|switch\s+from|migrate\s+from|migrating\s+from|replace|versus|vs\.?)\s+([A-Z][A-Za-z0-9.+-]{1,28}(?:\s+[A-Z][A-Za-z0-9.+-]{1,20})?)", re.MULTILINE),
)
_COMPETITOR_STOPWORDS = {
    "the", "our", "your", "their", "other", "legacy", "traditional", "manual",
    "spreadsheets", "email", "competitors", "solutions", "tools", "platforms",
    "enterprise", "cloud", "any", "all", "more", "what", "why", "how",
    "coders", "developers", "engineers", "builders", "teams", "users", "clients",
    "vibe", "everyone", "anyone", "nothing", "something",
}

_POSITIONING_PROFILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Enterprise Category Leader", "Leader / Enterprise", ("#1 ", "leading ", "market leader", "fortune 500", "enterprise-grade", "trusted by thousands")),
    ("Open-Source Challenger", "Open-Source Challenger", ("open source", "open-source", "apache 2.0", "mit license", "self-hosted", "community edition")),
    ("AI-Native Innovator", "AI-Native Innovator", ("ai-native", "ai-powered", "autonomous", "agentic", "foundation model", "copilot")),
    ("Developer-First Platform", "Developer-First", ("developer-first", "built for developers", "api-first", "sdk", "cli", "infrastructure for developers")),
    ("Cost-Effective Challenger", "Value Challenger", ("fraction of the cost", "transparent pricing", "free tier", "save up to", "no hidden fees", "lightweight alternative")),
)

_FUNDING_RE = re.compile(
    r"\b(Series\s+[A-F]|Seed\s+(?:round|funded|stage)|Pre-Seed|IPO|NYSE:\s*[A-Z]{1,5}|NASDAQ:\s*[A-Z]{1,5}|bootstrapped)\b",
    re.IGNORECASE,
)


def extract_tech_stack(bundles: list[PageBundle]) -> TechStackDetail:
    """Detect technologies via HTTP headers, asset/HTML signatures, and careers/tech mentions."""
    categories: dict[str, list[str]] = {}
    all_tech: list[str] = []

    raw_blob = "\n".join(
        [
            " ".join(f"{k}: {v}" for k, v in bundle.headers.items())
            + "\n"
            + bundle.html[:250_000]
            for bundle in bundles
        ]
    ).lower()

    # Use word-boundary matching on text keywords primarily from engineering/careers/about/home pages
    text_blob = " ".join(bundle.visible_text[:15_000] for bundle in bundles).lower()

    for tech_name, category, html_markers, text_keywords in TECH_SIGNATURES:
        matched = False
        if any(marker.lower() in raw_blob for marker in html_markers):
            matched = True
        elif text_keywords:
            for kw in text_keywords:
                if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", text_blob):
                    matched = True
                    break
        if matched:
            all_tech.append(tech_name)
            categories.setdefault(category, []).append(tech_name)

    all_tech = dedupe_preserve_order(all_tech)
    deduped_categories = {
        cat: dedupe_preserve_order(items) for cat, items in categories.items()
    }
    return TechStackDetail(all_technologies=all_tech, categories=deduped_categories)


def extract_current_projects(bundles: list[PageBundle]) -> list[str]:
    """Extract active initiatives, launches, research labs, or recent project announcements."""
    projects: list[str] = []

    for bundle in bundles:
        url_lower = bundle.url.lower()
        is_project_page = any(
            seg in url_lower for seg in ("/blog", "/news", "/project", "/research", "/changelog", "/release")
        )
        for heading in bundle.soup.find_all(["h1", "h2", "h3", "article"]):
            if heading.name == "article":
                h_tag = heading.find(["h2", "h3", "h4"])
                txt = clean_text(h_tag.get_text(" ", strip=True)) if h_tag else ""
            else:
                txt = clean_text(heading.get_text(" ", strip=True))
            if not (10 <= len(txt) <= 130):
                continue
            lower = txt.lower()
            if is_project_page or any(verb in lower for verb in _PROJECT_VERBS):
                projects.append(txt)

    return dedupe_preserve_order(projects)[:8]


def extract_competitive_landscape(
    bundles: list[PageBundle],
    company_name: str,
) -> CompetitiveLandscape:
    """Identify mentioned competitors/alternatives and infer market positioning."""
    competitors: list[str] = []
    self_lower = company_name.lower()

    for bundle in bundles:
        # 1. URL pattern `/vs-competitor` or `/alternatives/competitor`
        vs_match = re.search(r"/(?:vs-|compare/|alternatives/)([a-z0-9-]+)", bundle.url.lower())
        if vs_match:
            cand = vs_match.group(1).replace("-", " ").title()
            if cand.lower() not in self_lower and cand.lower() not in _COMPETITOR_STOPWORDS:
                competitors.append(cand)

        # 2. Anchor links referencing comparison pages
        for anchor in bundle.soup.find_all("a", href=True):
            href = anchor["href"].lower()
            if any(k in href for k in ("/vs-", "/compare/", "-vs-", "/alternative")):
                txt = clean_text(anchor.get_text(" ", strip=True))
                vs_text = re.search(r"(?:vs\.?|alternative to)\s+([A-Z][A-Za-z0-9.+-]{1,30})", txt)
                if vs_text:
                    cand = vs_text.group(1)
                    if cand.lower() not in self_lower and cand.lower() not in _COMPETITOR_STOPWORDS:
                        competitors.append(cand)

        # 3. Text comparison mentions
        for pattern in _COMPETITOR_PHRASES:
            for match in pattern.findall(bundle.visible_text):
                cand = clean_text(match).rstrip(".,;:")
                words = [w.lower() for w in cand.split()]
                if (
                    cand
                    and not any(w in _COMPETITOR_STOPWORDS for w in words)
                    and cand.lower() not in self_lower
                    and self_lower not in cand.lower()
                ):
                    competitors.append(cand)

    # Positioning assessment
    corpus = " ".join(bundle.visible_text[:10_000] for bundle in bundles[:3]).lower()
    best_label: str | None = None
    best_signals: list[str] = []
    best_hits = 0

    for label, _, markers in _POSITIONING_PROFILES:
        matched_markers = [m.strip() for m in markers if m in corpus]
        if len(matched_markers) > best_hits:
            best_hits = len(matched_markers)
            best_label = label
            best_signals = matched_markers

    return CompetitiveLandscape(
        competitors_mentioned=dedupe_preserve_order(competitors)[:10],
        market_positioning=best_label,
        positioning_signals=best_signals[:6],
    )


async def enrich_company_data(
    bundles: list[PageBundle],
    company_name: str,
    socials: SocialProfiles,
    enrich_external: bool = True,
    timeout: float = 6.0,
) -> EnrichmentData:
    """Augment insights using JSON-LD metadata, text funding signals, and optional public GitHub/Wikidata APIs."""
    enrichment = EnrichmentData()

    # 1. Local structured metadata (JSON-LD & text signals)
    for bundle in bundles:
        for node in bundle.json_ld:
            emp = node.get("numberOfEmployees")
            if isinstance(emp, dict) and (emp.get("value") or emp.get("minValue")):
                val = emp.get("value") or f"{emp.get('minValue')}-{emp.get('maxValue', '+')}"
                enrichment.company_size = str(val)
                if "Schema.org JSON-LD" not in enrichment.sources:
                    enrichment.sources.append("Schema.org JSON-LD")
            elif isinstance(emp, (int, str)) and str(emp).strip():
                enrichment.company_size = str(emp).strip()
                if "Schema.org JSON-LD" not in enrichment.sources:
                    enrichment.sources.append("Schema.org JSON-LD")

        if not enrichment.funding_stage:
            f_match = _FUNDING_RE.search(bundle.visible_text)
            if f_match:
                enrichment.funding_stage = clean_text(f_match.group(1))
                if "Site Disclosures" not in enrichment.sources:
                    enrichment.sources.append("Site Disclosures")

    if not enrich_external:
        return enrichment

    # 2. Optional public GitHub Organization enrichment (no API key required)
    if socials.github:
        parts = [p for p in socials.github.split("github.com/")[-1].split("/") if p]
        if parts:
            org = parts[0]
            try:
                async with httpx.AsyncClient(
                    timeout=min(timeout, 5.0),
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "unicraft-scraper"},
                ) as client:
                    org_resp = await client.get(f"https://api.github.com/orgs/{org}")
                    if org_resp.status_code == 200:
                        org_data = org_resp.json()
                        enrichment.github_org = org
                        enrichment.github_public_repos = org_data.get("public_repos")
                        enrichment.github_followers = org_data.get("followers")
                        enrichment.sources.append("GitHub Public API")

                        repos_resp = await client.get(
                            f"https://api.github.com/orgs/{org}/repos?sort=updated&per_page=15"
                        )
                        if repos_resp.status_code == 200:
                            repos = repos_resp.json()
                            repos_sorted = sorted(
                                repos, key=lambda r: r.get("stargazers_count", 0), reverse=True
                            )
                            langs = Counter(
                                r["language"] for r in repos if r.get("language")
                            )
                            enrichment.github_top_languages = [lang for lang, _ in langs.most_common(5)]
                            enrichment.github_top_projects = [
                                f"{r['name']} ({r.get('stargazers_count', 0)}★)"
                                for r in repos_sorted[:4]
                            ]
            except Exception:
                pass

    # 3. Optional public Wikidata summary enrichment (zero-key)
    if company_name and len(company_name) >= 2:
        try:
            async with httpx.AsyncClient(
                timeout=min(timeout, 4.0),
                headers={"User-Agent": "unicraft-scraper/0.1"},
            ) as client:
                wd_resp = await client.get(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbsearchentities",
                        "search": company_name,
                        "language": "en",
                        "format": "json",
                        "limit": 3,
                    },
                )
                if wd_resp.status_code == 200:
                    hits = wd_resp.json().get("search") or []
                    for hit in hits:
                        desc = (hit.get("description") or "").lower()
                        if any(
                            w in desc
                            for w in (
                                "company", "corporation", "startup", "software",
                                "enterprise", "provider", "platform", "analytics",
                                "cloud", "hosting", "technology", "database",
                            )
                        ):
                            enrichment.wikidata_id = hit.get("id")
                            enrichment.wikidata_description = hit.get("description")
                            enrichment.sources.append("Wikidata")
                            break
        except Exception:
            pass

    enrichment.sources = dedupe_preserve_order(enrichment.sources)
    return enrichment


async def extract_advanced_info(
    bundles: list[PageBundle],
    company_name: str,
    socials: SocialProfiles,
    enrich_external: bool = True,
) -> AdvancedInsights:
    """Run full Level 3 extraction and return AdvancedInsights."""
    tech_stack = extract_tech_stack(bundles)
    projects = extract_current_projects(bundles)

    # Combine GitHub top repos as project/tech evidence if found
    enrichment = await enrich_company_data(
        bundles,
        company_name=company_name,
        socials=socials,
        enrich_external=enrich_external,
    )
    if enrichment.github_top_languages:
        for lang in enrichment.github_top_languages:
            if lang not in tech_stack.all_technologies:
                tech_stack.all_technologies.append(lang)
                tech_stack.categories.setdefault("GitHub Languages", []).append(lang)

    competitive = extract_competitive_landscape(bundles, company_name)

    return AdvancedInsights(
        tech_stack=tech_stack,
        current_projects=projects,
        competitive_landscape=competitive,
        enrichment=enrichment,
    )
