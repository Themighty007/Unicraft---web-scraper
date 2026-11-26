"""Tests for Level 1 (Basic), Level 2 (Medium), and Level 3 (Advanced) extractors."""

import pytest

from scraper.extractors import parse_page_bundle
from scraper.extractors.advanced import extract_advanced_info
from scraper.extractors.basic import extract_basic_info
from scraper.extractors.medium import extract_medium_info

SAMPLE_HOME_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>NebulaCloud | Cloud Infrastructure for Europe</title>
  <meta name="description" content="NebulaCloud delivers sovereign Kubernetes and serverless cloud infrastructure across Europe." />
  <meta property="og:site_name" content="NebulaCloud" />
  <script src="https://cdn.example.net/_next/static/chunks/main.js"></script>
  <script src="https://js.stripe.com/v3/"></script>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "NebulaCloud GmbH",
    "foundingDate": "2020-04-01",
    "industry": "Cloud Infrastructure",
    "numberOfEmployees": {"@type": "QuantitativeValue", "value": "51-200"},
    "sameAs": [
      "https://www.linkedin.com/company/nebulacloud/",
      "https://github.com/nebulacloud",
      "https://x.com/nebulacloud"
    ],
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "Torstrasse 102",
      "addressLocality": "Berlin",
      "postalCode": "10119",
      "addressCountry": "Germany"
    }
  }
  </script>
</head>
<body>
  <header>
    <nav>
      <a href="/products/managed-k8s">Managed Kubernetes</a>
      <a href="/solutions/edge-compute">Edge Compute</a>
      <a href="/blog">Blog</a>
    </nav>
  </header>
  <main>
    <h1>Sovereign Cloud Computing Built for Developers</h1>
    <p>An open-source, developer-first platform and modern alternative to AWS and Heroku. Backed by Series B funding.</p>
    <p>Contact us at hello@nebulacloud.eu or security [at] nebulacloud [dot] eu, or call +49 30 1234 5678.</p>
    <p>Ignore placeholder logo@2x.png or test@example.com and dates like 2024-05-12.</p>
  </main>
  <footer>
    <a href="mailto:sales@nebulacloud.eu">Sales</a>
    <a href="tel:+493012345678">Call</a>
    <a href="https://twitter.com/intent/tweet?text=hi">Share</a>
  </footer>
</body>
</html>
"""

SAMPLE_BLOG_HTML = """
<html><body>
  <h2>Introducing Nebula Mesh: Zero-Trust Multi-Region Networking</h2>
  <h3>Launching AI-Native Inference Clusters in Frankfurt</h3>
  <p>Built using Rust, Go, Kubernetes, and PostgreSQL for extreme throughput.</p>
  <a href="/vs-digitalocean">NebulaCloud vs DigitalOcean</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_level_1_2_3_extraction_accuracy():
    home_bundle = parse_page_bundle(
        "https://nebulacloud.eu/",
        SAMPLE_HOME_HTML,
        headers={"cf-ray": "abc123", "x-vercel-id": "fra1"},
    )
    blog_bundle = parse_page_bundle("https://nebulacloud.eu/blog", SAMPLE_BLOG_HTML, headers={})
    bundles = [home_bundle, blog_bundle]

    # Level 1
    name, contact = extract_basic_info(bundles, "nebulacloud.eu")
    assert name == "NebulaCloud GmbH"
    assert "sales@nebulacloud.eu" in contact.emails
    assert "hello@nebulacloud.eu" in contact.emails
    assert "security@nebulacloud.eu" in contact.emails
    assert "test@example.com" not in contact.emails
    assert len(contact.phone_numbers) == 1
    assert "493012345678" in contact.phone_numbers[0].replace(" ", "")

    # Level 2
    med = extract_medium_info(bundles)
    assert med.social_profiles.linkedin == "https://www.linkedin.com/company/nebulacloud"
    assert med.social_profiles.github == "https://github.com/nebulacloud"
    assert med.social_profiles.twitter == "https://x.com/nebulacloud"
    assert med.address == "Torstrasse 102, Berlin, 10119, Germany"
    assert med.location == "Berlin, Germany"
    assert med.year_founded == 2020
    assert med.industry == "Cloud Infrastructure"
    assert "Managed Kubernetes" in med.products_services
    assert "Edge Compute" in med.products_services

    # Level 3
    adv = await extract_advanced_info(
        bundles=bundles,
        company_name=name,
        socials=med.social_profiles,
        enrich_external=False,
    )
    tech = adv.tech_stack.all_technologies
    assert "Next.js" in tech
    assert "Cloudflare" in tech
    assert "Vercel" in tech
    assert "Stripe" in tech
    assert "Kubernetes" in tech
    assert "PostgreSQL" in tech
    assert "Rust" in tech

    assert any("Nebula Mesh" in p for p in adv.current_projects)
    assert "AWS" in adv.competitive_landscape.competitors_mentioned
    assert "DigitalOcean" in adv.competitive_landscape.competitors_mentioned
    assert adv.enrichment.company_size == "51-200"
    assert adv.enrichment.funding_stage == "Series B"
