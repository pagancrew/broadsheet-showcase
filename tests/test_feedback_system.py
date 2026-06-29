"""
tests/test_feedback_system.py

Verifies the feedback system end-to-end using mock data.
No Notion API, no LLM, no external calls required.

Run with: python -m pytest tests/test_feedback_system.py -v
"""

import json
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.newsroom_lead import (
    _wilson_lower_bound,
    update_calibration_from_feedback,
    select_stories,
    _cluster_stories,
)
from agents.editor import write_digest


def _passthrough_cluster(stories, threshold=0.35):
    """Bypass clustering — returns all stories as their own clusters."""
    return list(stories), {s.get("url", ""): 1 for s in stories}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_story(title, category, source=None, url=None):
    # Use title as source by default — guaranteed unique and deterministic, avoiding
    # hash-collision issues with hash randomization in Python 3.3+.
    # published_iso must be within the 14-day recency window — use "now" so tests
    # don't break when the hardcoded date drifts outside the cutoff.
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    return {
        "title": title,
        "category": category,
        "source": source or title,
        "url": url or f"https://example.com/{title.replace(' ', '-')}",
        "description": f"Description of {title}",
        "published_iso": recent,
    }


def _mock_llm_select(scores: list[dict]):
    """Return a mock LLM that yields pre-determined story scores."""
    llm = MagicMock()
    llm.call.return_value = json.dumps(scores)
    return llm


def _mock_llm_edit(summaries: list[dict]):
    """Return a mock LLM that yields pre-determined summaries with tags."""
    llm = MagicMock()
    llm.call.return_value = json.dumps(summaries)
    return llm


# ---------------------------------------------------------------------------
# Test 1: Tagging — editor attaches tags to every story
# ---------------------------------------------------------------------------

def test_editor_attaches_tags():
    stories = [
        _make_story("GPT-6 released", "big_news"),
        _make_story("EU AI Act delayed", "laws_ethics"),
        _make_story("New OSS tool for RAG", "builder"),
        _make_story("AI hype critique", "critical_voices"),
    ]

    mock_summaries = [
        {"id": i, "summary": f"Summary {i}", "tags": ["model release", "industry news"]}
        for i in range(len(stories))
    ]
    llm = _mock_llm_edit(mock_summaries)

    with patch("agents.editor._load_agents_config", return_value={"editor_agent": {}}):
        with patch("agents.editor._load_digest_config", return_value={"format": {"category_order": []}}):
            result, _, _ = write_digest(stories, llm)

    assert len(result) == 4
    for story in result:
        assert "tags" in story, f"Story '{story['title']}' missing tags"
        assert len(story["tags"]) >= 1, f"Story '{story['title']}' has empty tags"

    # Tags must not be source names or category names
    forbidden = {"big_news", "laws_ethics", "builder", "critical_voices",
                 "Test Source", "Big News", "Law & Ethics", "Builder", "Critical Voices"}
    for story in result:
        for tag in story["tags"]:
            assert tag not in forbidden, f"Tag '{tag}' looks like a source or category name"


# ---------------------------------------------------------------------------
# Test 2: Category budget — each category gets equal base allocation
# ---------------------------------------------------------------------------

def test_category_budget_allocation():
    # 4 Big News, 2 Critical Voices, 2 Builder, 2 Laws & Ethics = 10 stories
    stories = (
        [_make_story(f"Big News {i}", "big_news") for i in range(4)] +
        [_make_story(f"Critical {i}", "critical_voices") for i in range(2)] +
        [_make_story(f"Builder {i}", "builder") for i in range(2)] +
        [_make_story(f"Law {i}", "laws_ethics") for i in range(2)]
    )

    # All stories score 7 (above default min_confidence=6)
    scores = [{"id": i, "confidence_score": 7, "reason": "good"} for i in range(len(stories))]
    llm = _mock_llm_select(scores)

    digest_cfg = {
        "max_stories": 8,
        "diversity_slot": False,
        "diversity_min_quality": 4,
        "format": {
            "category_labels": {
                "big_news": "Big News",
                "laws_ethics": "Law & Ethics",
                "builder": "Builder",
                "critical_voices": "Critical Voices",
            },
            "category_order": ["big_news", "builder", "laws_ethics", "critical_voices"],
        },
    }

    with patch("agents.newsroom_lead._load_calibration", return_value={}):
        with patch("agents.newsroom_lead._save_calibration"):
            with patch("agents.newsroom_lead._load_agents_config", return_value={"newsroom_lead_agent": {}}):
                with patch("agents.newsroom_lead._load_digest_config", return_value=digest_cfg):
                    with patch("tools.notion_delivery.write_candidates_log"):
                        with patch("agents.newsroom_lead._cluster_stories", side_effect=_passthrough_cluster):
                            result, _ = select_stories(stories, {}, llm, min_confidence=6)

    assert len(result) == 8, f"Expected 8 stories, got {len(result)}"

    # Each category should appear at least once (base_budget=2 per category)
    result_cats = [s["category"] for s in result]
    for cat in ["big_news", "laws_ethics", "builder", "critical_voices"]:
        assert cat in result_cats, f"Category '{cat}' missing from selection"

    # Big News had 4 stories but budget is 2 — should only contribute 2 in base pass
    big_news_count = result_cats.count("big_news")
    assert big_news_count == 2, f"Expected 2 Big News in base pass, got {big_news_count}"


# ---------------------------------------------------------------------------
# Test 3: Budget rollover — empty category donates its slots
# ---------------------------------------------------------------------------

def test_budget_rollover_when_category_empty():
    # Critical Voices has 0 stories; Big News has 6
    stories = (
        [_make_story(f"Big News {i}", "big_news") for i in range(6)] +
        [_make_story(f"Builder {i}", "builder") for i in range(2)] +
        [_make_story(f"Law {i}", "laws_ethics") for i in range(2)]
    )

    scores = [{"id": i, "confidence_score": 7, "reason": "good"} for i in range(len(stories))]
    llm = _mock_llm_select(scores)

    digest_cfg = {
        "max_stories": 8,
        "diversity_slot": False,
        "diversity_min_quality": 4,
        "format": {
            "category_labels": {
                "big_news": "Big News",
                "laws_ethics": "Law & Ethics",
                "builder": "Builder",
                "critical_voices": "Critical Voices",
            },
            "category_order": ["big_news", "builder", "laws_ethics", "critical_voices"],
        },
    }

    with patch("agents.newsroom_lead._load_calibration", return_value={}):
        with patch("agents.newsroom_lead._save_calibration"):
            with patch("agents.newsroom_lead._load_agents_config", return_value={"newsroom_lead_agent": {}}):
                with patch("agents.newsroom_lead._load_digest_config", return_value=digest_cfg):
                    with patch("tools.notion_delivery.write_candidates_log"):
                        with patch("agents.newsroom_lead._cluster_stories", side_effect=_passthrough_cluster):
                            result, _ = select_stories(stories, {}, llm, min_confidence=6)

    assert len(result) == 8, f"Expected 8 stories total after rollover, got {len(result)}"
    # Big News should have more than its base budget (2) due to rollover
    big_news_count = sum(1 for s in result if s["category"] == "big_news")
    assert big_news_count > 2, f"Rollover didn't work — Big News only got {big_news_count} stories"


# ---------------------------------------------------------------------------
# Test 4: Wilson score — single rating produces conservative boost
# ---------------------------------------------------------------------------

def test_wilson_score_is_conservative_for_sparse_data():
    score_1_vote = _wilson_lower_bound(1, 1)     # 1 click
    score_10_votes = _wilson_lower_bound(10, 10)  # 10 clicks

    # Both are 100% approval but different confidence — 10 votes should score higher
    assert score_10_votes > score_1_vote, (
        f"10 clicks ({score_10_votes:.3f}) should outscore 1 click ({score_1_vote:.3f})"
    )

    # Single click should be noticeably conservative (well below 1.0)
    assert score_1_vote < 0.7, f"Single click Wilson score {score_1_vote:.3f} is not conservative enough"


def test_wilson_boost_scales_with_clicks():
    feedback_1 = {"tag_feedback": {"benchmark": {"more": 0, "less": 0, "top": 1}}, "total_items": 1}
    feedback_10 = {"tag_feedback": {"benchmark": {"more": 0, "less": 0, "top": 10}}, "total_items": 10}

    # Use side_effect so each call gets a fresh dict, not the same object
    with patch("agents.newsroom_lead._load_calibration", side_effect=lambda: {}):
        with patch("agents.newsroom_lead._save_calibration"):
            cal_1 = update_calibration_from_feedback(feedback_1)
            cal_10 = update_calibration_from_feedback(feedback_10)

    weight_1 = cal_1["tag_weights"].get("benchmark", 1.0)
    weight_10 = cal_10["tag_weights"].get("benchmark", 1.0)

    assert weight_10 > weight_1, f"10 clicks ({weight_10}) should outweigh 1 click ({weight_1})"
    # But NOT 10x more (log damping)
    assert weight_10 < weight_1 * 5, "Log damping failed — weight grew more than expected"


# ---------------------------------------------------------------------------
# Test 5: Log accumulation — 20 clicks does not saturate the weight cap
# ---------------------------------------------------------------------------

def test_log_accumulation_prevents_saturation():
    feedback = {"tag_feedback": {"model release": {"more": 0, "less": 0, "top": 20}}, "total_items": 20}

    with patch("agents.newsroom_lead._load_calibration", return_value={}):
        with patch("agents.newsroom_lead._save_calibration"):
            cal = update_calibration_from_feedback(feedback)

    weight = cal["tag_weights"].get("model release", 1.0)
    assert weight < 1.5, f"20 clicks pushed weight to {weight} — log damping should prevent this"
    assert weight > 1.0, f"Weight should be above 1.0 after positive feedback, got {weight}"


# ---------------------------------------------------------------------------
# Test 6: Click-through boosts tag weight and source affinity
# ---------------------------------------------------------------------------

def test_top_click_boosts_tag_and_source():
    feedback = {
        "tag_feedback": {"safety research": {"more": 0, "less": 0, "top": 3}},
        "source_feedback": {"The Verge AI": 3},
        "total_items": 3,
    }

    with patch("agents.newsroom_lead._load_calibration", return_value={}):
        with patch("agents.newsroom_lead._save_calibration") as mock_save:
            cal = update_calibration_from_feedback(feedback)

    # Tag weight should be boosted above 1.0
    tag_weight = cal["tag_weights"].get("safety research", 1.0)
    assert tag_weight > 1.0, f"Tag weight should rise above 1.0 after clicks, got {tag_weight}"

    # Source score should be positive
    source_score = cal["source_scores"].get("The Verge AI", 0.0)
    assert source_score > 0.0, f"Source score should be positive after clicks, got {source_score}"
    assert source_score <= 1.0, f"Source score should not exceed 1.0, got {source_score}"

    # No category_weights or source_weights should exist
    assert "category_weights" not in cal, "category_weights should not exist in calibration"
    assert "source_weights" not in cal, "source_weights should not exist in calibration"


# ---------------------------------------------------------------------------
# Test 7: Source affinity decays over runs
# ---------------------------------------------------------------------------

def test_source_affinity_decays_each_run():
    initial_cal = {"source_scores": {"The Verge AI": 0.5}}

    # Run with no new clicks — only decay should apply
    feedback_no_clicks = {"tag_feedback": {}, "source_feedback": {}, "total_items": 0}

    with patch("agents.newsroom_lead._load_calibration", return_value=initial_cal):
        with patch("agents.newsroom_lead._save_calibration"):
            cal = update_calibration_from_feedback(feedback_no_clicks)

    score_after = cal["source_scores"].get("The Verge AI", 0.0)
    assert score_after < 0.5, f"Score should decay below 0.5, got {score_after}"
    assert score_after > 0.4, f"Score should not decay too fast in one run, got {score_after}"


# ---------------------------------------------------------------------------
# Test 8: Diversity slot — missing category gets a bonus story
# ---------------------------------------------------------------------------

def test_diversity_slot_adds_missing_category():
    # Big News dominates; Critical Voices has no stories above min_confidence=6
    # but has one story above diversity_min_quality=4
    stories = (
        [_make_story(f"Big News {i}", "big_news") for i in range(6)] +
        [_make_story(f"Builder {i}", "builder") for i in range(2)] +
        [_make_story(f"Law {i}", "laws_ethics") for i in range(2)] +
        [_make_story("Low Quality Critical", "critical_voices")]
    )

    # Critical Voices story scores 5 (below min_confidence=6 but above diversity_min_quality=4)
    scores = [
        {"id": i, "confidence_score": 7, "reason": "good"}
        for i in range(len(stories) - 1)
    ] + [{"id": len(stories) - 1, "confidence_score": 5, "reason": "marginal"}]

    llm = _mock_llm_select(scores)

    digest_cfg = {
        "max_stories": 8,
        "diversity_slot": True,
        "diversity_min_quality": 4,
        "format": {
            "category_labels": {
                "big_news": "Big News",
                "laws_ethics": "Law & Ethics",
                "builder": "Builder",
                "critical_voices": "Critical Voices",
            },
            "category_order": ["big_news", "builder", "laws_ethics", "critical_voices"],
        },
    }

    with patch("agents.newsroom_lead._load_calibration", return_value={}):
        with patch("agents.newsroom_lead._save_calibration"):
            with patch("agents.newsroom_lead._load_agents_config", return_value={"newsroom_lead_agent": {}}):
                with patch("agents.newsroom_lead._load_digest_config", return_value=digest_cfg):
                    with patch("tools.notion_delivery.write_candidates_log"):
                        with patch("agents.newsroom_lead._cluster_stories", side_effect=_passthrough_cluster):
                            result, _ = select_stories(stories, {}, llm, min_confidence=6)

    result_cats = [s["category"] for s in result]
    assert "critical_voices" in result_cats, (
        "Diversity slot should have added a Critical Voices story even though it scored below min_confidence"
    )
