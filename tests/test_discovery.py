"""Tests for discovery, pagination, internal subpage crawling, and SPA detection."""

import httpx
import pytest

from scraper.discovery import (
    discover_companies_from_query,
    discover_company_subpages,
    discover_paginated_urls,
    find_next_pagination_url,
)
from scraper.fetcher import PageFetcher, needs_dynamic_render
from scraper.models import RenderMode, ScraperConfig


def test_spa_detection_heuristics():
    spa = '<html><body><div id="root"></div><script src="/bundle.js"></script></body></html>'
    assert needs_dynamic_render(spa, 200) is True
    assert needs_dynamic_render("<html><body>" + "word " * 100 + "</body></html>", 200) is False
    assert needs_dynamic_render("<html><body>Forbidden</body></html>", 403) is True


def test_pagination_next_link_detection():
    html = """
    <html><body>
      <div class="posts"><article>Post 1</article></div>
      <nav class="pagination"><a href="/blog?page=2" rel="next">Next</a></nav>
    </body></html>
    """
    assert find_next_pagination_url(html, "https://acme.dev/blog") == "https://acme.dev/blog?page=2"


@pytest.mark.asyncio
async def test_subpage_and_pagination_discovery():
    pages = {
        "https://acme.dev/": """
            <html><body>
              <nav><a href="/about">About</a><a href="/blog">Blog</a><a href="/contact">Contact</a></nav>
              <a href="https://external.org/ignore">External</a>
            </body></html>
        """,
        "https://acme.dev/sitemap.xml": """
            <urlset>
              <url><loc>https://acme.dev/products</loc></url>
              <url><loc>https://acme.dev/careers</loc></url>
            </urlset>
        """,
        "https://acme.dev/blog": """
            <html><body><h2>Release v1</h2><a href="/blog?page=2">Next</a></body></html>
        """,
        "https://acme.dev/blog?page=2": """
            <html><body><h2>Release v2</h2></body></html>
        """,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            ct = "application/xml" if url.endswith(".xml") else "text/html"
            return httpx.Response(200, text=pages[url], headers={"content-type": ct})
        return httpx.Response(404)

    cfg = ScraperConfig(
        seed_urls=["https://acme.dev"],
        render_mode=RenderMode.STATIC,
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
        respect_robots=False,
    )
    async with PageFetcher(cfg, transport=httpx.MockTransport(handler)) as fetcher:
        subpages = await discover_company_subpages(
            fetcher=fetcher,
            homepage_url="https://acme.dev/",
            homepage_html=pages["https://acme.dev/"],
            max_subpages=4,
            follow_sitemap=True,
        )
        assert "https://acme.dev/about" in subpages
        assert "https://acme.dev/contact" in subpages
        assert all("external.org" not in u for u in subpages)

        paginated = await discover_paginated_urls(
            fetcher=fetcher,
            start_url="https://acme.dev/blog",
            first_html=pages["https://acme.dev/blog"],
            max_pages=3,
        )
        assert len(paginated) == 1
        assert paginated[0][0] == "https://acme.dev/blog?page=2"


@pytest.mark.asyncio
async def test_query_discovery_mines_listicles_when_needed(monkeypatch):
    def fake_ddgs(query: str, max_results: int):
        return [
            "https://tech-blog.example.org/top-cloud-startups-europe",
            "https://directstartup.eu/",
        ]

    monkeypatch.setattr("scraper.discovery._run_ddgs_search", fake_ddgs)

    listicle_html = """
    <html><body>
      <a href="https://alpinecloud.ch/">AlpineCloud</a>
      <a href="https://nordiccompute.se/">NordicCompute</a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "tech-blog.example.org" in str(request.url):
            return httpx.Response(200, text=listicle_html, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    cfg = ScraperConfig(
        query="cloud startups",
        render_mode=RenderMode.STATIC,
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
        respect_robots=False,
    )
    async with PageFetcher(cfg, transport=httpx.MockTransport(handler)) as fetcher:
        found = await discover_companies_from_query("cloud startups", max_companies=3, fetcher=fetcher)
        assert found == [
            "https://directstartup.eu/",
            "https://alpinecloud.ch/",
            "https://nordiccompute.se/",
        ]
