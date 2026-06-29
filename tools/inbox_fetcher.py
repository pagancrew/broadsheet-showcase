"""
tools/inbox_fetcher.py

Fetches recent posts from Substack-hosted newsletters via Tavily search.
Substack blocks cloud IPs (GitHub Actions / AWS) with 403 on direct RSS
requests; Tavily's infrastructure is not affected by that block.

Config-driven: reads `tavily_substack.sources` from sources.yaml.
Each source entry must have `domain`, `category`, and optionally `source_name`.
"""

import logging

from tools.source_monitor import record_failure, record_success
from tools.tavily_search import search_site

logger = logging.getLogger(__name__)

_SOURCE_NAME = "Tavily Substack"
_SEARCH_DAYS = 2   # mirror the existing 48h inbox window
_RESULTS_PER_SOURCE = 3

# Map display labels (as they appear in sources.yaml) to internal category keys.
# Display labels are used only at render time (email/Notion); the rest of the
# pipeline always uses internal keys so budget accounting stays correct.
# Brief 21 (2026-06-04): fixes phantom categories escaping the per-category cap.
_DISPLAY_TO_INTERNAL: dict[str, str] = {
    "big news": "big_news",
    "commentary": "critical_voices",
    "critical voices": "critical_voices",
    "built & released": "builder",
    "law & ethics": "laws_ethics",
    "laws & ethics": "laws_ethics",
    "laws ethics": "laws_ethics",
    "builder": "builder",
}


def _normalize_category(label: str) -> str:
    """Return the internal category key for a display label (or raw key).

    Already-internal keys (e.g. "critical_voices") pass through unchanged.
    Unknown labels are returned as-is so a bad config is visible downstream.
    """
    return _DISPLAY_TO_INTERNAL.get(label.lower(), label)


def fetch_substack_via_tavily(
    sources: list[dict],
    default_category: str = "critical_voices",
) -> list[dict]:
    """
    Search Tavily for recent posts from each configured Substack source.

    Args:
        sources: list of dicts with keys: domain (required), category, source_name
        default_category: fallback category for entries without an explicit one
            (should be an internal key; display labels are normalized automatically)

    Returns:
        List of story dicts (title, url, source, published_iso, description, category)
    """
    if not sources:
        logger.info(f"{_SOURCE_NAME}: no sources configured")
        return []

    all_stories: list[dict] = []

    for src in sources:
        domain = src.get("domain", "").strip()
        if not domain:
            logger.warning(f"{_SOURCE_NAME}: skipping entry with no domain: {src}")
            continue

        category = _normalize_category(src.get("category", default_category))
        source_name = src.get("source_name", domain)

        stories = search_site(domain, "latest post", category, max_results=_RESULTS_PER_SOURCE, days=_SEARCH_DAYS)

        priority = src.get("priority", "")
        for story in stories:
            story["source"] = source_name
            if priority:
                story["priority"] = priority

        all_stories.extend(stories)
        logger.info(f"{_SOURCE_NAME} '{source_name}': {len(stories)} results")

    if all_stories:
        record_success(_SOURCE_NAME)
    else:
        record_failure(_SOURCE_NAME, "0 results across all sources")

    return all_stories
