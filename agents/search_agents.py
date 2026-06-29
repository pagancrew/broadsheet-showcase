"""
agents/search_agents.py

Four search agents, one per content category.
Each agent gathers raw stories from its assigned sources.
They run sequentially (CrewAI handles orchestration).

Sources per agent:
  BigNewsAgent       — RSS feeds + Tavily
  LawsEthicsAgent   — RSS feeds + Tavily (Guardian via no_feed_sites)
  BuilderAgent      — HN Show HN + Reddit RSS + personal blogs + GitHub Trending + Tavily
  CriticalVoicesAgent — RSS feeds + Tavily
"""

import logging
from typing import Any

import yaml
from crewai import Agent, Task, Crew, LLM

from tools.rss_fetcher import fetch_multiple_feeds
from tools.tavily_search import run_queries, search_site
from tools.source_monitor import record_success, record_zero_yield
from tools.hacker_news import fetch_show_hn
from tools.github_trending import fetch_trending

logger = logging.getLogger(__name__)


def _load_config() -> tuple[dict, dict]:
    """Load agents.yaml and sources.yaml."""
    base = __import__("pathlib").Path(__file__).parent.parent
    with open(base / "config" / "agents.yaml") as f:
        agents_cfg = yaml.safe_load(f)
    with open(base / "config" / "sources.yaml") as f:
        sources_cfg = yaml.safe_load(f)
    return agents_cfg, sources_cfg


def gather_big_news(llm: LLM) -> list[dict]:
    """Gather big news stories from RSS feeds and Tavily."""
    agents_cfg, sources_cfg = _load_config()
    cat_cfg = sources_cfg.get("big_news", {})
    cfg = agents_cfg.get("big_news_agent", {})

    stories = []

    # RSS
    feeds = cat_cfg.get("rss_feeds", [])
    stories.extend(fetch_multiple_feeds(feeds, category="big_news"))

    # Tavily
    queries = cat_cfg.get("tavily_queries", [])
    stories.extend(run_queries(queries, category="big_news", days=3))

    # No-feed sites
    default_queries = cat_cfg.get("no_feed_default_queries", ["news today"])
    for site in cat_cfg.get("no_feed_sites", []):
        if not site.get("enabled", True):
            continue
        site_queries = [site["query"]] if site.get("query") else default_queries
        site_count = 0
        for q in site_queries:
            site_stories = search_site(site["domain"], q, category="big_news", days=2)
            for s in site_stories:
                s["source"] = site["name"]
            stories.extend(site_stories)
            site_count += len(site_stories)
        if site_count > 0:
            record_success(site["name"])
        else:
            record_zero_yield(site["name"])

    logger.info(f"BigNews: {len(stories)} raw stories")
    return _dedupe_urls(stories)

    


def gather_laws_ethics(llm: LLM) -> list[dict]:
    """Gather laws & ethics stories from RSS and Tavily (incl. Guardian via no_feed_sites)."""
    agents_cfg, sources_cfg = _load_config()
    cat_cfg = sources_cfg.get("laws_ethics", {})

    stories = []

    # RSS
    feeds = cat_cfg.get("rss_feeds", [])
    stories.extend(fetch_multiple_feeds(feeds, category="laws_ethics"))

    # Tavily
    queries = cat_cfg.get("tavily_queries", [])
    stories.extend(run_queries(queries, category="laws_ethics", days=3))

    # No-feed sites (includes The Guardian — Brief 21)
    default_queries = cat_cfg.get("no_feed_default_queries", ["news today"])
    for site in cat_cfg.get("no_feed_sites", []):
        if not site.get("enabled", True):
            continue
        site_queries = [site["query"]] if site.get("query") else default_queries
        site_count = 0
        for q in site_queries:
            site_stories = search_site(site["domain"], q, category="laws_ethics", days=2)
            for s in site_stories:
                s["source"] = site["name"]
            stories.extend(site_stories)
            site_count += len(site_stories)
        if site_count > 0:
            record_success(site["name"])
        else:
            record_zero_yield(site["name"])

    logger.info(f"LawsEthics: {len(stories)} raw stories")
    return _dedupe_urls(stories)


def gather_builder(llm: LLM) -> list[dict]:
    """Gather builder/technical stories from HN, Reddit, blogs, GitHub, Tavily."""
    agents_cfg, sources_cfg = _load_config()
    cat_cfg = sources_cfg.get("builder", {})

    stories = []

    # Hacker News Show HN
    hn_cfg = cat_cfg.get("hacker_news", {})
    if hn_cfg.get("enabled", True):
        stories.extend(
            fetch_show_hn(
                min_score=hn_cfg.get("min_score", 20),
                max_items=hn_cfg.get("max_items", 30),
                time_window_hours=hn_cfg.get("time_window_hours", 48),
                category="builder",
            )
        )

    # Reddit RSS
    reddit_cfg = cat_cfg.get("reddit_rss", {})
    if reddit_cfg.get("enabled", True):
        feeds = reddit_cfg.get("feeds", [])
        raw = fetch_multiple_feeds(
            [{"name": f["name"], "url": f["url"], "enabled": f.get("enabled", True)} for f in feeds],
            category="builder",
            max_age_hours=48,
        )
        min_score = reddit_cfg.get("min_score", 50)
        # Reddit feeds don't expose score directly — include all and let agent filter
        stories.extend(raw)

    # Personal blogs and Substacks
    blogs_cfg = cat_cfg.get("personal_blogs", {})
    if blogs_cfg.get("enabled", True):
        feeds = blogs_cfg.get("feeds", [])
        stories.extend(
            fetch_multiple_feeds(
                [{"name": f["name"], "url": f["url"], "enabled": f.get("enabled", True)} for f in feeds],
                category="builder",
                max_age_hours=48,
            )
        )

    # GitHub Trending
    gh_cfg = cat_cfg.get("github_trending", {})
    if gh_cfg.get("enabled", True):
        stories.extend(
            fetch_trending(
                language=gh_cfg.get("language", ""),
                since=gh_cfg.get("since", "daily"),
                max_repos=gh_cfg.get("max_repos", 10),
                category="builder",
            )
        )

    # Tavily
    queries = cat_cfg.get("tavily_queries", [])
    stories.extend(run_queries(queries, category="builder", days=3))

    # No-feed sites
    default_queries = cat_cfg.get("no_feed_default_queries", ["news today"])
    for site in cat_cfg.get("no_feed_sites", []):
        if not site.get("enabled", True):
            continue
        site_queries = [site["query"]] if site.get("query") else default_queries
        site_count = 0
        for q in site_queries:
            site_stories = search_site(site["domain"], q, category="builder", days=2)
            for s in site_stories:
                s["source"] = site["name"]
            stories.extend(site_stories)
            site_count += len(site_stories)
        if site_count > 0:
            record_success(site["name"])
        else:
            record_zero_yield(site["name"])

    logger.info(f"Builder: {len(stories)} raw stories")
    return _dedupe_urls(stories)


def gather_critical_voices(llm: LLM) -> list[dict]:
    """Gather critical/sceptic AI content from RSS and Tavily."""
    agents_cfg, sources_cfg = _load_config()
    cat_cfg = sources_cfg.get("critical_voices", {})

    stories = []

    # RSS
    feeds = cat_cfg.get("rss_feeds", [])
    stories.extend(fetch_multiple_feeds(feeds, category="critical_voices", max_age_hours=72))

    # Tavily
    queries = cat_cfg.get("tavily_queries", [])
    stories.extend(run_queries(queries, category="critical_voices", days=3))

    # No-feed sites
    default_queries = cat_cfg.get("no_feed_default_queries", ["news today"])
    for site in cat_cfg.get("no_feed_sites", []):
        if not site.get("enabled", True):
            continue
        site_queries = [site["query"]] if site.get("query") else default_queries
        site_count = 0
        for q in site_queries:
            site_stories = search_site(site["domain"], q, category="critical_voices", days=2)
            for s in site_stories:
                s["source"] = site["name"]
            stories.extend(site_stories)
            site_count += len(site_stories)
        if site_count > 0:
            record_success(site["name"])
        else:
            record_zero_yield(site["name"])

    logger.info(f"CriticalVoices: {len(stories)} raw stories")
    return _dedupe_urls(stories)


def _dedupe_urls(stories: list[dict]) -> list[dict]:
    """Remove duplicate URLs, keeping first occurrence."""
    seen = set()
    unique = []
    for s in stories:
        url = s.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(s)
    return unique
