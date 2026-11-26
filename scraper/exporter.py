"""Exporters for JSON, CSV, and Markdown intelligence reports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from scraper.models import CompanyProfile, ScrapeRunSummary


def _flatten_company_row(company: CompanyProfile, custom_keys: list[str]) -> dict[str, Any]:
    """Flatten a multidimensional CompanyProfile into a clean tabular CSV row."""
    row: dict[str, Any] = {
        "company_name": company.company_name,
        "website_url": company.website_url,
        "domain": company.domain,
        "extraction_level": company.extraction_level.value,
        "emails": "; ".join(company.contact.emails),
        "phone_numbers": "; ".join(company.contact.phone_numbers),
        "pages_crawled_count": len(company.pages_crawled),
        "render_methods": "; ".join(company.render_methods_used),
    }

    if company.medium is not None:
        m = company.medium
        row.update(
            {
                "tagline": m.tagline or "",
                "description": m.description or "",
                "industry": m.industry or "",
                "year_founded": m.year_founded or "",
                "operational_status": m.operational_status,
                "address": m.address or "",
                "location": m.location or "",
                "products_services": "; ".join(m.products_services),
                "linkedin": m.social_profiles.linkedin or "",
                "twitter": m.social_profiles.twitter or "",
                "github": m.social_profiles.github or "",
                "youtube": m.social_profiles.youtube or "",
                "instagram": m.social_profiles.instagram or "",
                "facebook": m.social_profiles.facebook or "",
            }
        )

    if company.advanced is not None:
        a = company.advanced
        row.update(
            {
                "tech_stack": "; ".join(a.tech_stack.all_technologies),
                "current_projects": " | ".join(a.current_projects),
                "competitors_mentioned": "; ".join(a.competitive_landscape.competitors_mentioned),
                "market_positioning": a.competitive_landscape.market_positioning or "",
                "positioning_signals": "; ".join(a.competitive_landscape.positioning_signals),
                "company_size": a.enrichment.company_size or "",
                "funding_stage": a.enrichment.funding_stage or "",
                "github_public_repos": a.enrichment.github_public_repos or "",
                "github_top_projects": "; ".join(a.enrichment.github_top_projects),
                "enrichment_sources": "; ".join(a.enrichment.sources),
            }
        )

    for key in custom_keys:
        val = company.custom_fields.get(key)
        row[f"custom_{key}"] = "; ".join(val) if isinstance(val, list) else (val or "")

    row["errors_count"] = len(company.errors)
    row["scraped_at"] = company.scraped_at
    return row


def export_json(summary: ScrapeRunSummary, path: str | Path) -> Path:
    """Export complete nested run summary and company profiles to formatted JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = summary.model_dump(mode="json", exclude_none=False)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def export_csv(companies: list[CompanyProfile], path: str | Path) -> Path:
    """Export flattened company profiles to UTF-8 CSV."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    custom_keys: list[str] = []
    for comp in companies:
        for k in comp.custom_fields:
            if k not in custom_keys:
                custom_keys.append(k)

    rows = [_flatten_company_row(comp, custom_keys) for comp in companies]
    if not rows:
        out.write_text("company_name,website_url,domain,emails,phone_numbers\n", encoding="utf-8")
        return out

    # Preserve union of headers while keeping consistent ordering
    fieldnames: list[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)

    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return out


def export_markdown(summary: ScrapeRunSummary, path: str | Path) -> Path:
    """Export a readable Markdown intelligence dossier for the scraped companies."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f"# Company Intelligence Report (`{summary.job_id}`)",
        "",
        f"- **Extraction Level:** `{summary.level.value}`",
        f"- **Query:** `{summary.query or 'N/A'}`",
        f"- **Targets:** {summary.total_targets} (Succeeded: {summary.succeeded}, Failed: {summary.failed})",
        f"- **Duration:** {summary.duration_seconds:.2f}s",
        "",
        "## Summary Table",
        "",
        "| Company | Website | Industry | Emails | Tech Stack |",
        "|---|---|---|---|---|",
    ]

    for c in summary.companies:
        industry = c.medium.industry if c.medium and c.medium.industry else "—"
        emails = ", ".join(c.contact.emails[:2]) if c.contact.emails else "—"
        tech = (
            ", ".join(c.advanced.tech_stack.all_technologies[:5])
            if c.advanced and c.advanced.tech_stack.all_technologies
            else "—"
        )
        lines.append(f"| {c.company_name} | {c.website_url} | {industry} | {emails} | {tech} |")

    lines.append("")
    for c in summary.companies:
        lines.extend([f"## {c.company_name}", f"- **Website:** {c.website_url}"])
        if c.contact.emails:
            lines.append(f"- **Emails:** {', '.join(c.contact.emails)}")
        if c.contact.phone_numbers:
            lines.append(f"- **Phones:** {', '.join(c.contact.phone_numbers)}")
        if c.medium:
            if c.medium.tagline:
                lines.append(f"- **Tagline:** {c.medium.tagline}")
            if c.medium.description:
                lines.append(f"- **Description:** {c.medium.description}")
            if c.medium.location or c.medium.address:
                lines.append(f"- **Location:** {c.medium.address or c.medium.location}")
            if c.medium.year_founded:
                lines.append(f"- **Founded:** {c.medium.year_founded}")
            if c.medium.products_services:
                lines.append(f"- **Products / Services:** {', '.join(c.medium.products_services[:6])}")
        if c.advanced:
            if c.advanced.tech_stack.all_technologies:
                lines.append(
                    f"- **Tech Stack:** {', '.join(c.advanced.tech_stack.all_technologies)}"
                )
            if c.advanced.current_projects:
                lines.append(
                    f"- **Current Projects / Focus:** {'; '.join(c.advanced.current_projects[:4])}"
                )
            if c.advanced.competitive_landscape.competitors_mentioned:
                lines.append(
                    f"- **Competitors Mentioned:** {', '.join(c.advanced.competitive_landscape.competitors_mentioned)}"
                )
            if c.advanced.competitive_landscape.market_positioning:
                lines.append(
                    f"- **Positioning:** {c.advanced.competitive_landscape.market_positioning}"
                )
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def export_results(
    summary: ScrapeRunSummary,
    output_dir: str | Path,
    formats: list[str],
    prefix: str | None = None,
) -> list[str]:
    """Export results in all requested formats and return written file paths."""
    base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or f"scrape_{summary.job_id}"
    written: list[str] = []

    if "json" in formats:
        written.append(str(export_json(summary, base_dir / f"{stem}.json")))
    if "csv" in formats:
        written.append(str(export_csv(summary.companies, base_dir / f"{stem}.csv")))
    if "markdown" in formats:
        written.append(str(export_markdown(summary, base_dir / f"{stem}.md")))

    return written
