"""Pydantic data models for configuration, extraction levels, jobs, and results."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ExtractionLevel(str, Enum):
    """Supported data extraction depth levels."""

    BASIC = "basic"        # Level 1: Company name, URL, basic contacts
    MEDIUM = "medium"      # Level 2: Socials, address, overview, year, products, industry
    ADVANCED = "advanced"  # Level 3: Tech stack, projects, competitors, positioning, enrichment


class RenderMode(str, Enum):
    """Page rendering strategy."""

    STATIC = "static"    # Fast HTTP client only
    AUTO = "auto"        # HTTP first, Playwright fallback on SPA/JS shells or 403/blocks
    DYNAMIC = "dynamic"  # Always render via Playwright headless browser


class CustomRule(BaseModel):
    """User-defined CSS, XPath, or Regex extraction rule."""

    name: str = Field(..., min_length=1, description="Output field name")
    type: Literal["css", "xpath", "regex"] = Field(..., description="Selector/pattern engine")
    pattern: str = Field(..., min_length=1, description="CSS selector, XPath, or Regex pattern")
    attribute: str | None = Field(
        default=None,
        description="Optional HTML attribute to read for css/xpath rules (defaults to text content)",
    )
    multiple: bool = Field(
        default=False,
        description="Return all matches as a list instead of the first match",
    )


class ScraperConfig(BaseModel):
    """Runtime configuration for a scraping run."""

    query: str | None = Field(default=None, description="Search query to discover companies")
    seed_urls: list[str] = Field(default_factory=list, description="Explicit seed URLs")
    max_companies: int = Field(default=8, ge=1, le=100)
    level: ExtractionLevel = Field(default=ExtractionLevel.ADVANCED)
    render_mode: RenderMode = Field(default=RenderMode.AUTO)

    max_pages_per_company: int = Field(default=5, ge=1, le=25)
    follow_sitemap: bool = Field(default=True)
    follow_pagination: bool = Field(default=True)
    max_pagination_pages: int = Field(default=3, ge=1, le=15)

    timeout_seconds: float = Field(default=15.0, ge=2.0, le=120.0)
    min_delay_seconds: float = Field(default=0.4, ge=0.0, le=30.0)
    max_delay_seconds: float = Field(default=1.2, ge=0.0, le=60.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    concurrency: int = Field(default=3, ge=1, le=16)

    proxies: list[str] = Field(default_factory=list, description="Optional HTTP/SOCKS proxy URLs")
    respect_robots: bool = Field(default=True)
    verify_reachability: bool = Field(default=True)
    enrich_external: bool = Field(default=True)

    custom_rules: list[CustomRule] = Field(default_factory=list)
    output_dir: str = Field(default="outputs")
    formats: list[Literal["json", "csv", "markdown"]] = Field(default=["json", "csv"])

    @field_validator("seed_urls", mode="before")
    @classmethod
    def _clean_seed_urls(cls, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]


class ContactInfo(BaseModel):
    """Level 1 — Basic contact information."""

    emails: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)


class SocialProfiles(BaseModel):
    """Level 2 — Social media links."""

    linkedin: str | None = None
    twitter: str | None = None
    github: str | None = None
    youtube: str | None = None
    instagram: str | None = None
    facebook: str | None = None


class ExtendedCompanyInfo(BaseModel):
    """Level 2 — Enhanced details & lead-generation specifics."""

    social_profiles: SocialProfiles = Field(default_factory=SocialProfiles)
    address: str | None = None
    location: str | None = None
    tagline: str | None = None
    description: str | None = None
    year_founded: int | None = None
    operational_status: str = "Active"
    products_services: list[str] = Field(default_factory=list)
    industry: str | None = None


class TechStackDetail(BaseModel):
    """Level 3 — Categorized technology stack."""

    all_technologies: list[str] = Field(default_factory=list)
    categories: dict[str, list[str]] = Field(default_factory=dict)


class CompetitiveLandscape(BaseModel):
    """Level 3 — Competitors and market positioning."""

    competitors_mentioned: list[str] = Field(default_factory=list)
    market_positioning: str | None = None
    positioning_signals: list[str] = Field(default_factory=list)


class EnrichmentData(BaseModel):
    """Level 3 — Augmented metadata from structured data and public APIs."""

    sources: list[str] = Field(default_factory=list)
    company_size: str | None = None
    funding_stage: str | None = None
    github_org: str | None = None
    github_public_repos: int | None = None
    github_followers: int | None = None
    github_top_languages: list[str] = Field(default_factory=list)
    github_top_projects: list[str] = Field(default_factory=list)
    wikidata_id: str | None = None
    wikidata_description: str | None = None


class AdvancedInsights(BaseModel):
    """Level 3 — Comprehensive technical, operational, and competitive insights."""

    tech_stack: TechStackDetail = Field(default_factory=TechStackDetail)
    current_projects: list[str] = Field(default_factory=list)
    competitive_landscape: CompetitiveLandscape = Field(default_factory=CompetitiveLandscape)
    enrichment: EnrichmentData = Field(default_factory=EnrichmentData)


class PageError(BaseModel):
    """Structured per-page error record."""

    url: str
    stage: str
    error_type: str
    message: str
    status_code: int | None = None


class CompanyProfile(BaseModel):
    """Complete extracted intelligence profile for a single company."""

    company_name: str
    website_url: str
    domain: str
    extraction_level: ExtractionLevel
    scraped_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    pages_crawled: list[str] = Field(default_factory=list)
    render_methods_used: list[str] = Field(default_factory=list)

    # Level 1 (always populated)
    contact: ContactInfo = Field(default_factory=ContactInfo)

    # Level 2 (populated when level in {medium, advanced})
    medium: ExtendedCompanyInfo | None = None

    # Level 3 (populated when level == advanced)
    advanced: AdvancedInsights | None = None

    # User-defined custom selector outputs
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    # Observability
    errors: list[PageError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """Result of validating and checking reachability for an input URL."""

    raw_input: str
    normalized_url: str | None = None
    is_valid_format: bool = False
    is_reachable: bool | None = None
    status_code: int | None = None
    final_url: str | None = None
    error: str | None = None


class ScrapeRunSummary(BaseModel):
    """Summary of a completed or active scraping run."""

    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "completed"
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    query: str | None = None
    seed_urls: list[str] = Field(default_factory=list)
    level: ExtractionLevel = ExtractionLevel.ADVANCED
    total_targets: int = 0
    processed_targets: int = 0
    succeeded: int = 0
    failed: int = 0
    output_files: list[str] = Field(default_factory=list)
    companies: list[CompanyProfile] = Field(default_factory=list)
    errors: list[PageError] = Field(default_factory=list)
