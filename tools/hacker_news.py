"""
tools/hacker_news.py

Fetches Hacker News "Show HN" posts via the official free Firebase API.
No API key required.

Show HN posts are exactly the "look what I built" content: practitioners
sharing projects, tools, demos, and experiments.

API docs: https://github.com/HackerNews/API
"""

import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from tools.source_monitor import record_failure, record_success

logger = logging.getLogger(__name__)

HN_BASE = "https://hacker-news.firebaseio.com/v0"
SOURCE_NAME = "Hacker News (Show HN)"


def fetch_show_hn(
    min_score: int = 20,
    max_items: int = 30,
    time_window_hours: int = 48,
    category: str = "builder",
) -> list[dict]:
    """
    Fetch recent Show HN posts above a score threshold.

    Args:
        min_score: Minimum upvotes to include
        max_items: How many top Show HN IDs to fetch and evaluate
        time_window_hours: Only include posts from the last N hours
        category: Category tag for returned stories

    Returns:
        List of story dicts
    """
    try:
        resp = requests.get(f"{HN_BASE}/showstories.json", timeout=15)
        resp.raise_for_status()
        story_ids = resp.json()[:max_items]
        record_success(SOURCE_NAME)
    except Exception as e:
        record_failure(SOURCE_NAME, str(e))
        logger.warning(f"HN Show HN fetch failed: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=time_window_hours)
    stories = []

    def fetch_item(item_id: int) -> dict | None:
        try:
            r = requests.get(f"{HN_BASE}/item/{item_id}.json", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_item, sid): sid for sid in story_ids}
        for future in as_completed(futures):
            item = future.result()
            if not item:
                continue
            if item.get("score", 0) < min_score:
                continue
            post_time = datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc)
            if post_time < cutoff:
                continue

            hn_url = f"https://news.ycombinator.com/item?id={item['id']}"
            story = {
                "title": item.get("title", "").strip(),
                "url": item.get("url") or hn_url,
                "hn_url": hn_url,
                "source": SOURCE_NAME,
                "published_iso": post_time.isoformat(),
                "description": (
                    f"Show HN post by {item.get('by', 'unknown')}. "
                    f"Score: {item.get('score', 0)} points, "
                    f"{item.get('descendants', 0)} comments."
                ),
                "score": item.get("score", 0),
                "category": category,
            }
            if story["title"]:
                stories.append(story)

    stories.sort(key=lambda x: x.get("score", 0), reverse=True)
    logger.info(f"Show HN: {len(stories)} posts (score ≥{min_score}, last {time_window_hours}h)")
    return stories
