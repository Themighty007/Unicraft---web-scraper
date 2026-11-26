"""Configuration loader (YAML/JSON + CLI overrides) and shared extraction catalogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from scraper.models import CustomRule, ScraperConfig


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load scraper settings from a YAML or JSON configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    raw_text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        data = json.loads(raw_text)
    else:
        data = yaml.safe_load(raw_text)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file must contain a mapping/object: {config_path}")
    return data


def build_config(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> ScraperConfig:
    """Merge file configuration with CLI or API overrides into a validated ScraperConfig."""
    merged: dict[str, Any] = {}
    if config_path:
        merged.update(load_config_file(config_path))

    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "custom_rules" and isinstance(value, list):
                existing = merged.get("custom_rules", [])
                merged["custom_rules"] = [*existing, *value]
            elif key == "seed_urls" and isinstance(value, list) and not value:
                continue
            elif key == "proxies" and isinstance(value, list) and not value:
                continue
            else:
                merged[key] = value

    return ScraperConfig(**merged)


def parse_inline_rule(spec: str, rule_type: str) -> CustomRule:
    """Parse a CLI inline rule spec formatted as `field=pattern[::attr(href)][::all]`."""
    if "=" not in spec:
        raise ValueError(
            f"Invalid {rule_type} rule '{spec}'. Expected format: field_name=pattern"
        )
    name, rest = spec.split("=", 1)
    name = name.strip()
    if not name or not rest.strip():
        raise ValueError(f"Rule name and pattern cannot be empty in '{spec}'")

    attribute: str | None = None
    multiple = False
    pattern = rest.strip()
    if pattern.endswith("::all"):
        multiple = True
        pattern = pattern[:-5].strip()
    if rule_type in ("css", "xpath") and "::attr(" in pattern and pattern.endswith(")"):
        pattern_part, attr_part = pattern.rsplit("::attr(", 1)
        pattern = pattern_part.strip()
        attribute = attr_part[:-1].strip()

    return CustomRule(
        name=name,
        type=rule_type,  # type: ignore[arg-type]
        pattern=pattern,
        attribute=attribute,
        multiple=multiple,
    )


USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
)

# Categorized technology fingerprints: (tech_name, category, html/header substrings, text_keywords)
TECH_SIGNATURES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    # Frontend Frameworks & UI
    ("React", "Frontend", ("_next/static", "react-dom", "data-reactroot", "__REACT_DEVTOOLS_GLOBAL_HOOK__", "react.production"), ("react", "reactjs")),
    ("Next.js", "Frontend", ("_next/static", "__NEXT_DATA__", "next/dist", "x-nextjs-cache"), ("next.js", "nextjs")),
    ("Vue.js", "Frontend", ("__vue__", "vue.runtime", "data-v-", "nuxt"), ("vue.js", "vuejs")),
    ("Nuxt.js", "Frontend", ("/_nuxt/", "__NUXT__", "nuxt.config"), ("nuxt", "nuxt.js")),
    ("Angular", "Frontend", ("ng-version", "ng-app", "angular.min.js", "zone.js"), ("angular",)),
    ("Svelte / SvelteKit", "Frontend", ("svelte-", "__sveltekit", "sveltekit"), ("svelte", "sveltekit")),
    ("Astro", "Frontend", ("astro-island", "_astro/", 'content="Astro'), ("astro",)),
    ("Tailwind CSS", "Frontend", ("tailwindcss", "tw-", "--tw-ring-"), ("tailwind css", "tailwindcss")),
    ("Bootstrap", "Frontend", ("bootstrap.min.css", "bootstrap.bundle", "data-bs-"), ("bootstrap",)),
    ("TypeScript", "Frontend", (), ("typescript",)),
    ("WebAssembly", "Frontend", (".wasm",), ("webassembly", "wasm")),
    # Backend & Languages
    ("Node.js", "Backend & Languages", ("x-powered-by: express",), ("node.js", "nodejs")),
    ("Python", "Backend & Languages", ("django", "fastapi", "uvicorn", "gunicorn", "wsgi"), ("python", "fastapi", "django")),
    ("Go", "Backend & Languages", (), ("golang", "go microservices")),
    ("Rust", "Backend & Languages", (), ("rust", "tokio")),
    ("Java / Spring", "Backend & Languages", ("jsessionid",), ("spring boot", "java")),
    ("Ruby on Rails", "Backend & Languages", ("csrf-param", "x-runtime"), ("ruby on rails",)),
    ("PHP / Laravel", "Backend & Languages", ("phpsessid", "laravel_session", "x-powered-by: php"), ("laravel", "php")),
    ("GraphQL", "Backend & Languages", ("/graphql", "apollo-client", "__apollo_state__"), ("graphql",)),
    ("gRPC", "Backend & Languages", (), ("grpc",)),
    # Cloud, CDN & Infrastructure
    ("Cloudflare", "Cloud & CDN", ("cf-ray", "cloudflare", "challenges.cloudflare.com", "__cf_bm"), ("cloudflare",)),
    ("AWS", "Cloud & CDN", ("amazonaws.com", "cloudfront.net", "x-amz-", "aws-waf"), ("aws", "amazon web services")),
    ("Google Cloud", "Cloud & CDN", ("googleapis.com", "gstatic.com", "storage.googleapis.com", "x-goog-"), ("google cloud", "gcp")),
    ("Microsoft Azure", "Cloud & CDN", ("azureedge.net", "azurewebsites.net", "blob.core.windows.net"), ("azure",)),
    ("Vercel", "Cloud & CDN", ("x-vercel-id", "vercel.app", "vercel-insights"), ("vercel",)),
    ("Netlify", "Cloud & CDN", ("x-nf-request-id", "netlify.app", "netlify-identity"), ("netlify",)),
    ("Fastly", "Cloud & CDN", ("x-fastly-request-id", "fastly.net"), ("fastly",)),
    ("Kubernetes", "DevOps & Data", (), ("kubernetes", "k8s")),
    ("Docker", "DevOps & Data", (), ("docker",)),
    ("Terraform", "DevOps & Data", (), ("terraform",)),
    ("PostgreSQL", "DevOps & Data", (), ("postgresql", "postgres")),
    ("Redis", "DevOps & Data", (), ("redis",)),
    ("Kafka", "DevOps & Data", (), ("kafka", "apache kafka")),
    ("Snowflake", "DevOps & Data", (), ("snowflake",)),
    # CMS & E-commerce
    ("WordPress", "CMS & Ecommerce", ("wp-content/", "wp-includes/", 'content="WordPress'), ()),
    ("Webflow", "CMS & Ecommerce", ("website-files.com", "webflow.js", 'content="Webflow'), ("webflow",)),
    ("Shopify", "CMS & Ecommerce", ("cdn.shopify.com", "shopify-section", "x-shopify"), ("shopify",)),
    ("Contentful", "CMS & Ecommerce", ("ctfassets.net", "contentful"), ("contentful",)),
    ("Sanity", "CMS & Ecommerce", ("cdn.sanity.io",), ("sanity.io",)),
    ("Ghost", "CMS & Ecommerce", ('content="Ghost', "ghost.org"), ()),
    ("Framer", "CMS & Ecommerce", ("framerusercontent.com", "framer.com/m"), ()),
    # Analytics, Marketing & Support
    ("Google Analytics", "Analytics & Growth", ("googletagmanager.com", "google-analytics.com", "gtag("), ()),
    ("PostHog", "Analytics & Growth", ("posthog.com", "app.posthog", "us.i.posthog"), ("posthog",)),
    ("Plausible Analytics", "Analytics & Growth", ("plausible.io/js",), ()),
    ("Segment", "Analytics & Growth", ("cdn.segment.com", "analytics.load("), ("segment",)),
    ("Mixpanel", "Analytics & Growth", ("cdn.mxpnl.com", "mixpanel"), ("mixpanel",)),
    ("HubSpot", "Analytics & Growth", ("js.hs-scripts.com", "hsforms.net", "hubspot"), ("hubspot",)),
    ("Intercom", "Analytics & Growth", ("widget.intercom.io", "intercomSettings"), ("intercom",)),
    ("Zendesk", "Analytics & Growth", ("static.zdassets.com", "zendesk"), ()),
    ("Stripe", "Payments & Auth", ("js.stripe.com", "checkout.stripe.com"), ("stripe",)),
    ("Auth0", "Payments & Auth", ("auth0.com", "cdn.auth0.com"), ("auth0",)),
    ("Sentry", "Observability & Security", ("browser.sentry-cdn.com", "sentry.io"), ("sentry",)),
    ("Datadog", "Observability & Security", ("datadoghq-browser-agent.com", "dd-rum"), ("datadog",)),
    ("reCAPTCHA", "Observability & Security", ("google.com/recaptcha", "grecaptcha"), ()),
    ("hCaptcha", "Observability & Security", ("hcaptcha.com/1/api.js",), ()),
)

INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Cloud Infrastructure & DevOps": (
        "cloud computing", "cloud infrastructure", "kubernetes", "serverless",
        "edge network", "paas", "iaas", "hosting", "devops", "observability",
        "distributed systems", "object storage", "virtual machine",
    ),
    "Artificial Intelligence & ML": (
        "artificial intelligence", "machine learning", "llm", "generative ai",
        "neural network", "computer vision", "ai agents", "foundation model",
        "nlp", "inference", "deep learning",
    ),
    "Developer Tools & SaaS": (
        "developer tools", "devtools", "api platform", "sdk", "ci/cd",
        "workflow automation", "saas", "software engineering", "low-code",
        "open source", "open-source", "git", "deployment", "postgres",
        "database", "authentication", "realtime", "backend", "developers",
    ),
    "Cybersecurity": (
        "cybersecurity", "zero trust", "threat detection", "vulnerability",
        "encryption", "soc 2", "identity management", "firewall", "siem",
    ),
    "FinTech & Payments": (
        "fintech", "payments", "banking", "payment processing", "treasury",
        "kyc", "ledger", "financial services", "card issuing",
    ),
    "Data & Analytics": (
        "data warehouse", "analytics", "product analytics", "web analytics",
        "session replay", "feature flags", "a/b testing", "business intelligence",
        "etl", "elt", "data pipeline", "streaming data", "lakehouse", "telemetry",
    ),
    "E-Commerce & Retail Tech": (
        "e-commerce", "ecommerce", "online store", "checkout", "fulfillment",
        "merchandising", "retail",
    ),
    "HealthTech & BioTech": (
        "healthcare", "healthtech", "clinical", "telehealth", "biotech",
        "genomics", "patient care", "diagnostics",
    ),
    "CleanTech & Energy": (
        "renewable energy", "clean energy", "carbon", "sustainability",
        "climate tech", "battery", "solar", "grid",
    ),
}
