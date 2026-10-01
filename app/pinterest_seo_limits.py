"""Central user-facing Pinterest Pin SEO text limits.

These limits describe Pinterest Pin title/description specifications. They are
not a substitute for Pinterest API request-schema validation.
"""

from __future__ import annotations

PINTEREST_PIN_TITLE_MAX_LENGTH = 100
PINTEREST_PIN_DESCRIPTION_MAX_LENGTH = 800


def pinterest_seo_limit_violations(
    title: str | None,
    description: str | None,
) -> list[dict[str, str]]:
    """Return stable limit errors, measuring Python Unicode code points.

    Text is expected to be trimmed by the caller before persistence or publish.
    No truncation or content rewriting is performed here.
    """
    violations: list[dict[str, str]] = []
    normalized_title = title.strip() if isinstance(title, str) else None
    normalized_description = description.strip() if isinstance(description, str) else None
    if normalized_title is not None and len(normalized_title) > PINTEREST_PIN_TITLE_MAX_LENGTH:
        violations.append({
            "code": "title_too_long",
            "message": (
                f"Pinterest Pin başlığı {PINTEREST_PIN_TITLE_MAX_LENGTH} karakteri "
                f"aşamaz (mevcut: {len(normalized_title)})."
            ),
        })
    if normalized_description is not None and len(normalized_description) > PINTEREST_PIN_DESCRIPTION_MAX_LENGTH:
        violations.append({
            "code": "description_too_long",
            "message": (
                f"Pinterest Pin açıklaması {PINTEREST_PIN_DESCRIPTION_MAX_LENGTH} karakteri "
                f"aşamaz (mevcut: {len(normalized_description)})."
            ),
        })
    return violations
