"""
tools/github_trending.py

Scrapes GitHub Trending page for today's trending AI/ML repositories.
No official API exists — this scrapes github.com/trending with BeautifulSoup.

GitHub does not prohibit scraping the trending page for personal use.
If this breaks (GitHub updates HTML structure), check BeautifulSoup selectors.
"""

import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from tools.source_monitor import record_failure, record_success

logger = logging.getLogger(__name__)

SOURCE_NAME = "GitHub Trending"
TRENDING_URL = "https://github.com/trending"


def fetch_trending(
    language: str = "",
    since: str = "daily",
    max_repos: int = 10,
    category: str = "builder",
) -> list[dict]:
    """
    Fetch trending GitHub repositories.

    Args:
        language: Filter by programming language (e.g. "python"), or "" for all
        since: "daily", "weekly", or "monthly"
        max_repos: Max repos to return
        category: Category tag for returned stories

    Returns:
        List of story dicts
    """
    params = {"since": since}
    if language:
        params["l"] = language

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Broadsheet/1.0 personal news digest)"
    }

    try:
        resp = requests.get(TRENDING_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        record_success(SOURCE_NAME)
    except Exception as e:
        record_failure(SOURCE_NAME, str(e))
        logger.warning(f"GitHub Trending fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    repo_articles = soup.select("article.Box-row")

    stories = []
    for article in repo_articles[:max_repos]:
        try:
            # Repo name and URL
            h2 = article.select_one("h2 a")
            if not h2:
                continue
            repo_path = h2.get("href", "").lstrip("/")
            repo_url = f"https://github.com/{repo_path}"
            repo_name = repo_path.replace("/", " / ")

            # Description
            p = article.select_one("p")
            description = p.get_text(strip=True) if p else ""

            # Stars
            stars_el = article.select_one("a[href$='/stargazers']")
            stars = stars_el.get_text(strip=True).replace(",", "") if stars_el else "?"

            # Stars today
            stars_today_el = article.select_one("span.d-inline-block.float-sm-right")
            stars_today = stars_today_el.get_text(strip=True) if stars_today_el else ""

            # Language
            lang_el = article.select_one("span[itemprop='programmingLanguage']")
            lang = lang_el.get_text(strip=True) if lang_el else ""

            story = {
                "title": repo_name,
                "url": repo_url,
                "source": SOURCE_NAME,
                "published_iso": datetime.now(timezone.utc).isoformat(),
                "description": (
                    f"{description} "
                    f"{'Language: ' + lang + '. ' if lang else ''}"
                    f"Stars: {stars}. {stars_today}"
                ).strip(),
                "stars": stars,
                "category": category,
            }
            stories.append(story)
        except Exception as e:
            logger.debug(f"Error parsing trending repo: {e}")
            continue

    logger.info(f"GitHub Trending: {len(stories)} repos (since={since})")
    return stories
