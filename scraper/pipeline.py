"""End-to-end scraping pipeline orchestrating discovery, validation, crawling, extraction, and export."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from scraper.discovery import (
    discover_companies_from_query,
    discover_company_subpages,
    discover_paginated_urls,
)
from scraper.exporter import export_results
from scraper.extractors import PageBundle, dedupe_preserve_order, parse_page_bundle
from scraper.extractors.advanced import extract_advanced_info
from scraper.extractors.basic import extract_basic_info
from scraper.extractors.custom import apply_custom_rules
from scraper.extractors.medium import extract_medium_info
from scraper.fetcher import FetchedPage, PageFetcher
from scraper.logging_utils import get_logger
from scraper.models import (
    CompanyProfile,
    ExtractionLevel,
    PageError,
    ScraperConfig,
    ScrapeRunSummary,
)
from scraper.validator import extract_root_domain, validate_seed_urls

logger = get_logger("pipeline")

ProgressCallback = Callable[[dict], Awaitable[None] | None]


async def _emit(callback: ProgressCallback | None, payload: dict) -> None:
    if callback is None:
        return
    try:
        res = callback(payload)
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        pass


async def _scrape_single_company(
    target_url: str,
    config: ScraperConfig,
    fetcher: PageFetcher,
) -> CompanyProfile:
    """Crawl homepage + relevant subpages for one company and extract configured levels."""
    domain = extract_root_domain(target_url)
    errors: list[PageError] = []
    warnings: list[str] = []
    crawled_pages: list[FetchedPage] = []

    home = await fetcher.fetch(target_url)
    if not home.ok:
        errors.append(
            PageError(
                url=target_url,
                stage="fetch_homepage",
                error_type=home.error_type or "FetchError",
                message=home.error or "Failed to fetch homepage",
                status_code=home.status_code,
            )
        )
        return CompanyProfile(
            company_name=domain.split(".")[0].replace("-", " ").title() or target_url,
            website_url=target_url,
            domain=domain,
            extraction_level=config.level,
            pages_crawled=[],
            render_methods_used=[home.render_method],
            errors=errors,
            warnings=["Homepage could not be retrieved; basic domain fallback used."],
        )

    crawled_pages.append(home)
    canonical_url = home.final_url
    domain = extract_root_domain(canonical_url) or domain

    # Crawl relevant internal subpages when Level >= MEDIUM or custom rules exist
    should_crawl_subpages = (
        config.max_pages_per_company > 1
        and (config.level != ExtractionLevel.BASIC or bool(config.custom_rules))
    )
    if should_crawl_subpages:
        subpages = await discover_company_subpages(
            fetcher=fetcher,
            homepage_url=canonical_url,
            homepage_html=home.html,
            max_subpages=config.max_pages_per_company - 1,
            follow_sitemap=config.follow_sitemap,
        )
        if subpages:
            sub_results = await asyncio.gather(
                *(fetcher.fetch(sub_url) for sub_url in subpages),
                return_exceptions=True,
            )
            for sub_url, res in zip(subpages, sub_results):
                if isinstance(res, Exception):
                    errors.append(
                        PageError(
                            url=sub_url,
                            stage="fetch_subpage",
                            error_type=type(res).__name__,
                            message=str(res),
                        )
                    )
                elif res.ok:
                    crawled_pages.append(res)
                else:
                    errors.append(
                        PageError(
                            url=sub_url,
                            stage="fetch_subpage",
                            error_type=res.error_type or "FetchError",
                            message=res.error or "Failed to fetch subpage",
                            status_code=res.status_code,
                        )
                    )

    # Optional pagination traversal on blog/projects/news pages (within budget)
    if (
        config.follow_pagination
        and config.level == ExtractionLevel.ADVANCED
        and len(crawled_pages) < config.max_pages_per_company
    ):
        for page in list(crawled_pages[1:]):
            if any(k in page.url.lower() for k in ("/blog", "/news", "/project", "/research")):
                remaining = config.max_pages_per_company - len(crawled_pages)
                if remaining <= 0:
                    break
                extra = await discover_paginated_urls(
                    fetcher=fetcher,
                    start_url=page.final_url,
                    first_html=page.html,
                    max_pages=min(config.max_pagination_pages, remaining + 1),
                )
                for extra_url, extra_html in extra:
                    crawled_pages.append(
                        FetchedPage(
                            url=extra_url,
                            final_url=extra_url,
                            status_code=200,
                            html=extra_html,
                            render_method="httpx",
                        )
                    )
                break

    bundles: list[PageBundle] = [
        parse_page_bundle(p.final_url, p.html, p.headers) for p in crawled_pages
    ]

    # Level 1 (Basic)
    company_name, contact = extract_basic_info(bundles, domain)
    if not contact.emails and not contact.phone_numbers:
        warnings.append("No public email or phone number detected on crawled pages.")

    # Level 2 (Medium)
    medium_info = None
    if config.level in (ExtractionLevel.MEDIUM, ExtractionLevel.ADVANCED):
        medium_info = extract_medium_info(bundles, domain=domain)

    # Level 3 (Advanced)
    advanced_info = None
    if config.level == ExtractionLevel.ADVANCED:
        socials = (
            medium_info.social_profiles
            if medium_info
            else extract_medium_info(bundles, domain=domain).social_profiles
        )
        advanced_info = await extract_advanced_info(
            bundles=bundles,
            company_name=company_name,
            socials=socials,
            enrich_external=config.enrich_external,
        )

    # Custom user CSS / XPath / Regex rules
    custom_fields, custom_errors = apply_custom_rules(bundles, config.custom_rules)
    errors.extend(custom_errors)

    return CompanyProfile(
        company_name=company_name,
        website_url=canonical_url,
        domain=domain,
        extraction_level=config.level,
        pages_crawled=[p.final_url for p in crawled_pages],
        render_methods_used=dedupe_preserve_order(p.render_method for p in crawled_pages),
        contact=contact,
        medium=medium_info,
        advanced=advanced_info,
        custom_fields=custom_fields,
        errors=errors,
        warnings=warnings,
    )


async def run_scrape_pipeline(
    config: ScraperConfig,
    job_id: str | None = None,
    on_progress: ProgressCallback | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    save_outputs: bool = True,
) -> ScrapeRunSummary:
    """Execute a complete scraping workflow from discovery/validation through structured export."""
    run_id = job_id or uuid.uuid4().hex[:8]
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    summary = ScrapeRunSummary(
        job_id=run_id,
        status="running",
        started_at=started_at,
        query=config.query,
        seed_urls=list(config.seed_urls),
        level=config.level,
    )

    if not config.query and not config.seed_urls:
        raise ValueError("Provide either a search `query` or at least one `seed_urls` entry.")

    await _emit(on_progress, {"event": "started", "job_id": run_id, "stage": "initializing"})

    target_urls: list[str] = []
    run_errors: list[PageError] = []

    async with PageFetcher(config, transport=transport) as fetcher:
        # 1. Validate explicit seed URLs
        if config.seed_urls:
            await _emit(
                on_progress,
                {
                    "event": "validating",
                    "message": f"Validating {len(config.seed_urls)} seed URL(s)...",
                },
            )
            validations = await validate_seed_urls(
                config.seed_urls,
                verify_reachability=config.verify_reachability,
                timeout=config.timeout_seconds,
                proxy=config.proxies[0] if config.proxies else None,
                transport=transport,
            )
            for v in validations:
                if not v.is_valid_format:
                    msg = f"Invalid URL '{v.raw_input}': {v.error}"
                    logger.error(msg)
                    run_errors.append(
                        PageError(
                            url=v.raw_input,
                            stage="validate_input",
                            error_type="InvalidURL",
                            message=v.error or "Invalid URL format",
                        )
                    )
                elif config.verify_reachability and v.is_reachable is False:
                    msg = f"Unreachable URL '{v.normalized_url}': {v.error}"
                    logger.warning(msg)
                    run_errors.append(
                        PageError(
                            url=v.normalized_url or v.raw_input,
                            stage="reachability_check",
                            error_type="UnreachableURL",
                            message=v.error or "Host unreachable",
                            status_code=v.status_code,
                        )
                    )
                else:
                    chosen = v.final_url or v.normalized_url
                    if chosen:
                        target_urls.append(chosen)

        # 2. Execute search query discovery
        if config.query:
            await _emit(
                on_progress,
                {
                    "event": "discovering",
                    "message": f"Discovering companies for query: {config.query!r}",
                },
            )
            discovered = await discover_companies_from_query(
                query=config.query,
                max_companies=config.max_companies,
                fetcher=fetcher,
            )
            if not discovered:
                run_errors.append(
                    PageError(
                        url="search://query",
                        stage="query_discovery",
                        error_type="NoResultsFound",
                        message=f"No company URLs discovered for query: {config.query!r}",
                    )
                )
            for url in discovered:
                if url not in target_urls:
                    target_urls.append(url)

        # Deduplicate by root domain up to max_companies
        seen_domains: set[str] = set()
        unique_targets: list[str] = []
        for url in target_urls:
            dom = extract_root_domain(url)
            if dom and dom not in seen_domains:
                seen_domains.add(dom)
                unique_targets.append(url)
            if len(unique_targets) >= config.max_companies:
                break

        summary.total_targets = len(unique_targets)
        await _emit(
            on_progress,
            {
                "event": "targets_ready",
                "total": len(unique_targets),
                "targets": unique_targets,
            },
        )

        # 3. Scrape targets concurrently with bounded semaphore
        sem = asyncio.Semaphore(config.concurrency)

        async def _worker(idx: int, url: str) -> CompanyProfile:
            async with sem:
                logger.info("[%d/%d] Scraping %s", idx + 1, len(unique_targets), url)
                await _emit(
                    on_progress,
                    {
                        "event": "target_started",
                        "index": idx + 1,
                        "total": len(unique_targets),
                        "url": url,
                    },
                )
                profile = await _scrape_single_company(url, config, fetcher)
                summary.processed_targets += 1
                if profile.pages_crawled:
                    summary.succeeded += 1
                else:
                    summary.failed += 1
                await _emit(
                    on_progress,
                    {
                        "event": "target_completed",
                        "processed": summary.processed_targets,
                        "total": len(unique_targets),
                        "company": profile.company_name,
                        "url": profile.website_url,
                        "errors": len(profile.errors),
                    },
                )
                return profile

        profiles = await asyncio.gather(
            *(_worker(i, u) for i, u in enumerate(unique_targets))
        )
        summary.companies = list(profiles)
        summary.errors = run_errors + [err for p in profiles for err in p.errors]

    summary.completed_at = datetime.now(timezone.utc).isoformat()
    summary.duration_seconds = round(time.monotonic() - t0, 3)
    summary.status = "completed"

    if save_outputs:
        output_files = export_results(
            summary=summary,
            output_dir=config.output_dir,
            formats=list(config.formats),
        )
        summary.output_files = output_files
        logger.info(
            "Run %s finished in %.2fs (%d succeeded, %d failed). Outputs: %s",
            run_id,
            summary.duration_seconds,
            summary.succeeded,
            summary.failed,
            ", ".join(output_files),
        )

    await _emit(
        on_progress,
        {
            "event": "completed",
            "job_id": run_id,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "output_files": summary.output_files,
        },
    )
    return summary
