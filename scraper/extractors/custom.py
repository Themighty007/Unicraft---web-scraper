"""Custom extraction rule engine supporting CSS selectors, XPath expressions, and Regex patterns."""

from __future__ import annotations

import re
from typing import Any

from scraper.extractors import PageBundle, clean_text, dedupe_preserve_order
from scraper.models import CustomRule, PageError


def _extract_css(bundle: PageBundle, rule: CustomRule) -> list[str]:
    matches = bundle.soup.select(rule.pattern)
    values: list[str] = []
    for node in matches:
        if rule.attribute:
            attr_val = node.get(rule.attribute)
            if isinstance(attr_val, list):
                values.append(" ".join(str(x) for x in attr_val))
            elif attr_val is not None:
                values.append(str(attr_val))
        else:
            values.append(clean_text(node.get_text(" ", strip=True)))
    return [v for v in values if v]


def _extract_xpath(bundle: PageBundle, rule: CustomRule) -> list[str]:
    results = bundle.lxml_tree.xpath(rule.pattern)
    values: list[str] = []
    for item in results:
        if isinstance(item, str):
            val = clean_text(item)
        elif hasattr(item, "attrib") and rule.attribute:
            val = clean_text(str(item.attrib.get(rule.attribute, "")))
        elif hasattr(item, "text_content"):
            val = clean_text(item.text_content())
        else:
            val = clean_text(str(item))
        if val:
            values.append(val)
    return values


def _extract_regex(bundle: PageBundle, rule: CustomRule) -> list[str]:
    compiled = re.compile(rule.pattern, re.IGNORECASE | re.MULTILINE)
    matches = compiled.findall(bundle.visible_text)
    values: list[str] = []
    for item in matches:
        if isinstance(item, tuple):
            joined = " ".join(part for part in item if part)
            values.append(clean_text(joined))
        else:
            values.append(clean_text(str(item)))
    return [v for v in values if v]


def apply_custom_rules(
    bundles: list[PageBundle],
    rules: list[CustomRule],
) -> tuple[dict[str, Any], list[PageError]]:
    """Execute user-configured CSS/XPath/Regex rules across all crawled pages of a company."""
    output: dict[str, Any] = {}
    errors: list[PageError] = []

    if not rules or not bundles:
        return output, errors

    for rule in rules:
        collected: list[str] = []
        for bundle in bundles:
            try:
                if rule.type == "css":
                    collected.extend(_extract_css(bundle, rule))
                elif rule.type == "xpath":
                    collected.extend(_extract_xpath(bundle, rule))
                elif rule.type == "regex":
                    collected.extend(_extract_regex(bundle, rule))
            except Exception as exc:
                errors.append(
                    PageError(
                        url=bundle.url,
                        stage=f"custom_rule:{rule.name}",
                        error_type=type(exc).__name__,
                        message=f"Custom {rule.type} rule '{rule.name}' failed: {exc}",
                    )
                )
                break

        deduped = dedupe_preserve_order(collected)
        if rule.multiple:
            output[rule.name] = deduped
        else:
            output[rule.name] = deduped[0] if deduped else None

    return output, errors
