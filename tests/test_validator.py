"""Tests for URL format validation and reachability checking."""

import httpx
import pytest

from scraper.validator import (
    check_url_reachability,
    extract_root_domain,
    normalize_and_validate_url,
    validate_seed_urls,
)


def test_normalize_bare_domain_and_invalid_inputs():
    ok = normalize_and_validate_url("cloudflare.com")
    assert ok.is_valid_format is True
    assert ok.normalized_url == "https://cloudflare.com/"

    empty = normalize_and_validate_url("   ")
    assert empty.is_valid_format is False

    bad_scheme = normalize_and_validate_url("ftp://example.com")
    assert bad_scheme.is_valid_format is False

    bad_host = normalize_and_validate_url("https://not_a_valid_domain")
    assert bad_host.is_valid_format is False

    with_space = normalize_and_validate_url("https://exa mple.com")
    assert with_space.is_valid_format is False


def test_extract_root_domain():
    assert extract_root_domain("https://www.Supabase.com/docs") == "supabase.com"
    assert extract_root_domain("http://localhost:8000/test") == "localhost"


@pytest.mark.asyncio
async def test_reachability_head_fallback_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "head-blocked.example.com":
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200, text="<html>OK</html>")
        if request.url.host == "missing.example.com":
            return httpx.Response(404)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    ok_res = await check_url_reachability("https://head-blocked.example.com", transport=transport)
    assert ok_res.is_reachable is True
    assert ok_res.status_code == 200

    bad_res = await check_url_reachability("https://missing.example.com", transport=transport)
    assert bad_res.is_reachable is False
    assert bad_res.status_code == 404

    batch = await validate_seed_urls(
        ["https://head-blocked.example.com", "invalid url"],
        verify_reachability=True,
        transport=transport,
    )
    assert len(batch) == 2
    assert batch[0].is_reachable is True
    assert batch[1].is_valid_format is False
