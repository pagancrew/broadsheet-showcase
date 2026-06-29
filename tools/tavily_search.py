"""
tools/tavily_search.py

Runs Tavily search queries and returns story-shaped results.
Tavily returns pre-parsed, LLM-ready web content — ideal for
finding fresh stories that RSS feeds might miss.

Free tier: 1000 credits/month (roughly 10 searches = 1 credit = ~10,000 searches/month).
At ~8 queries/run × 30 runs/month ≈ 240 searches ≈ 24 credits. Well within the limit.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from tools.source_monitor import record_failure, record_success

logger = logging.getLogger(__name__)

# Regex patterns for date extraction from URLs and content text.
# URL pattern: /YYYY/MM/DD/ or /YYYY-MM-DD or similar embedded date segments.
_URL_DATE_RE = re.compile(r"/(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[/\-_]|$)")
# Content text pattern: catches "March 15, 2024", "15 Mar 2024", "2024-03-15", etc.
_CONTENT_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{4})\b"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"
    r"|\b(\d{4})[/-](\d{2})[/-](\d{2})\b",
    re.IGNORECASE,
)


def _extract_published(r: dict) -> Optional[str]:
    """
    Extract a publication date from a Tavily result dict.

    Tries four tiers in order, returning an ISO 8601 UTC string or None:
      1. Tavily's own published_date field (ISO parse with tolerance).
      2. dateutil.parser.parse() on the published_date string.
      3. Date pattern embedded in the article URL.
      4. Date pattern in the first 500 chars of content text.
    """
    # Tier 1 & 2 — from published_date field
    raw = r.get("published_date")
    if raw:
        # Tier 1: strict ISO (handles "2024-03-15T10:00:00Z", "+00:00" etc.)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except (ValueError, TypeError):
            pass
        # Tier 2: tolerant dateutil parse
        try:
            from dateutil import parser as _du
            dt = _du.parse(str(raw), dayfirst=False)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass

    # Tier 3: date embedded in URL
    url = r.get("url", "")
    if url:
        m = _URL_DATE_RE.search(url)
        if m:
            try:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
                dt = datetime(year, month, day, tzinfo=timezone.utc)
                return dt.isoformat()
            except (ValueError, TypeError):
                pass

    # Tier 4: date pattern in content text
    content = r.get("content", "") or ""
    if content:
        m = _CONTENT_DATE_RE.search(content[:500])
        if m:
            try:
                from dateutil import parser as _du
                dt = _du.parse(m.group(), dayfirst=False)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                pass

    return None

# Set to True if any search call hits the monthly credit limit.
# main.py reads this after the gather phase and includes it in the run report.
quota_exceeded = False


def search(query: str, category: str, max_results: int = 5, include_domains: list[str] | None = None, days: int | None = None) -> list[dict]:
    """
    Run a Tavily search and return story dicts.

    Args:
        query: Search query string
        category: Category to tag results with
        max_results: How many results to return (1-10)

    Returns:
        List of story dicts
    """
    try:
        from tavily import TavilyClient
    except ImportError:
        logger.error("tavily-python not installed. Run: pip install tavily-python")
        return []

    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY not set — skipping Tavily search")
        return []

    global quota_exceeded
    source_name = f"Tavily:{query[:40]}"
    try:
        client = TavilyClient(api_key=api_key)
        kwargs = dict(
            query=query,
            search_depth="basic",
            topic="news",
            max_results=max_results,
            include_answer=False,
            include_raw_content=False,
        )
        if include_domains:
            kwargs["include_domains"] = include_domains
        if days is not None:
            kwargs["days"] = days
        results = client.search(**kwargs)

        record_success(source_name)
    except Exception as e:
        err_str = str(e).lower()
        if any(kw in err_str for kw in ("quota", "limit", "credit", "402", "429", "exceeded")):
            quota_exceeded = True
            logger.warning(f"[TAVILY-QUOTA] Tavily credit limit reached — search skipped: {e}")
        else:
            record_failure(source_name, str(e))
            logger.warning(f"Tavily search failed for '{query}': {e}")
        return []

    from urllib.parse import urlparse
    stories = []
    for r in results.get("results", []):
        url = r.get("url", "")
        # Drop domain roots — no path beyond "/" is not a specific article
        if url:
            path = urlparse(url).path.rstrip("/")
            if not path:
                continue
        story = {
            "title": r.get("title", "").strip(),
            "url": url,
            "source": url.split("/")[2] if url else "Tavily",
            "published_iso": _extract_published(r),
            "description": r.get("content", "")[:500],
            "category": category,
        }
        if story["title"] and story["url"]:
            stories.append(story)

    logger.info(f"Tavily '{query}': {len(stories)} results")
    return stories


def get_usage() -> dict | None:
    """
    Fetch monthly credit usage from Tavily's /usage endpoint.

    Returns {"used": int, "limit": int, "pct": float} or None if the call
    fails, the key is missing, or limit is null (unlimited plans).
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return None
    try:
        import requests
        r = requests.get(
            "https://api.tavily.com/usage",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        used = data.get("account", {}).get("plan_usage")
        limit = data.get("account", {}).get("plan_limit")
        if used is None or not limit:
            return None
        return {"used": used, "limit": limit, "pct": round(used / limit * 100, 1)}
    except Exception as e:
        logger.warning(f"Tavily /usage fetch failed (non-fatal): {e}")
        return None


def run_queries(queries: list[str], category: str, max_results_per_query: int = 5, days: int | None = None) -> list[dict]:
    """Run multiple Tavily queries and combine results."""
    all_stories = []
    for q in queries:
        all_stories.extend(search(q, category, max_results_per_query, days=days))
    return all_stories

def search_site(domain: str, query: str, category: str, max_results: int = 5, days: int | None = None) -> list[dict]:
    """Search a specific domain via Tavily include_domains filter."""
    return search(query, category, max_results=max_results, include_domains=[domain], days=days)