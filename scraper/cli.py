"""Rich + Typer command-line interface for running scrapes, validating URLs, and launching the web dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from scraper.config import build_config, parse_inline_rule
from scraper.logging_utils import configure_logging, console
from scraper.models import ExtractionLevel, RenderMode
from scraper.pipeline import run_scrape_pipeline
from scraper.validator import validate_seed_urls

app = typer.Typer(
    name="unicraft",
    help="Multi-level Company Intelligence & Web Scraping Tool.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _render_results_table(summary) -> None:
    table = Table(
        title=f"Extraction Summary — Job {summary.job_id} ({summary.level.value.upper()})",
        show_lines=True,
        header_style="bold cyan",
    )
    table.add_column("Company", style="bold white", no_wrap=True)
    table.add_column("Website", style="cyan")
    table.add_column("Contacts", style="green")
    if summary.level in (ExtractionLevel.MEDIUM, ExtractionLevel.ADVANCED):
        table.add_column("Industry & Location", style="yellow")
    if summary.level == ExtractionLevel.ADVANCED:
        table.add_column("Tech Stack & Insights", style="magenta")
    table.add_column("Pages / Errors", justify="right")

    for company in summary.companies:
        contacts = []
        if company.contact.emails:
            contacts.append(", ".join(company.contact.emails[:2]))
        if company.contact.phone_numbers:
            contacts.append(", ".join(company.contact.phone_numbers[:1]))
        contact_str = "\n".join(contacts) or "—"

        row = [company.company_name, company.website_url, contact_str]

        if summary.level in (ExtractionLevel.MEDIUM, ExtractionLevel.ADVANCED):
            med_parts = []
            if company.medium:
                if company.medium.industry:
                    med_parts.append(company.medium.industry)
                if company.medium.location:
                    med_parts.append(company.medium.location)
                if company.medium.year_founded:
                    med_parts.append(f"Est. {company.medium.year_founded}")
            row.append("\n".join(med_parts) or "—")

        if summary.level == ExtractionLevel.ADVANCED:
            adv_parts = []
            if company.advanced:
                if company.advanced.tech_stack.all_technologies:
                    adv_parts.append(
                        "Tech: " + ", ".join(company.advanced.tech_stack.all_technologies[:5])
                    )
                if company.advanced.competitive_landscape.market_positioning:
                    adv_parts.append(
                        f"Pos: {company.advanced.competitive_landscape.market_positioning}"
                    )
                if company.advanced.competitive_landscape.competitors_mentioned:
                    adv_parts.append(
                        "Vs: "
                        + ", ".join(company.advanced.competitive_landscape.competitors_mentioned[:3])
                    )
            row.append("\n".join(adv_parts) or "—")

        status_str = f"{len(company.pages_crawled)}p / {len(company.errors)}e"
        row.append(status_str)
        table.add_row(*row)

    console.print(table)


@app.command()
def scrape(
    query: Optional[str] = typer.Option(
        None, "--query", "-q", help="Search query to discover companies (e.g. 'cloud startups in Europe')."
    ),
    urls: list[str] = typer.Option(
        [], "--url", "-u", help="Seed company URL(s). Repeat flag or pass comma-separated values."
    ),
    urls_file: Optional[Path] = typer.Option(
        None, "--urls-file", "-f", help="Path to a text file containing one seed URL per line."
    ),
    config_file: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to YAML or JSON configuration file."
    ),
    level: ExtractionLevel = typer.Option(
        ExtractionLevel.ADVANCED, "--level", "-l", help="Extraction depth: basic, medium, or advanced."
    ),
    render: RenderMode = typer.Option(
        RenderMode.AUTO, "--render", "-r", help="Rendering strategy: static, auto (JS fallback), or dynamic."
    ),
    max_companies: int = typer.Option(
        8, "--max-companies", "-n", min=1, max=100, help="Maximum companies to scrape."
    ),
    max_pages: int = typer.Option(
        5, "--max-pages", min=1, max=25, help="Maximum internal pages crawled per company."
    ),
    output_dir: str = typer.Option(
        "outputs", "--output-dir", "-o", help="Directory where output JSON/CSV/Markdown files are saved."
    ),
    formats: list[str] = typer.Option(
        ["json", "csv"], "--format", help="Output format(s): json, csv, markdown."
    ),
    css: list[str] = typer.Option(
        [], "--css", help="Custom CSS rule formatted as `field=selector` or `field=selector::attr(href)`."
    ),
    xpath: list[str] = typer.Option(
        [], "--xpath", help="Custom XPath rule formatted as `field=//h1`."
    ),
    regex: list[str] = typer.Option(
        [], "--regex", help="Custom Regex rule formatted as `field=pattern`."
    ),
    proxy: list[str] = typer.Option(
        [], "--proxy", help="Optional HTTP/SOCKS proxy URL(s)."
    ),
    delay: Optional[float] = typer.Option(
        None, "--delay", help="Minimum polite delay (seconds) between requests to the same domain."
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich", help="Disable external GitHub/Wikidata enrichment calls."
    ),
    no_robots: bool = typer.Option(
        False, "--no-robots", help="Ignore robots.txt directives."
    ),
    skip_reachability: bool = typer.Option(
        False, "--skip-reachability", help="Skip pre-flight HTTP reachability checks on seed URLs."
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="Logging verbosity (DEBUG, INFO, WARNING)."),
) -> None:
    """Discover and extract structured intelligence from company websites."""
    configure_logging(level=log_level)

    seed_urls: list[str] = []
    for raw in urls:
        seed_urls.extend(part.strip() for part in raw.split(",") if part.strip())
    if urls_file:
        if not urls_file.exists():
            console.print(f"[red]Seed URLs file not found:[/red] {urls_file}")
            raise typer.Exit(code=1)
        seed_urls.extend(
            line.strip()
            for line in urls_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    custom_rules = []
    try:
        for spec in css:
            custom_rules.append(parse_inline_rule(spec, "css").model_dump())
        for spec in xpath:
            custom_rules.append(parse_inline_rule(spec, "xpath").model_dump())
        for spec in regex:
            custom_rules.append(parse_inline_rule(spec, "regex").model_dump())
    except ValueError as exc:
        console.print(f"[red]Rule syntax error:[/red] {exc}")
        raise typer.Exit(code=2)

    overrides: dict = {
        "query": query,
        "seed_urls": seed_urls,
        "level": level,
        "render_mode": render,
        "max_companies": max_companies,
        "max_pages_per_company": max_pages,
        "output_dir": output_dir,
        "formats": formats,
        "proxies": proxy,
        "enrich_external": not no_enrich,
        "respect_robots": not no_robots,
        "verify_reachability": not skip_reachability,
        "custom_rules": custom_rules,
    }
    if delay is not None:
        overrides["min_delay_seconds"] = delay
        overrides["max_delay_seconds"] = max(delay, delay + 0.5)

    try:
        cfg = build_config(config_path=config_file, overrides=overrides)
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    if not cfg.query and not cfg.seed_urls:
        console.print("[red]Error:[/red] Provide `--query`, `--url`, `--urls-file`, or a config file with targets.")
        raise typer.Exit(code=2)

    console.print(
        Panel.fit(
            f"[bold cyan]Unicraft Company Intelligence Scraper[/bold cyan]\n"
            f"Level: [bold]{cfg.level.value.upper()}[/bold] | Render: [bold]{cfg.render_mode.value}[/bold] | "
            f"Max Companies: [bold]{cfg.max_companies}[/bold]",
            border_style="cyan",
        )
    )

    async def _run() -> None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Initializing...", total=1)

            def _on_progress(event: dict) -> None:
                ev = event.get("event")
                if ev in ("validating", "discovering"):
                    progress.update(task_id, description=event.get("message", ev))
                elif ev == "targets_ready":
                    total = max(1, int(event.get("total", 1)))
                    progress.update(task_id, total=total, completed=0, description=f"Scraping {total} target(s)...")
                elif ev == "target_completed":
                    progress.update(
                        task_id,
                        completed=int(event.get("processed", 0)),
                        description=f"Extracted {event.get('company')}",
                    )
                elif ev == "completed":
                    progress.update(task_id, description="Export complete")

            summary = await run_scrape_pipeline(cfg, on_progress=_on_progress)

        _render_results_table(summary)
        if summary.errors:
            console.print(f"[yellow]Warnings/Errors logged:[/yellow] {len(summary.errors)} (see log/JSON for details)")
        if summary.output_files:
            console.print("[bold green]Saved outputs:[/bold green]")
            for path in summary.output_files:
                console.print(f"  • {path}")

    asyncio.run(_run())


@app.command()
def validate(
    urls: list[str] = typer.Argument(..., help="One or more URLs to check for format & reachability."),
    timeout: float = typer.Option(10.0, "--timeout", "-t", help="Timeout in seconds."),
) -> None:
    """Validate URL syntax and live HTTP reachability."""
    configure_logging(level="WARNING")
    results = asyncio.run(validate_seed_urls(urls, verify_reachability=True, timeout=timeout))

    table = Table(title="URL Validation & Reachability", header_style="bold cyan")
    table.add_column("Input")
    table.add_column("Normalized URL", style="cyan")
    table.add_column("Format")
    table.add_column("Reachable")
    table.add_column("Status / Error")

    for r in results:
        fmt = "[green]OK[/green]" if r.is_valid_format else "[red]INVALID[/red]"
        reach = (
            "[green]YES[/green]"
            if r.is_reachable
            else ("[red]NO[/red]" if r.is_reachable is False else "—")
        )
        detail = str(r.status_code) if r.status_code else (r.error or "—")
        if r.error and r.status_code:
            detail = f"{r.status_code} ({r.error})"
        table.add_row(r.raw_input, r.normalized_url or "—", fmt, reach, detail)

    console.print(table)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for the web dashboard."),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port for the web dashboard."),
) -> None:
    """Launch the interactive Web Dashboard & Job Scheduler."""
    configure_logging(level="INFO")
    console.print(
        Panel.fit(
            f"[bold green]Unicraft Web Dashboard running at[/bold green] [bold cyan]http://{host}:{port}[/bold cyan]",
            border_style="green",
        )
    )
    uvicorn.run("scraper.web.app:app", host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
