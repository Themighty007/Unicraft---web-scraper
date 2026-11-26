# Unicraft — Multi-Level Company Intelligence Web Scraper

A clean, modular company discovery and web intelligence scraper supporting **search-query discovery**, **seed URL validation**, **Level 1–3 multi-page extraction**, **headless Playwright rendering**, **custom CSS/XPath/Regex rules**, a **Rich CLI**, and a **FastAPI Web Dashboard & Job Scheduler**.

---

## Implemented Features & Extraction Levels

### 1. Data Extraction Levels (All 3 Implemented)
- **Level 1 — Basic (`--level basic`)**:
  - Canonical company name (JSON-LD `Organization` → OpenGraph → `<title>` → copyright → domain stem)
  - Normalized final website URL & domain
  - Contact emails (`mailto:`, visible text, `[at]`/`[dot]` de-obfuscation, asset/template noise filtering) & phone numbers (`tel:`, JSON-LD `telephone`, validated contact patterns)
- **Level 2 — Medium (`--level medium`)**:
  - Extended contacts: Social media profiles (LinkedIn, X/Twitter, GitHub, YouTube, Instagram, Facebook — prioritizing header/footer/JSON-LD brand accounts) and physical address/location (`PostalAddress`, `<address>`, contact/footer parsing)
  - Company overview: Tagline, concise description, founding year, and operational status
  - Lead-generation specifics: Products/services catalog and industry/sector classification
- **Level 3 — Advanced (`--level advanced`, default)**:
  - Categorized tech stack detection across HTTP headers, `<script>`/`<link>`/`<meta>` signatures, and engineering/careers text
  - Current projects & active initiatives from `/blog`, `/news`, `/projects`, `/research`, and release sections
  - Competitive landscape (`competitors_mentioned` from comparison pages/phrases and `market_positioning` classification with evidence signals)
  - Zero-key external enrichment (JSON-LD `numberOfEmployees`, funding stage cues, GitHub Public Org API stats/top repos/languages, and Wikidata entity summaries)

### 2. Core & Optional Capabilities
- **Input Handling & Validation**: Accepts natural-language search queries or seed URLs (`--url`, `--urls-file`, YAML/JSON config, or dashboard); validates URL syntax and live HTTP reachability (`HEAD` with `GET` fallback).
- **Resilient Query Discovery**: Multi-backend search (`ddgs` across Yahoo/Bing/Google/DuckDuckGo + direct Bing HTML fallback) with automatic editorial listicle mining and company-entity resolution.
- **Dynamic JS Rendering**: `auto` render mode fetches via fast `httpx` first and escalates to headless **Playwright** (`domcontentloaded` + `networkidle` wait) when SPA shells (`#root`, `#app`, `__NEXT_DATA__`) or JS challenges are detected.
- **Pagination & Subpage Crawling**: Discovers high-signal internal pages (`/about`, `/contact`, `/products`, `/careers`, `/blog`, `sitemap.xml`) and follows `rel="next"` / pagination links.
- **Custom Rules Engine**: Configure custom **CSS selectors**, **XPath**, and **Regex** rules via YAML/JSON (`config.example.yaml`), CLI flags (`--css`, `--xpath`, `--regex`), or the Web Dashboard.
- **Polite Crawling & Error Isolation**: Per-domain randomized rate delays, `User-Agent` rotation, exponential backoff retries, optional proxy rotation, `robots.txt` awareness, and structured per-page error logging (`logs/scraper.log`).
- **CLI & Web Dashboard**: Rich terminal UI + FastAPI/Uvicorn dashboard with real-time progress, live logs, recurring interval scheduling, and one-click **JSON / CSV / Markdown** downloads.

---

## Quick Setup & Usage

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies & package entrypoint
uv sync

# 2. (Optional) Ensure Playwright browser binaries if Chromium/Chrome/Edge is not installed locally
uv run playwright install chromium
```

### CLI Commands

```bash
# Run Level 3 extraction from seed URLs with custom rules
uv run unicraft scrape \
  --url https://posthog.com --url https://supabase.com --url https://hetzner.com \
  --level advanced --max-pages 5 \
  --css "hero_cta=a[href*='signup'],a[href*='start']" \
  --regex "compliance=\b(?:SOC\s*2|ISO\s*27001|GDPR)\b::all"

# Discover & scrape companies from a search query
uv run unicraft scrape --query "cloud computing startups in Europe" --max-companies 5 --level medium

# Run using a YAML/JSON config file
uv run unicraft scrape --config config.example.yaml

# Validate URL syntax and live HTTP reachability
uv run unicraft validate https://supabase.com https://example.invalid

# Launch the Web Dashboard & Job Scheduler (http://127.0.0.1:8000)
uv run unicraft serve
```

> All commands can also be invoked directly via `uv run python main.py ...`.

---

## Testing

Run the full unit and integration test suite with a single command:

```bash
uv run pytest
```

The test suite uses deterministic `httpx.MockTransport` fixtures and FastAPI `TestClient` to verify URL validation, Level 1–3 extractors, custom CSS/XPath/Regex rules, pagination & subpage discovery, end-to-end pipeline error isolation/exports, and dashboard/scheduler endpoints.

---

## Sample Output

Pre-generated outputs from a live Level 3 run (`posthog.com`, `supabase.com`, `hetzner.com`) are included in [`samples/`](samples/):
- [`samples/sample_output.json`](samples/sample_output.json) — Full multidimensional nested dataset
- [`samples/sample_output.csv`](samples/sample_output.csv) — Flattened spreadsheet-ready export
- [`samples/sample_report.md`](samples/sample_report.md) — Executive Markdown intelligence report

---

## Design Decisions & Assumptions

1. **Hybrid Fetch Strategy (`httpx` + Lazy Playwright)**: Static HTTP covers the majority of company sites cleanly and concurrently, while Playwright only spins up when `render_mode=dynamic` or `auto` detects an empty SPA mount or anti-bot gate, keeping runs fast without sacrificing JS-rendered content.
2. **Single-Pass `PageBundle` Parsing**: Each crawled page is parsed once into BeautifulSoup, `lxml`, `<meta>` maps, and flattened JSON-LD (`@graph`), then shared across Level 1–3 and custom extractors to avoid redundant DOM parsing.
3. **Resilient Zero-Key Discovery & Enrichment**: Search queries avoid brittle single-engine dependencies by cascading across `ddgs` backends + direct HTTP search fallback, and external enrichment uses unauthenticated public GitHub/Wikidata endpoints with short timeouts so offline or rate-limited environments degrade gracefully.
4. **Domain-Scoped Concurrency & Error Isolation**: Semaphores bound cross-company concurrency while per-domain locks enforce polite jittered delays; invalid URLs, HTTP 4xx/5xx responses, or broken custom rules are captured per record without aborting the batch.
