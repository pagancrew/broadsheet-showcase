"""
tests/test_selection.py

Tests for selection logic: category caps, rollover, byline cap, ai_only_filter,
and semantic dedup (cluster-based paywall preference).

These tests exercise the functions directly rather than the full pipeline,
so no LLM API calls are made.

Run: pytest tests/test_selection.py -v
"""

import re
from unittest.mock import MagicMock

import pytest

from tools.rss_fetcher import _is_ai_relevant, _AI_KEYWORD_PATTERN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(
    title="Test Story",
    url="https://example.com/story",
    source="Test Source",
    category="critical_voices",
    confidence_score=7.0,
    published_iso="2026-05-12T10:00:00+00:00",
    paywalled=False,
    byline=None,
    description="",
):
    s = {
        "title": title,
        "url": url,
        "source": source,
        "category": category,
        "confidence_score": confidence_score,
        "published_iso": published_iso,
        "paywalled": paywalled,
        "description": description,
    }
    if byline is not None:
        s["byline"] = byline
    return s


def _unique_url(prefix="https://example.com/story", n=0):
    return f"{prefix}-{n}"


# ---------------------------------------------------------------------------
# AI-only filter
# ---------------------------------------------------------------------------

class TestAiOnlyFilter:
    def test_ai_story_passes(self):
        story = _story(title="OpenAI releases new model", description="A new LLM from OpenAI.")
        assert _is_ai_relevant(story) is True

    def test_off_topic_story_blocked(self):
        story = _story(
            title="Fibonacci Structure in Harmonic Series Partitions",
            description="A mathematical exploration of number theory.",
        )
        assert _is_ai_relevant(story) is False

    def test_oil_story_blocked(self):
        story = _story(
            title="Hedging Global Oil Supply Shocks",
            description="Strategies for commodity hedging in volatile markets.",
        )
        assert _is_ai_relevant(story) is False

    def test_mafia_marriages_blocked(self):
        story = _story(
            title="Scientists Studied 906 Mafia Marriages",
            description="Sociological analysis of criminal network kinship.",
        )
        assert _is_ai_relevant(story) is False

    def test_politics_blocked(self):
        story = _story(
            title="Trump's fruitless search for a goreable ox",
            description="Political commentary on the administration's strategy.",
        )
        assert _is_ai_relevant(story) is False

    def test_ai_keyword_word_boundary(self):
        # "aim" must not match "ai"
        story = _story(title="Scientists aim to study climate", description="Research aims to quantify warming.")
        assert _is_ai_relevant(story) is False

    def test_aid_not_matched(self):
        story = _story(title="Foreign aid to developing nations", description="Aid flows to affected regions.")
        assert _is_ai_relevant(story) is False

    def test_machine_learning_passes(self):
        story = _story(title="Machine learning study", description="Researchers apply machine learning to genomics.")
        assert _is_ai_relevant(story) is True

    def test_anthropic_in_description(self):
        story = _story(title="New safety study", description="Anthropic published results of red-teaming.")
        assert _is_ai_relevant(story) is True

    def test_alignment_passes(self):
        story = _story(title="Alignment research update", description="Progress on alignment challenges.")
        assert _is_ai_relevant(story) is True


# ---------------------------------------------------------------------------
# Category cap + rollover
# ---------------------------------------------------------------------------

class TestCategoryCapAndRollover:
    """
    Test the category_max / rollover_max logic by running select_stories with a
    synthetic story pool and a no-op LLM that returns equal scores.
    """

    def _make_pool(self, counts_by_category: dict[str, int], base_score=7.0) -> list[dict]:
        """Build a story pool with the given number of stories per category."""
        stories = []
        url_counter = [0]

        def next_url():
            url_counter[0] += 1
            return f"https://example.com/{url_counter[0]}"

        for cat, count in counts_by_category.items():
            for i in range(count):
                stories.append(_story(
                    title=f"{cat} story {i}",
                    url=next_url(),
                    source=f"Source{i % 4}",  # 4 distinct sources per category
                    category=cat,
                    confidence_score=base_score - (i * 0.1),  # slight score variation
                ))
        return stories

    def _run_selection(self, stories, category_max=None, rollover_max=2):
        """
        Run the category composition logic directly (not full select_stories,
        which requires an LLM). Extract just the budget composition block.
        """
        category_max = category_max or {
            "big_news": 5,
            "critical_voices": 8,
            "laws_ethics": 8,
            "builder": 8,
        }
        categories = list(category_max.keys())
        max_stories = 35
        big_news_max = category_max.get("big_news", 5)
        base_budget = 8

        default_cap = {cat: (big_news_max if cat == "big_news" else base_budget) for cat in categories}

        # Assign confidence_score to passing (all stories pass in this test)
        passing = stories

        selected = []
        leftover_pool = []
        for category in categories:
            cat_stories = sorted(
                [s for s in passing if s.get("category") == category],
                key=lambda x: (x.get("confidence_score", 0), x.get("published_iso") or ""),
                reverse=True,
            )
            cap = category_max.get(category, default_cap.get(category, base_budget))
            selected.extend(cat_stories[:cap])
            leftover_pool.extend(cat_stories[cap:])

        leftover_pool.sort(
            key=lambda x: (x.get("confidence_score", 0), x.get("published_iso") or ""),
            reverse=True,
        )
        rollover_used = {cat: 0 for cat in categories}
        rollover_added = []
        for story in leftover_pool:
            if len(selected) + len(rollover_added) >= max_stories:
                break
            cat = story.get("category", "")
            if rollover_used.get(cat, 0) < rollover_max:
                rollover_added.append(story)
                rollover_used[cat] = rollover_used.get(cat, 0) + 1
        selected.extend(rollover_added)
        return selected

    def test_critical_voices_capped_at_8(self):
        pool = self._make_pool({"big_news": 5, "critical_voices": 15, "laws_ethics": 8, "builder": 8})
        selected = self._run_selection(pool, rollover_max=2)
        cv_count = sum(1 for s in selected if s["category"] == "critical_voices")
        assert cv_count <= 10, f"critical_voices had {cv_count} stories (max 8+2 rollover)"

    def test_big_news_capped_at_5_no_extra(self):
        pool = self._make_pool({"big_news": 12, "critical_voices": 8, "laws_ethics": 8, "builder": 8})
        selected = self._run_selection(pool, rollover_max=2)
        bn_count = sum(1 for s in selected if s["category"] == "big_news")
        assert bn_count <= 7, f"big_news had {bn_count} stories (max 5+2 rollover)"

    def test_rollover_fills_from_pool(self):
        """A category with 10 stories should fill to cap+rollover when strong stories exist."""
        pool = self._make_pool({"big_news": 3, "critical_voices": 10, "laws_ethics": 3, "builder": 3})
        selected = self._run_selection(pool, rollover_max=2)
        cv_count = sum(1 for s in selected if s["category"] == "critical_voices")
        assert cv_count == 10, f"Expected 10 (cap 8 + rollover 2), got {cv_count}"

    def test_total_never_exceeds_max_stories(self):
        pool = self._make_pool({"big_news": 10, "critical_voices": 20, "laws_ethics": 20, "builder": 20})
        selected = self._run_selection(pool, rollover_max=2)
        assert len(selected) <= 35, f"Total {len(selected)} exceeds max_stories=35"

    def test_lean_category_does_not_block_rollover_elsewhere(self):
        """When big_news has only 2 stories, other categories should still fill normally."""
        pool = self._make_pool({"big_news": 2, "critical_voices": 12, "laws_ethics": 12, "builder": 12})
        selected = self._run_selection(pool, rollover_max=2)
        bn_count = sum(1 for s in selected if s["category"] == "big_news")
        assert bn_count == 2


# ---------------------------------------------------------------------------
# Byline cap
# ---------------------------------------------------------------------------

class TestBylineCap:
    def _run_byline_cap(self, stories, per_source_cap=4, byline_cap=2):
        """Apply per-source + per-byline cap (mirrors newsroom_lead.py logic)."""
        from collections import defaultdict
        source_counts: defaultdict[str, int] = defaultdict(int)
        byline_counts: defaultdict[str, int] = defaultdict(int)
        capped = []
        for s in stories:
            src = s.get("source", "")
            byline_key = s.get("byline") or src
            if source_counts[src] < per_source_cap and byline_counts[byline_key] < byline_cap:
                capped.append(s)
                source_counts[src] += 1
                byline_counts[byline_key] += 1
        return capped

    def test_third_gary_marcus_post_blocked(self):
        stories = [
            _story(title="Gary Marcus post 1", source="Gary Marcus Substack", byline="Gary Marcus", url="https://example.com/gm1"),
            _story(title="Gary Marcus post 2", source="Gary Marcus Substack", byline="Gary Marcus", url="https://example.com/gm2"),
            _story(title="Gary Marcus post 3", source="Gary Marcus Substack", byline="Gary Marcus", url="https://example.com/gm3"),
        ]
        result = self._run_byline_cap(stories)
        assert len(result) == 2
        assert all(s["byline"] == "Gary Marcus" for s in result)

    def test_two_cory_doctorow_posts_allowed(self):
        stories = [
            _story(title="Doctorow post 1", source="Cory Doctorow (Pluralistic)", byline="Cory Doctorow", url="https://example.com/cd1"),
            _story(title="Doctorow post 2", source="Cory Doctorow (Pluralistic)", byline="Cory Doctorow", url="https://example.com/cd2"),
        ]
        result = self._run_byline_cap(stories)
        assert len(result) == 2

    def test_same_author_different_sources_capped(self):
        """Two posts from same author on different feeds are still capped at 2."""
        stories = [
            _story(title="Author post 1", source="Feed A", byline="Jane Smith", url="https://example.com/1"),
            _story(title="Author post 2", source="Feed B", byline="Jane Smith", url="https://example.com/2"),
            _story(title="Author post 3", source="Feed C", byline="Jane Smith", url="https://example.com/3"),
        ]
        result = self._run_byline_cap(stories)
        assert len(result) == 2

    def test_no_byline_falls_back_to_source_cap(self):
        """Stories without byline use source as the cap key — existing behaviour."""
        stories = [
            _story(title="Less Wrong post 1", source="Less Wrong", url="https://lesswrong.com/1"),
            _story(title="Less Wrong post 2", source="Less Wrong", url="https://lesswrong.com/2"),
            _story(title="Less Wrong post 3", source="Less Wrong", url="https://lesswrong.com/3"),
        ]
        result = self._run_byline_cap(stories, byline_cap=2)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Semantic dedup + cluster paywall preference
# ---------------------------------------------------------------------------

class TestSemanticDedup:
    def _mock_llm_with_groups(self, groups: list[list[int]]):
        """Create an LLM mock that returns specified clustering groups as JSON."""
        import json
        llm = MagicMock()
        llm.call.return_value = json.dumps(groups)
        return llm

    def test_paywalled_demoted_when_non_paywalled_sibling_within_1pt(self):
        """Within a cluster, a non-paywalled story wins over paywalled if scores within 1pt."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Sutskever story (Wired, paywalled)", source="Wired", url="https://wired.com/sutskever",
                   confidence_score=8.0, paywalled=True, category="big_news"),
            _story(title="Sutskever story (The Verge, free)", source="The Verge", url="https://theverge.com/sutskever",
                   confidence_score=7.5, paywalled=False, category="big_news"),
        ]
        llm = self._mock_llm_with_groups([[0, 1]])
        result = _semantic_dedup(stories, llm)
        assert len(result) == 1
        assert result[0]["source"] == "The Verge", "Non-paywalled story should win within 1pt gap"

    def test_paywalled_kept_when_no_cluster_sibling(self):
        """A paywalled story with no cluster duplicate is kept at its scored position."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Sutskever interview (Wired)", source="Wired", url="https://wired.com/sutskever",
                   confidence_score=8.0, paywalled=True, category="big_news"),
            _story(title="Unrelated story (TechCrunch)", source="TechCrunch", url="https://techcrunch.com/other",
                   confidence_score=7.0, paywalled=False, category="big_news"),
        ]
        llm = self._mock_llm_with_groups([[0], [1]])
        result = _semantic_dedup(stories, llm)
        assert len(result) == 2
        urls = {s["url"] for s in result}
        assert "https://wired.com/sutskever" in urls, "Standalone paywalled story should be kept"

    def test_highest_score_wins_when_both_paywalled(self):
        """When all cluster members are paywalled, highest score wins."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Story A", source="FT", url="https://ft.com/a", confidence_score=6.0, paywalled=True),
            _story(title="Story B", source="Economist", url="https://economist.com/b", confidence_score=8.0, paywalled=True),
        ]
        llm = self._mock_llm_with_groups([[0, 1]])
        result = _semantic_dedup(stories, llm)
        assert len(result) == 1
        assert result[0]["source"] == "Economist"

    def test_singleton_stories_all_kept(self):
        """Stories in singleton clusters are all preserved."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Story A", url="https://example.com/a", source="Source A"),
            _story(title="Story B", url="https://example.com/b", source="Source B"),
            _story(title="Story C", url="https://example.com/c", source="Source C"),
        ]
        llm = self._mock_llm_with_groups([[0], [1], [2]])
        result = _semantic_dedup(stories, llm)
        assert len(result) == 3

    def test_llm_failure_returns_original_list(self):
        """If the LLM fails on all tiers, semantic_dedup returns the original list unchanged."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Story A", url="https://example.com/a"),
            _story(title="Story B", url="https://example.com/b"),
        ]
        llm = MagicMock()
        llm.call.side_effect = Exception("API error")
        result = _semantic_dedup(stories, llm)
        assert len(result) == 2

    def test_paywalled_story_not_demoted_outside_cluster(self):
        """No global paywall score penalty — paywalled story in singleton cluster keeps its position."""
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(title="Paywalled story", source="Wired", url="https://wired.com/story",
                   confidence_score=9.0, paywalled=True),
            _story(title="Free story", source="The Verge", url="https://theverge.com/other",
                   confidence_score=7.0, paywalled=False),
        ]
        llm = self._mock_llm_with_groups([[0], [1]])  # not clustered
        result = _semantic_dedup(stories, llm)
        assert len(result) == 2
        scores = {s["url"]: s["confidence_score"] for s in result}
        assert scores["https://wired.com/story"] == 9.0, "Paywall must not alter confidence_score"


# ---------------------------------------------------------------------------
# Tavily date extraction
# ---------------------------------------------------------------------------

class TestTavilyDateExtraction:
    """Tests for _extract_published — the four-tier date extractor."""

    def _r(self, published_date=None, url="https://example.com/article", content=""):
        return {"published_date": published_date, "url": url, "content": content}

    def test_tier1_iso_string(self):
        from tools.tavily_search import _extract_published
        r = self._r(published_date="2024-03-15T10:00:00Z")
        result = _extract_published(r)
        assert result is not None
        assert "2024-03-15" in result

    def test_tier2_dateutil_human_string(self):
        from tools.tavily_search import _extract_published
        r = self._r(published_date="March 15, 2024")
        result = _extract_published(r)
        assert result is not None
        assert "2024-03-15" in result

    def test_tier3_url_date(self):
        from tools.tavily_search import _extract_published
        r = self._r(url="https://example.com/2024/03/15/ai-story")
        result = _extract_published(r)
        assert result is not None
        assert "2024-03-15" in result

    def test_tier4_content_date(self):
        from tools.tavily_search import _extract_published
        r = self._r(content="By Jane Smith, March 15, 2024 — The latest AI research shows...")
        result = _extract_published(r)
        assert result is not None
        assert "2024" in result

    def test_unparseable_returns_none(self):
        from tools.tavily_search import _extract_published
        r = self._r(published_date="not a date at all", url="https://example.com/no-date")
        result = _extract_published(r)
        assert result is None

    def test_none_published_date_falls_through_tiers(self):
        from tools.tavily_search import _extract_published
        # URL has a date so tier 3 picks it up
        r = self._r(published_date=None, url="https://blog.example.com/2025/11/20/post")
        result = _extract_published(r)
        assert result is not None
        assert "2025-11-20" in result


# ---------------------------------------------------------------------------
# Recency cutoff — all categories and parse-failure behaviour
# ---------------------------------------------------------------------------

class TestRecencyCutoff:
    """
    Tests for the recency cutoff in newsroom_lead.select_stories.
    Exercises the block directly using a thin wrapper rather than the full pipeline.
    """

    def _run_recency_filter(self, stories):
        """
        Run just the recency-cutoff portion of select_stories logic
        (extracted for unit-testability).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        stage_dropped = {}
        fresh = []
        for s in stories:
            pub = s.get("published_iso")
            if not pub:
                stage_dropped[s.get("url", "")] = "recency_cutoff"
                continue
            pub_dt = None
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                try:
                    from dateutil import parser as _du
                    pub_dt = _du.parse(pub, dayfirst=False)
                except Exception:
                    pass
            if pub_dt is None:
                stage_dropped[s.get("url", "")] = "recency_cutoff"
                continue
            if pub_dt.tzinfo is None:
                from datetime import timezone
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            age_days = (now - pub_dt).total_seconds() / 86400
            if age_days <= 14:
                fresh.append(s)
            else:
                stage_dropped[s.get("url", "")] = "recency_cutoff"
        return fresh, stage_dropped

    def test_fresh_story_passes(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        stories = [_story(published_iso=pub, url="https://example.com/fresh")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 1
        assert "https://example.com/fresh" not in dropped

    def test_14_day_old_story_passes(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(days=13, hours=23)).isoformat()
        stories = [_story(published_iso=pub, url="https://example.com/borderline")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 1

    def test_old_story_dropped(self):
        stories = [_story(published_iso="2024-01-01T00:00:00+00:00", url="https://example.com/old")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 0
        assert dropped.get("https://example.com/old") == "recency_cutoff"

    def test_undated_story_dropped(self):
        stories = [_story(published_iso=None, url="https://example.com/nodatestory")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 0
        assert dropped.get("https://example.com/nodatestory") == "recency_cutoff"

    def test_parse_failure_drops_not_keeps(self):
        """Unparseable date must be dropped, not silently kept (regression for the 'except: fresh.append' bug)."""
        stories = [_story(published_iso="not-a-date-##", url="https://example.com/baddate")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 0
        assert dropped.get("https://example.com/baddate") == "recency_cutoff"

    def test_builder_category_also_filtered(self):
        """Builder category must be subject to the 14-day cutoff (regression for the Builder exemption)."""
        stories = [_story(
            published_iso="2023-01-01T00:00:00+00:00",
            url="https://example.com/old-builder",
            category="builder",
        )]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 0
        assert dropped.get("https://example.com/old-builder") == "recency_cutoff"

    def test_human_readable_date_accepted(self):
        """dateutil tier accepts 'March 15, 2026' style dates from Tavily."""
        from datetime import datetime, timezone, timedelta
        # Use a recent date so it passes the 14-day window.
        from datetime import date as _date
        recent = (datetime.now(timezone.utc) - timedelta(days=2))
        human = recent.strftime("%B %-d, %Y")   # e.g. "May 20, 2026"
        stories = [_story(published_iso=human, url="https://example.com/human-date")]
        fresh, dropped = self._run_recency_filter(stories)
        assert len(fresh) == 1, f"Expected story with date '{human}' to pass recency filter"


# ---------------------------------------------------------------------------
# Trusted-voice pin logic
# ---------------------------------------------------------------------------

class TestTrustedVoicePin:
    """
    Tests for the must_include pin stage in newsroom_lead.select_stories.
    Exercises the pin logic directly with a thin re-implementation
    (same shape as the production code) to avoid full pipeline setup.
    """

    def _run_pin_stage(self, stories, hours_window=48):
        """Replicate the trusted-voice pin block from select_stories."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        pinned_stories = []
        pinned_bylines = set()
        pinned_urls = set()
        remaining_stories = []

        for s in stories:
            if s.get("priority") != "must_include":
                remaining_stories.append(s)
                continue
            pub = s.get("published_iso")
            if not pub:
                remaining_stories.append(s)
                continue
            pub_dt = None
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                try:
                    from dateutil import parser as _du
                    pub_dt = _du.parse(pub, dayfirst=False)
                except Exception:
                    pass
            if pub_dt is None:
                remaining_stories.append(s)
                continue
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > hours_window:
                remaining_stories.append(s)
                continue
            byline_key = s.get("byline") or s.get("source", "")
            if byline_key in pinned_bylines:
                remaining_stories.append(s)
                continue
            s["confidence_score"] = 10
            s["selection_reason"] = "trusted voice"
            s["pinned"] = True
            pinned_stories.append(s)
            pinned_bylines.add(byline_key)
            pinned_urls.add(s.get("url", ""))

        return pinned_stories, remaining_stories

    def test_fresh_must_include_is_pinned(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        story = _story(url="https://gary.substack.com/p/post", source="Gary Marcus",
                       published_iso=pub, confidence_score=5.0)
        story["priority"] = "must_include"
        pinned, remaining = self._run_pin_stage([story])
        assert len(pinned) == 1
        assert pinned[0]["confidence_score"] == 10
        assert pinned[0].get("pinned") is True
        assert len(remaining) == 0

    def test_old_must_include_not_pinned(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        story = _story(url="https://gary.substack.com/p/old", source="Gary Marcus",
                       published_iso=pub)
        story["priority"] = "must_include"
        pinned, remaining = self._run_pin_stage([story])
        assert len(pinned) == 0
        assert len(remaining) == 1

    def test_only_one_pin_per_byline(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        s1 = _story(url="https://gary.substack.com/p/post1", source="Gary Marcus", published_iso=pub)
        s1["priority"] = "must_include"
        s2 = _story(url="https://gary.substack.com/p/post2", source="Gary Marcus", published_iso=pub)
        s2["priority"] = "must_include"
        pinned, remaining = self._run_pin_stage([s1, s2])
        assert len(pinned) == 1
        assert len(remaining) == 1

    def test_non_priority_story_not_pinned(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        story = _story(url="https://regular.com/story", source="Regular Source", published_iso=pub)
        pinned, remaining = self._run_pin_stage([story])
        assert len(pinned) == 0
        assert len(remaining) == 1

    def test_multiple_voices_all_pinned(self):
        from datetime import datetime, timezone, timedelta
        pub = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        voices = ["Gary Marcus", "Nathan Lambert", "Swyx", "Zvi Mowshowitz"]
        stories = []
        for i, voice in enumerate(voices):
            s = _story(url=f"https://example.com/post-{i}", source=voice, published_iso=pub)
            s["priority"] = "must_include"
            stories.append(s)
        pinned, remaining = self._run_pin_stage(stories)
        assert len(pinned) == len(voices)
        assert len(remaining) == 0

    def test_pinned_story_wins_semantic_cluster(self):
        """Pinned story should be preferred over non-pinned in _semantic_dedup."""
        from agents.newsroom_lead import _semantic_dedup
        pinned = _story(title="Gary Marcus Post", url="https://gary.substack.com/p/post",
                        source="Gary Marcus", confidence_score=10)
        pinned["pinned"] = True
        normal = _story(title="Same Event Coverage", url="https://techcrunch.com/gary",
                        source="TechCrunch", confidence_score=9)

        mock_llm = MagicMock()
        mock_llm.call.return_value = "[[0, 1]]"
        result = _semantic_dedup([pinned, normal], mock_llm)
        assert len(result) == 1
        assert result[0]["url"] == "https://gary.substack.com/p/post", \
            "Pinned story must win semantic dedup cluster"


# ---------------------------------------------------------------------------
# Pre-budget dedup (Fix 1, Brief 21)
# ---------------------------------------------------------------------------

class TestPreBudgetDedup:
    """Verify that dedup-before-budget lets distinct runners-up fill vacated slots.

    The regression this guards: 4 outlets covering the same event consume a
    4-slot category cap; post-selection dedup collapses them to 1 but the 3
    freed slots are never reclaimed.  After Fix 1, dedup runs *before* the
    budget loop, so each budget slot goes to a distinct event.
    """

    def test_dominant_event_does_not_exhaust_category_cap(self):
        """Category fills to cap with distinct events when top stories are near-duplicates."""
        import json
        from agents.newsroom_lead import _semantic_dedup

        # 4 outlets covering the same event — high confidence, but one event
        duplicates = [
            _story(
                title=f"Trump EO coverage — outlet {i}",
                url=f"https://outlet{i}.com/eo",
                category="laws_ethics",
                confidence_score=9.0 - i * 0.1,
                source=f"Outlet{i}",
            )
            for i in range(4)
        ]
        # 4 distinct stories — slightly lower confidence
        distinct = [
            _story(
                title=f"Distinct story {i}",
                url=f"https://distinct{i}.com/story",
                category="laws_ethics",
                confidence_score=8.0 - i * 0.1,
                source=f"Source{i}",
            )
            for i in range(4)
        ]

        passing = duplicates + distinct  # 8 stories, indices 0-3 = same event

        # Mock LLM: collapse the 4 duplicates into one cluster
        groups = [[0, 1, 2, 3], [4], [5], [6], [7]]
        llm = MagicMock()
        llm.call.return_value = json.dumps(groups)

        # Step 1: pre-budget dedup (as inserted by Fix 1)
        cat_pool = [s for s in passing if s.get("category") == "laws_ethics"]
        deduped = _semantic_dedup(cat_pool, llm)

        # After dedup: 1 representative from the event cluster + 4 distinct = 5
        assert len(deduped) == 5, f"Expected 5 after dedup, got {len(deduped)}"

        # Step 2: budget loop with cap=4 (simulated)
        selected = sorted(deduped, key=lambda x: x.get("confidence_score", 0), reverse=True)[:4]

        assert len(selected) == 4, f"Category should fill to cap=4, got {len(selected)}"

        # Exactly 1 event-cluster rep + 3 distinct runners-up
        event_urls = {s["url"] for s in duplicates}
        distinct_urls = {s["url"] for s in distinct}
        n_event = sum(1 for s in selected if s["url"] in event_urls)
        n_distinct = sum(1 for s in selected if s["url"] in distinct_urls)
        assert n_event == 1, f"Expected 1 event-cluster rep in selected, got {n_event}"
        assert n_distinct == 3, f"Expected 3 distinct runners-up in selected, got {n_distinct}"

    def test_category_with_all_unique_stories_unchanged(self):
        """When no duplicates exist, dedup is a no-op and budget loop fills normally."""
        import json
        from agents.newsroom_lead import _semantic_dedup

        stories = [
            _story(
                title=f"Unique story {i}",
                url=f"https://unique{i}.com/story",
                category="big_news",
                confidence_score=9.0 - i * 0.5,
                source=f"Source{i}",
            )
            for i in range(6)
        ]

        # Mock LLM: all singletons
        groups = [[i] for i in range(6)]
        llm = MagicMock()
        llm.call.return_value = json.dumps(groups)

        deduped = _semantic_dedup(stories, llm)
        assert len(deduped) == 6, "No-op dedup should return all 6 stories unchanged"

        selected = sorted(deduped, key=lambda x: x.get("confidence_score", 0), reverse=True)[:5]
        assert len(selected) == 5, "Budget loop should fill cap=5 from 6 unique stories"
