"""
tools/rss_fetcher.py

Fetches and parses RSS/Atom feeds. Returns a list of story dicts.
Records successes/failures via source_monitor.
"""

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import requests

from tools.source_monitor import record_success, record_failure

logger = logging.getLogger(__name__)

# Stories older than this are ignored (hours)
DEFAULT_MAX_AGE_HOURS = 48

_INGEST_FILTER_LOG = Path(__file__).parent.parent / "logs" / "ingest_filter.log"

# Keywords for the AI-relevance filter (ai_only_filter: true sources).
# Word-boundary matched against title + description to avoid false positives.
_AI_KEYWORDS = [
    "ai", "artificial intelligence", "llm", "large language model", "gpt", "claude",
    "gemini", "anthropic", "openai", "deepmind", "mistral", "meta ai",
    "machine learning", "neural network", "deep learning", "transformer",
    "alignment", "agi", "agent", "fine-tune", "training data",
    "chatbot", "diffusion", "generative ai", "rag", "embedding",
]
_AI_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _AI_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_ai_relevant(story: dict) -> bool:
    """Return True if the story title or description contains an AI keyword."""
    text = (story.get("title", "") + " " + story.get("description", "")).lower()
    return bool(_AI_KEYWORD_PATTERN.search(text))


def _log_ingest_drop(name: str, title: str) -> None:
    _INGEST_FILTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt
    with open(_INGEST_FILTER_LOG, "a") as f:
        f.write(f"[{_dt.now().isoformat(timespec='seconds')}] DROPPED [{name}] {title!r}\n")


def fetch_feed(
    name: str,
    url: str,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    ai_only_filter: bool = False,
) -> list[dict]:
    """
    Fetch a single RSS/Atom feed and return recent stories.

    Args:
        name: Human-readable source name (used in logs and monitor)
        url: Feed URL
        max_age_hours: Only return stories newer than this

    Returns:
        List of story dicts with keys:
            title, url, source, published_iso, description, category (blank — set by caller)
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Broadsheet/1.0; +https://github.com/pagancrew/broadsheet)"}
    delays = [1, 3, 7]
    last_exc: Exception = Exception("no attempts made")
    for attempt, delay in enumerate(delays, 1):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            break
        except Exception as e:
            last_exc = e
            if attempt < len(delays):
                logger.debug(f"RSS '{name}' attempt {attempt} failed ({e}), retrying in {delay}s")
                time.sleep(delay)
    else:
        record_failure(name, str(last_exc))
        logger.warning(f"Failed to fetch RSS feed '{name}' after {len(delays)} attempts: {last_exc}")
        return []

    if feed.bozo and not feed.entries:
        record_failure(name, f"Feed parse error: {feed.bozo_exception}")
        return []

    record_success(name)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    stories = []

    for entry in feed.entries:
        published = _parse_date(entry)
        if published and published < cutoff:
            continue

        story = {
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "source": name,
            "published_iso": published.isoformat() if published else None,
            "description": _extract_description(entry),
            "category": "",  # set by caller
        }

        if story["title"] and story["url"]:
            if ai_only_filter and not _is_ai_relevant(story):
                _log_ingest_drop(name, story["title"])
                continue
            stories.append(story)

    logger.info(f"RSS '{name}': {len(stories)} stories (last {max_age_hours}h)")
    return stories


def fetch_multiple_feeds(
    feeds: list[dict],
    category: str,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> list[dict]:
    """
    Fetch multiple feeds from a config list.

    Args:
        feeds: List of dicts with 'name', 'url', 'enabled' keys
        category: Category string to tag each story
        max_age_hours: Age cutoff

    Returns:
        Combined list of story dicts
    """
    all_stories = []
    for feed_cfg in feeds:
        if not feed_cfg.get("enabled", True):
            # Clear any stale failure state so disabled sources don't keep alerting
            record_success(feed_cfg["name"])
            continue
        stories = fetch_feed(
            feed_cfg["name"],
            feed_cfg["url"],
            max_age_hours,
            ai_only_filter=feed_cfg.get("ai_only_filter", False),
        )
        for s in stories:
            s["category"] = category
            if "byline" in feed_cfg:
                s["byline"] = feed_cfg["byline"]
        all_stories.extend(stories)
    return all_stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(entry) -> Optional[datetime]:
    """Try multiple feedparser date fields, return a timezone-aware datetime."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _extract_description(entry) -> str:
    """Pull the best available description from a feed entry."""
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list):
                val = val[0].get("value", "")
            # Strip basic HTML tags
            import re
            val = re.sub(r"<[^>]+>", " ", str(val))
            val = " ".join(val.split())  # normalise whitespace
            return val[:500]  # truncate for LLM context efficiency
    return ""
