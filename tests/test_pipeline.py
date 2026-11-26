"""End-to-end integration tests for the complete scraping pipeline and exporters."""

import csv
import json
from pathlib import Path

import httpx
import pytest

from scraper.models import CustomRule, ExtractionLevel, RenderMode, ScraperConfig
from scraper.pipeline import run_scrape_pipeline

SITE_PAGES = {
    "https://alpha-cloud.io/": """
        <html>
        <head>
          <title>AlphaCloud — Edge Compute</title>
          <meta name="description" content="Edge cloud computing platform built for low-latency workloads." />
          <script src="https://cdn.example.com/_next/static/app.js"></script>
        </head>
        <body>
          <h1>Deploy Containers Near Every User</h1>
          <p>Founded in 2021. Powered by Kubernetes, Rust, and Cloudflare.</p>
          <a href="/contact">Contact</a>
          <a href="/blog">Blog</a>
          <a href="https://linkedin.com/company/alphacloud">LinkedIn</a>
        </body>
        </html>
    """,
    "https://alpha-cloud.io/contact": """
        <html><body>
          <address>42 King Street, London, UK</address>
          <a href="mailto:team@alpha-cloud.io">team@alpha-cloud.io</a>
          <a href="tel:+442079460192">+44 20 7946 0192</a>
        </body></html>
    """,
    "https://alpha-cloud.io/blog": """
        <html><body>
          <h2>Announcing AlphaMesh Serverless GPU Initiative</h2>
          <p>A lightweight alternative to AWS Lambda with SOC 2 Type II compliance.</p>
        </body></html>
    """,
}


@pytest.mark.asyncio
async def test_full_pipeline_exports_and_error_isolation(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in SITE_PAGES:
            return httpx.Response(200, text=SITE_PAGES[url], headers={"content-type": "text/html"})
        if request.url.host == "broken-site.io":
            return httpx.Response(500, text="Server Error", headers={"content-type": "text/html"})
        return httpx.Response(404)

    cfg = ScraperConfig(
        seed_urls=["https://alpha-cloud.io/", "https://broken-site.io/", "not a valid url"],
        max_companies=5,
        level=ExtractionLevel.ADVANCED,
        render_mode=RenderMode.STATIC,
        max_pages_per_company=4,
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
        max_retries=0,
        respect_robots=False,
        enrich_external=False,
        output_dir=str(tmp_path),
        formats=["json", "csv", "markdown"],
        custom_rules=[
            CustomRule(name="certifications", type="regex", pattern=r"SOC\s*2\s*Type\s*II", multiple=True)
        ],
    )

    summary = await run_scrape_pipeline(
        cfg,
        job_id="testrun",
        transport=httpx.MockTransport(handler),
        save_outputs=True,
    )

    # Invalid and 500 URLs should be recorded in errors cleanly without crashing AlphaCloud
    assert summary.total_targets == 1
    assert summary.succeeded == 1
    assert len(summary.errors) >= 2

    comp = summary.companies[0]
    assert comp.company_name == "AlphaCloud"
    assert "team@alpha-cloud.io" in comp.contact.emails
    assert comp.medium is not None
    assert comp.medium.year_founded == 2021
    assert comp.medium.location == "London, UK"
    assert comp.advanced is not None
    assert "Kubernetes" in comp.advanced.tech_stack.all_technologies
    assert "AWS Lambda" in comp.advanced.competitive_landscape.competitors_mentioned
    assert comp.custom_fields["certifications"] == ["SOC 2 Type II"]

    # Verify JSON, CSV, and Markdown outputs were created and are valid
    json_path = tmp_path / "scrape_testrun.json"
    csv_path = tmp_path / "scrape_testrun.csv"
    md_path = tmp_path / "scrape_testrun.md"
    assert json_path.exists()
    assert csv_path.exists()
    assert md_path.exists()

    parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed_json["companies"][0]["company_name"] == "AlphaCloud"

    with csv_path.open("r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["company_name"] == "AlphaCloud"
    assert "Kubernetes" in rows[0]["tech_stack"]
