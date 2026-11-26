"""Tests for user-defined CSS selector, XPath, and Regex custom extraction rules."""

from scraper.config import parse_inline_rule
from scraper.extractors import parse_page_bundle
from scraper.extractors.custom import apply_custom_rules
from scraper.models import CustomRule

HTML_DOC = """
<html><body>
  <h1>Fast Global Compute</h1>
  <a class="primary-cta" href="/signup">Start Free Trial</a>
  <div class="badges"><span>SOC 2 Type II</span><span>ISO 27001</span></div>
</body></html>
"""


def test_custom_css_xpath_regex_rules():
    bundle = parse_page_bundle("https://example.io/", HTML_DOC)
    rules = [
        parse_inline_rule("cta_href=a.primary-cta::attr(href)", "css"),
        CustomRule(name="headline", type="xpath", pattern="//h1"),
        CustomRule(
            name="compliance",
            type="regex",
            pattern=r"\b(?:SOC\s*2(?:\s*Type\s*II)?|ISO\s*27001)\b",
            multiple=True,
        ),
    ]
    out, errors = apply_custom_rules([bundle], rules)
    assert errors == []
    assert out["cta_href"] == "/signup"
    assert out["headline"] == "Fast Global Compute"
    assert out["compliance"] == ["SOC 2 Type II", "ISO 27001"]


def test_invalid_custom_rule_captured_gracefully():
    bundle = parse_page_bundle("https://example.io/", HTML_DOC)
    bad_rule = CustomRule(name="broken", type="regex", pattern="[unclosed")
    out, errors = apply_custom_rules([bundle], [bad_rule])
    assert out["broken"] is None
    assert len(errors) == 1
    assert errors[0].stage == "custom_rule:broken"
