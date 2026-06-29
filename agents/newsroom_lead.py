"""
agents/newsroom_lead.py

NewsroomLeadAgent: reviews all raw stories, applies feedback calibration,
deduplicates across categories, scores each story, and returns a shortlist
for the EditorAgent.

Feedback model:
- Title click-throughs (⭐) boost the topic TAGS of the clicked story (tag-level affinity)
- Title click-throughs also accumulate a slow-decaying source score (source-level affinity)
- Category composition is structural: equal base budget per category,
  unused slots roll to stronger stories from other categories
- A diversity slot ensures all categories are represented when possible
"""

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config"


def _load_calibration() -> dict:
    path = CONFIG_PATH / "calibration.yaml"
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_calibration(cal: dict) -> None:
    with open(CONFIG_PATH / "calibration.yaml", "w") as f:
        yaml.dump(cal, f, default_flow_style=False, allow_unicode=True)


def _load_digest_config() -> dict:
    with open(CONFIG_PATH / "digest.yaml") as f:
        return yaml.safe_load(f)


def _load_agents_config() -> dict:
    with open(CONFIG_PATH / "agents.yaml") as f:
        return yaml.safe_load(f)


def _category_label(category: str, digest_cfg: dict) -> str:
    return digest_cfg.get("format", {}).get("category_labels", {}).get(
        category, category.replace("_", " ").title()
    )


def _wilson_lower_bound(positive: int, total: int, z: float = 1.96) -> float:
    """Confidence-adjusted approval score. Conservative when sample size is small."""
    if total == 0:
        return 0.5
    p = positive / total
    return (
        p + z**2 / (2 * total)
        - z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total)
    ) / (1 + z**2 / total)


def update_calibration_from_feedback(feedback: dict) -> dict:
    """
    Recompute topic-tag weights and source affinity scores from click-through feedback.

    Tag weights: Wilson score + logarithmic accumulation (sparse data moves slowly).
    Source scores: decay ×0.978/run (≈×0.85/week), increment +0.1 per click, cap 1.0.

    Returns updated calibration dict.
    """
    cal = _load_calibration()
    # Remove legacy category/source weights if present
    cal.pop("category_weights", None)
    cal.pop("source_weights", None)

    # --- Tag affinity (topic-level) ---
    tag_weights = cal.get("tag_weights", {})
    tag_feedback = feedback.get("tag_feedback", {})

    for tag, signals in tag_feedback.items():
        positive = signals.get("top", 0)
        confidence = _wilson_lower_bound(positive, positive)
        boost = math.log1p(positive) * confidence * 0.05
        current = tag_weights.get(tag, 1.0)
        tag_weights[tag] = round(max(0.3, min(2.0, current + boost)), 2)

    cal["tag_weights"] = tag_weights

    # --- Source affinity (source-level, slow-accumulating) ---
    # Decay every run so a source needs sustained clicks to maintain its bonus.
    source_scores = cal.get("source_scores") or {}
    DECAY = 0.978       # ≈ ×0.85 per week when running daily
    INCREMENT = 0.1
    MAX_SCORE = 1.0

    for src in list(source_scores):
        source_scores[src] = round(source_scores[src] * DECAY, 4)
        if source_scores[src] < 0.005:
            del source_scores[src]

    for src, count in feedback.get("source_feedback", {}).items():
        if src:
            source_scores[src] = min(MAX_SCORE, round(source_scores.get(src, 0.0) + count * INCREMENT, 4))

    cal["source_scores"] = source_scores
    cal["last_updated"] = date.today().isoformat()
    cal["feedback_items_read"] = feedback.get("total_items", 0)

    _save_calibration(cal)
    logger.info(f"Calibration updated from {feedback.get('total_items', 0)} feedback items")
    return cal


def select_stories(
    all_stories: list[dict],
    feedback: dict,
    llm,
    min_confidence: int = 6,
    fallback_llm=None,
    tertiary_llm=None,
    published_urls=None,
    rejected_urls=None,
) -> list[dict]:
    """
    Score and select the best stories using the LLM, then apply category
    budget composition and diversity slot.

    Scoring chain (each tier tried only if the previous one raised):
      1. primary llm (Gemini)
      2. fallback_llm (Cerebras)
      3. tertiary_llm (Groq)
      4. equal scoring (last resort — all stories get 6)

    Category composition:
      - Each category gets base_budget = max_stories // 4 slots
      - Unused slots roll to the highest-scoring stories from other categories
      - A diversity slot adds one story from any category not yet represented

    Args:
        all_stories: Combined raw stories from all 4 search agents
        feedback: Feedback dict from notion_delivery.read_feedback()
        llm: Primary LLM instance
        min_confidence: Drop stories below this score
        fallback_llm: Optional second-tier LLM
        tertiary_llm: Optional third-tier LLM
        published_urls: Set of URLs already delivered in previous digests (cross-day dedup)
        rejected_urls: Set of URLs previously dropped for recency (don't re-enter the funnel)

    Returns:
        Ranked list of selected story dicts with 'confidence_score' added
    """
    cal = update_calibration_from_feedback(feedback)
    agents_cfg = _load_agents_config()
    digest_cfg = _load_digest_config()
    lead_cfg = agents_cfg.get("newsroom_lead_agent", {})

    # Inject source scores into digest_cfg as a runtime key (not user-editable config)
    digest_cfg["_source_scores"] = cal.get("source_scores") or {}

    import math
    max_stories = digest_cfg.get("max_stories", 35)
    categories = list(digest_cfg.get("format", {}).get("category_labels", {}).keys())
    big_news_max = digest_cfg.get("big_news_max", max_stories // len(categories))
    base_budget = max(1, (max_stories - big_news_max) // (len(categories) - 1))
    quality_floor = digest_cfg.get("diversity_min_quality", 4)
    per_source_cap = math.ceil(base_budget / 3)
    stage_dropped = {}

    # Add category labels for display
    for s in all_stories:
        s["category_label"] = _category_label(s.get("category", ""), digest_cfg)

    # Flag paywalled stories
    known_paywalls = digest_cfg.get("filters", {}).get("known_paywalls", [])
    for s in all_stories:
        s["paywalled"] = any(domain in s.get("url", "") for domain in known_paywalls)

    # Deduplicate across categories
    seen_urls = set()
    unique_stories = []
    for s in all_stories:
        url = s.get("url", "")
        if url not in seen_urls:
            seen_urls.add(url)
            unique_stories.append(s)
        elif url not in stage_dropped:
            stage_dropped[url] = "url_dedup"

    # Cross-day deduplication: drop URLs already delivered in a previous digest
    if published_urls:
        before = len(unique_stories)
        unique_stories = [s for s in unique_stories if s.get("url", "") not in published_urls]
        for s in all_stories:
            if s.get("url", "") in published_urls and s.get("url", "") not in stage_dropped:
                stage_dropped[s.get("url", "")] = "cross_day_dedup"
        logger.info(f"Cross-day dedup: {before} → {len(unique_stories)} after removing previously-published URLs")

    # Rejected-URL memory: skip URLs previously dropped for recency (last 30 days).
    # Stops evergreen/stale content from re-entering the LLM funnel every run.
    if rejected_urls:
        before_rejected = len(unique_stories)
        unique_stories = [s for s in unique_stories if s.get("url", "") not in rejected_urls]
        for s in all_stories:
            if s.get("url", "") in rejected_urls and s.get("url", "") not in stage_dropped:
                stage_dropped[s.get("url", "")] = "previously_rejected"
        dropped_count = before_rejected - len(unique_stories)
        if dropped_count:
            logger.info(f"Rejected-URL memory: {before_rejected} → {len(unique_stories)} (dropped {dropped_count} previously-stale URLs)")

    # Drop stale articles (>14 days old) from ALL categories — including builder.
    # Undated stories in all categories are also dropped: no date = can't verify freshness.
    # GitHub Trending entries are stamped with the scrape time, so they pass cleanly.
    now = datetime.now(timezone.utc)
    before_recency = len(unique_stories)
    fresh = []
    for s in unique_stories:
        pub = s.get("published_iso")
        if not pub:
            stage_dropped[s.get("url", "")] = "recency_cutoff"
            continue
        # Try strict ISO first, then dateutil for tolerant parsing (handles most Tavily date formats).
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
            # Unparseable date — drop rather than silently keeping.
            stage_dropped[s.get("url", "")] = "recency_cutoff"
            continue
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        age_days = (now - pub_dt).total_seconds() / 86400
        if age_days <= 14:
            fresh.append(s)
        else:
            stage_dropped[s.get("url", "")] = "recency_cutoff"
    unique_stories = fresh
    logger.info(f"Recency cutoff: {before_recency} → {len(unique_stories)} (dropped >{14}d or undated, all categories)")

    # --- Trusted-voice pin (must_include) ---
    # Stories from named individual sources (e.g. Substack newsletters) flagged
    # with priority="must_include" and published within the freshness window are
    # pinned into the digest before the normal funnel runs.  They bypass per-source
    # cap, confidence floor, category budget, and source-diversity rules.  One story
    # per byline (falls back to source name) at most.
    # Window is configurable via must_include_max_hours in digest.yaml (default 48).
    MUST_INCLUDE_HOURS = digest_cfg.get("must_include_max_hours", 48)
    pinned_stories: list[dict] = []
    pinned_bylines: set[str] = set()
    pinned_urls: set[str] = set()
    remaining_stories: list[dict] = []

    for s in unique_stories:
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
        if age_hours > MUST_INCLUDE_HOURS:
            remaining_stories.append(s)
            continue
        # Fresh enough — check byline dedup (1 pinned story per named individual).
        byline_key = s.get("byline") or s.get("source", "")
        if byline_key in pinned_bylines:
            remaining_stories.append(s)
            continue
        # Pin it.
        s["confidence_score"] = 10
        s["selection_reason"] = "trusted voice"
        s["pinned"] = True
        s["llm_rank"] = 0
        pinned_stories.append(s)
        pinned_bylines.add(byline_key)
        pinned_urls.add(s.get("url", ""))

    if pinned_stories:
        logger.info(
            f"Trusted-voice pin: {len(pinned_stories)} story/stories pinned "
            f"({', '.join(s.get('source', '') for s in pinned_stories)})"
        )

    # Exclude pinned URLs from normal pool (no double-counting).
    unique_stories = [s for s in remaining_stories if s.get("url", "") not in pinned_urls]

    # Per-source cap + per-byline cap.
    # Source cap: ceil(base_budget/3) stories per source (prevents feed domination).
    # Byline cap: 2 stories per named author per digest (set via byline field in sources.yaml).
    # If a story has no byline, the byline key falls back to its source name.
    from collections import defaultdict
    _BYLINE_CAP = 2
    logger.info(f"Stories before per-source cap: {len(unique_stories)}")
    source_counts: defaultdict[str, int] = defaultdict(int)
    byline_counts: defaultdict[str, int] = defaultdict(int)
    capped = []
    for s in unique_stories:
        src = s.get("source", "")
        byline_key = s.get("byline") or src
        if source_counts[src] < per_source_cap and byline_counts[byline_key] < _BYLINE_CAP:
            capped.append(s)
            source_counts[src] += 1
            byline_counts[byline_key] += 1
    unique_stories = capped
    logger.info(
        f"Stories after per-source/byline cap: {len(unique_stories)} "
        f"(source cap: {per_source_cap}, byline cap: {_BYLINE_CAP})"
    )

    # --- Topic clustering (C2) ---
    pre_cluster_count = len(unique_stories)
    unique_stories, cluster_sizes = _cluster_stories(unique_stories)
    logger.info(f"Clustered {pre_cluster_count} stories into {len(unique_stories)} topics")

    # --- Heuristic scoring (C1) ---
    tag_weights = cal.get("tag_weights", {})
    sorted_stories = _score_heuristic(unique_stories, tag_weights, cluster_sizes, digest_cfg)
    h_weights = digest_cfg.get("heuristic_weights", {})
    top_n_standard = h_weights.get("llm_rerank_top_n", 12)
    top_n_editorial = h_weights.get("llm_rerank_top_n_editorial", 20)

    # Per-category cutoff: editorial categories get a more generous slot allocation
    # because their sources lack the engagement signals that standard scoring rewards.
    by_cat: dict = {}
    for s in sorted_stories:
        by_cat.setdefault(s.get("category", ""), []).append(s)

    candidates = []
    for cat, cat_stories in by_cat.items():
        top_n = top_n_editorial if cat in _EDITORIAL_CATS else top_n_standard
        candidates.extend(cat_stories[:top_n])
        for s in cat_stories[top_n:]:
            stage_dropped[s.get("url", "")] = "heuristic_cutoff"
    logger.info(
        f"Heuristic: {len(candidates)} candidates selected for LLM rerank "
        f"(from {len(unique_stories)}, standard={top_n_standard}/cat, editorial={top_n_editorial}/cat)"
    )

    # --- LLM pairwise rerank (C1) ---
    feedback_summary = _format_feedback_summary(feedback, cal)
    system_prompt = lead_cfg.get("backstory", "") + "\n\n" + lead_cfg.get("instructions", "")
    filters = digest_cfg.get("filters", {})
    scoring_label = "primary"
    ranked = None

    try:
        ranked = _rerank_with_llm(candidates, llm, feedback_summary, system_prompt, provider="gemini", filters=filters)
    except Exception as e:
        logger.warning(f"Primary LLM rerank failed: {e}")

    if ranked is None and fallback_llm is not None:
        logger.info("Retrying rerank with fallback model")
        try:
            ranked = _rerank_with_llm(candidates, fallback_llm, feedback_summary, system_prompt, provider="cerebras", filters=filters)
            scoring_label = "fallback"
            logger.info("Fallback model rerank succeeded")
        except Exception as e:
            logger.warning(f"Fallback rerank failed: {e}")

    if ranked is None and tertiary_llm is not None:
        logger.info("Retrying rerank with tertiary model (Groq)")
        try:
            ranked = _rerank_with_llm(candidates, tertiary_llm, feedback_summary, system_prompt, provider="groq", filters=filters)
            scoring_label = "tertiary"
            logger.info("Tertiary model rerank succeeded")
        except Exception as e:
            logger.warning(f"Tertiary rerank failed: {e}")

    if ranked is None:
        logger.warning("All rerank tiers failed — using equal scoring")
        ranked = [
            {"id": i, "rank": i + 1, "category": candidates[i].get("category", ""), "reason": "fallback"}
            for i in range(len(candidates))
        ]
        scoring_label = "equal"

    # --- Attach scores and re-categorize (C1, C4) ---
    # Stories not in top N start at 0; candidates get 5–10 based on LLM rank
    for story in unique_stories:
        story["confidence_score"] = 0
        story["selection_reason"] = ""

    valid_categories = set(categories)
    rank_map = {item["id"]: item for item in ranked}
    n_ranked = len(candidates)
    logger.info(f"Scores returned: {len(ranked)}/{n_ranked} stories reranked")

    for i, story in enumerate(candidates):
        rank_info = rank_map.get(i, {"rank": n_ranked, "category": story.get("category", ""), "reason": ""})
        rank_pos = rank_info.get("rank", n_ranked)
        cs = round(10 - (rank_pos - 1) * 5 / max(1, n_ranked - 1))
        story["confidence_score"] = max(5, min(10, cs))
        story["selection_reason"] = rank_info.get("reason", "")
        story["ai_subject"] = rank_info.get("ai_subject", "")
        story["llm_rank"] = rank_pos

        # C4: re-categorize if LLM assigns a valid different category
        new_cat = rank_info.get("category", "")
        if new_cat in valid_categories and new_cat != story.get("category", ""):
            logger.debug(f"Re-categorized '{story.get('title', '')}': {story.get('category')} → {new_cat}")
            story["category"] = new_cat
            story["category_label"] = _category_label(new_cat, digest_cfg)

    # Score distribution — helps diagnose conservative/generous LLM calibration
    dist = {"9-10": 0, "7-8": 0, "5-6": 0, "1-4": 0}
    for s in unique_stories:
        sc = s.get("confidence_score", 0)
        if sc >= 9:
            dist["9-10"] += 1
        elif sc >= 7:
            dist["7-8"] += 1
        elif sc >= 5:
            dist["5-6"] += 1
        else:
            dist["1-4"] += 1
    logger.info(f"Score distribution: 9-10: {dist['9-10']}, 7-8: {dist['7-8']}, 5-6: {dist['5-6']}, 1-4: {dist['1-4']}")

    # Filter to quality floor — guarantees every category can fill its budget
    passing = [s for s in unique_stories if s.get("confidence_score", 0) >= quality_floor]
    for s in unique_stories:
        if s.get("confidence_score", 0) < quality_floor and s.get("url", "") not in stage_dropped:
            stage_dropped[s.get("url", "")] = "confidence_floor"
    for cat in categories:
        cat_count = len([s for s in passing if s.get("category") == cat])
        logger.info(f"  Above floor ({quality_floor}): {cat}: {cat_count} stories")

    # --- Pre-budget semantic dedup (Fix 1, Brief 21) ---
    # Dedup within each category BEFORE the budget loop. Without this, near-duplicate
    # events (e.g. four outlets covering the same EO) consume the per-category cap;
    # semantic dedup then collapses them post-selection and freed slots are never
    # reclaimed. Running per-category keeps each LLM prompt small and focused.
    # The post-selection dedup below still runs as a safety net for pinned-story
    # conflicts and any cross-category duplicates.
    pre_budget_deduped: list[dict] = []
    for cat in categories:
        cat_pool = [s for s in passing if s.get("category") == cat]
        if len(cat_pool) > 1:
            cat_pool = _semantic_dedup(cat_pool, llm, fallback_llm, tertiary_llm)
        pre_budget_deduped.extend(cat_pool)
    passing = pre_budget_deduped

    # --- Category budget composition ---
    # Each category fills up to its category_max hard cap from highest-scored stories.
    # Remaining passing stories pool together; each category may absorb up to
    # rollover_max extra from the pool (in score order) until max_stories is reached.
    category_max_cfg = digest_cfg.get("category_max", {})
    rollover_max = digest_cfg.get("rollover_max", 2)
    default_cap = {cat: (big_news_max if cat == "big_news" else base_budget) for cat in categories}

    selected = []
    leftover_pool = []
    for category in categories:
        cat_stories = sorted(
            [s for s in passing if s.get("category") == category],
            key=lambda x: (x.get("confidence_score", 0), (x.get("published_iso") or "")),
            reverse=True,
        )
        cap = category_max_cfg.get(category, default_cap.get(category, base_budget))
        selected.extend(cat_stories[:cap])
        leftover_pool.extend(cat_stories[cap:])

    # Distribute rollover slots from the combined leftover pool
    leftover_pool.sort(
        key=lambda x: (x.get("confidence_score", 0), (x.get("published_iso") or "")),
        reverse=True,
    )
    rollover_used: dict[str, int] = {cat: 0 for cat in categories}
    rollover_added = []
    for story in leftover_pool:
        if len(selected) + len(rollover_added) >= max_stories:
            break
        cat = story.get("category", "")
        if rollover_used.get(cat, 0) < rollover_max:
            rollover_added.append(story)
            rollover_used[cat] = rollover_used.get(cat, 0) + 1
    selected.extend(rollover_added)
    logger.info(
        "Category selection: "
        + ", ".join(
            f"{c}={len([s for s in selected if s.get('category') == c])}" for c in categories
        )
        + f" ({len(rollover_added)} rollover)"
    )

    # --- Source diversity enforcement ---
    # Within each category, ensure no two stories share the same source when
    # an alternative is available. Prevents a single feed dominating on
    # equal-scoring days where all stories sort by recency (same feed = same source).
    diversity_min = digest_cfg.get("diversity_min_quality", 6)
    final_selected = []
    for category in categories:
        cat_selected = [s for s in selected if s.get("category") == category]
        cat_pool = sorted(
            [s for s in passing if s.get("category") == category and s not in cat_selected],
            key=lambda x: (x.get("confidence_score", 0), (x.get("published_iso") or "")),
            reverse=True,
        )
        seen_sources: set[str] = set()
        diverse: list[dict] = []
        duplicates: list[dict] = []
        for story in cat_selected:
            src = story.get("source", "")
            if src not in seen_sources:
                seen_sources.add(src)
                diverse.append(story)
            else:
                duplicates.append(story)
        # Try to replace duplicate-source stories with alternatives
        for dup in duplicates:
            dup_score = dup.get("confidence_score", 0)
            replacement = next(
                (s for s in cat_pool if s.get("source", "") not in seen_sources
                 and s.get("confidence_score", 0) >= diversity_min),
                None,
            )
            if replacement and (dup_score - replacement.get("confidence_score", 0)) < 2:
                diverse.append(replacement)
                seen_sources.add(replacement.get("source", ""))
                cat_pool = [s for s in cat_pool if s is not replacement]
            else:
                diverse.append(dup)
        final_selected.extend(diverse)

    # Preserve any stories not in a known category (shouldn't happen, but safety)
    known_cats = set(categories)
    final_selected.extend(s for s in selected if s.get("category") not in known_cats)
    selected = final_selected

    # --- Diversity slots (C5) ---
    # One slot per missing category — rescues all absent categories, not just one
    diversity_slot = digest_cfg.get("diversity_slot", True)
    diversity_min = digest_cfg.get("diversity_min_quality", 4)
    if diversity_slot:
        selected_categories = {s.get("category") for s in selected}
        missing = [c for c in categories if c not in selected_categories]
        for missing_cat in missing:
            explore_pool = [
                s for s in unique_stories
                if s.get("category") == missing_cat
                and s.get("confidence_score", 0) >= diversity_min
            ]
            if explore_pool:
                best = max(explore_pool, key=lambda x: x.get("confidence_score", 0))
                selected.append(best)
                logger.info(f"Diversity slot: added '{best.get('title', '')}' ({best.get('category')})")

    selected.sort(key=lambda x: x.get("confidence_score", 0), reverse=True)

    # Merge pinned trusted-voice stories into the selection pool before semantic dedup.
    # They enter at the front so that in any semantic cluster, a pinned story wins.
    if pinned_stories:
        selected = pinned_stories + [s for s in selected if s.get("url", "") not in pinned_urls]

    # --- Selection-time semantic dedup (C6) ---
    # URL dedup and TF-IDF clustering above catch exact/near-exact duplicates.
    # This pass uses the LLM to catch cross-outlet stories about the same event
    # (same launch, paper, ruling, or incident — not merely the same topic).
    # Within each cluster, prefer the non-paywalled story if scores are within 1pt.
    # Pinned stories are protected: a pinned story always wins its cluster.
    if len(selected) > 1:
        selected = _semantic_dedup(selected, llm, fallback_llm, tertiary_llm)

    logger.info(f"NewsroomLead: selected {len(selected)} stories from {len(unique_stories)} unique")

    # Write candidate audit log to Notion (skipped silently if env var not set)
    from tools.notion_delivery import write_candidates_log
    selected_urls = {s.get("url") for s in selected}
    for s in all_stories:
        url = s.get("url", "")
        if url not in stage_dropped and url not in selected_urls:
            stage_dropped[url] = "not_selected"
    write_candidates_log(all_stories, selected_urls, date.today().isoformat(), stage_dropped)

    return selected, scoring_label


def _format_feedback_summary(feedback: dict, cal: dict) -> str:
    """Format tag weight and source affinity data as readable text for the LLM prompt."""
    if not feedback or not feedback.get("total_items"):
        return "No feedback data yet — score stories purely on newsworthiness and quality."

    tag_weights = cal.get("tag_weights", {})
    boosted = [(tag, w) for tag, w in tag_weights.items() if w > 1.05]
    suppressed = [(tag, w) for tag, w in tag_weights.items() if w < 0.95]

    lines = [f"Based on {feedback['total_items']} click-throughs:"]
    for tag, w in sorted(boosted, key=lambda x: x[1], reverse=True)[:5]:
        lines.append(f"  ↑ '{tag}': reader clicks these stories more (weight {w})")
    for tag, w in sorted(suppressed, key=lambda x: x[1])[:5]:
        lines.append(f"  ↓ '{tag}': reader rarely clicks these (weight {w})")
    if not boosted and not suppressed:
        lines.append("  All topics currently at neutral weight — score on quality alone.")

    # Surface high-affinity sources so the LLM can factor them in
    source_scores = cal.get("source_scores") or {}
    notable_sources = [(src, score) for src, score in source_scores.items() if score > 0.2]
    if notable_sources:
        lines.append("  Preferred sources (from click history):")
        for src, score in sorted(notable_sources, key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"    · {src} (affinity {score:.2f})")

    return "\n".join(lines)


def _cluster_stories(stories, threshold=0.35):
    """Cluster stories by title similarity (TF-IDF + cosine). One representative per cluster.

    Returns (representatives, cluster_sizes) where cluster_sizes maps URL → cluster size.
    Cluster size feeds into the heuristic trending bonus: if 5 outlets cover the same
    story, that IS the news.
    """
    if len(stories) < 2:
        return list(stories), {s.get("url", ""): 1 for s in stories}

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as sk_cosine_similarity
    except ImportError:
        logger.warning("scikit-learn not installed — skipping topic clustering")
        return list(stories), {s.get("url", ""): 1 for s in stories}

    titles = [s.get("title", "") for s in stories]
    try:
        vectorizer = TfidfVectorizer(stop_words="english", min_df=1)
        tfidf_matrix = vectorizer.fit_transform(titles)
    except ValueError:
        return list(stories), {s.get("url", ""): 1 for s in stories}

    sim_matrix = sk_cosine_similarity(tfidf_matrix)
    n = len(stories)
    assigned = [False] * n
    representatives = []
    cluster_sizes = {}

    for i in range(n):
        if assigned[i]:
            continue
        cluster_indices = [i]
        for j in range(i + 1, n):
            if not assigned[j] and sim_matrix[i][j] >= threshold:
                cluster_indices.append(j)
                assigned[j] = True
        assigned[i] = True
        rep_idx = max(cluster_indices, key=lambda k: stories[k].get("published_iso") or "")
        rep = stories[rep_idx]
        representatives.append(rep)
        cluster_sizes[rep.get("url", "")] = len(cluster_indices)

    return representatives, cluster_sizes


def _semantic_dedup(
    stories: list[dict],
    llm,
    fallback_llm=None,
    tertiary_llm=None,
) -> list[dict]:
    """
    LLM-assisted dedup: group selected stories by same underlying event
    (same launch, paper, ruling, or incident — not just the same topic area).
    Within each cluster of size >1, keep one representative:
      - Prefer non-paywalled if scores are within 1 point.
      - Otherwise: highest confidence_score, then most recent published_iso.
    Logs cluster decisions to logs/dedup.log.
    """
    from pathlib import Path as _Path
    from datetime import datetime as _datetime

    dedup_log = _Path(__file__).parent.parent / "logs" / "dedup.log"
    dedup_log.parent.mkdir(parents=True, exist_ok=True)

    def _log_dedup(msg: str) -> None:
        with open(dedup_log, "a") as f:
            f.write(f"[{_datetime.now().isoformat(timespec='seconds')}] {msg}\n")

    story_list = "\n".join(
        f"{i}: {s.get('title', '')} ({s.get('source', '')})"
        for i, s in enumerate(stories)
    )
    prompt = (
        "Below is a list of AI news stories. "
        "Group them by whether they report the SAME underlying event — the same launch, "
        "paper, court ruling, or incident. Not just the same topic area. "
        "Return ONLY a JSON array of arrays of integer indices. "
        "Singleton stories (no duplicate) must still appear as single-element arrays. "
        "Example: [[0], [1, 4], [2], [3, 7, 9], [5], [6], [8]]\n\n"
        f"{story_list}"
    )

    raw = None
    for client in (llm, fallback_llm, tertiary_llm):
        if client is None:
            continue
        try:
            raw = client.call([{"role": "user", "content": prompt}])
            break
        except Exception as e:
            logger.debug(f"Semantic dedup LLM call failed: {e}")

    if raw is None:
        logger.warning("Semantic dedup: all LLM tiers failed — skipping")
        return stories

    import json as _json, re as _re
    match = _re.search(r"\[.*\]", raw, _re.DOTALL)
    if not match:
        logger.warning("Semantic dedup: could not parse LLM response — skipping")
        return stories

    try:
        groups: list[list[int]] = _json.loads(match.group())
    except Exception:
        logger.warning("Semantic dedup: JSON parse error — skipping")
        return stories

    # Validate: all indices in range, no story appears twice
    all_seen: set[int] = set()
    valid = True
    for grp in groups:
        for idx in grp:
            if not isinstance(idx, int) or idx < 0 or idx >= len(stories):
                valid = False
                break
            if idx in all_seen:
                valid = False
                break
            all_seen.add(idx)
        if not valid:
            break
    if not valid or len(all_seen) != len(stories):
        logger.warning("Semantic dedup: LLM returned invalid index groups — skipping")
        return stories

    kept: list[dict] = []
    for grp in groups:
        if len(grp) == 1:
            kept.append(stories[grp[0]])
            continue

        cluster = [stories[i] for i in grp]
        _log_dedup(
            f"CLUSTER: "
            + " | ".join(f"[{i}] {s.get('title','')[:60]} ({s.get('source','')})" for i, s in zip(grp, cluster))
        )

        # Pick representative: pinned trusted-voice stories always win their cluster;
        # then prefer non-paywalled over paywalled within 1-pt score gap.
        pinned_in_cluster = [s for s in cluster if s.get("pinned")]
        if pinned_in_cluster:
            best = pinned_in_cluster[0]
        else:
            best = cluster[0]
        for candidate in cluster[1:]:
            best_score = best.get("confidence_score", 0)
            cand_score = candidate.get("confidence_score", 0)
            best_paywalled = best.get("paywalled", False)
            cand_paywalled = candidate.get("paywalled", False)

            # Non-paywalled wins over paywalled if score within 1pt
            if best_paywalled and not cand_paywalled and (best_score - cand_score) <= 1:
                best = candidate
            elif not best_paywalled and cand_paywalled:
                pass  # keep current best (already non-paywalled)
            elif cand_score > best_score:
                best = candidate
            elif cand_score == best_score:
                # Tiebreak: more recent
                if (candidate.get("published_iso") or "") > (best.get("published_iso") or ""):
                    best = candidate

        dropped = [s for s in cluster if s is not best]
        _log_dedup(
            f"  KEPT: {best.get('title','')[:60]} ({best.get('source','')})"
            + (f" [paywall={'yes' if best.get('paywalled') else 'no'}]")
        )
        for d in dropped:
            _log_dedup(f"  DROP: {d.get('title','')[:60]} ({d.get('source','')})")
        kept.append(best)

    logger.info(f"Semantic dedup: {len(stories)} → {len(kept)} stories")
    return kept


_EDITORIAL_CATS = {"critical_voices", "laws_ethics", "big_news", "builder"}


def _score_heuristic(stories, tag_weights, cluster_sizes, digest_cfg):
    """Score all candidates with deterministic signals — no LLM, no cost.

    Editorial categories (critical_voices, laws_ethics) use recency + tag affinity
    + source bonus only — engagement and authority signals are omitted because small
    publications won't have HN points or high source weights.
    All other categories use the full formula.
    Returns stories sorted by heuristic_score descending (in-place field added).
    Blocked topics are assigned score -999 and filtered out before sorting.
    """
    weights = digest_cfg.get("heuristic_weights", {})
    engagement_w = weights.get("engagement_weight", 1.0)
    recency_24h = weights.get("recency_bonus_24h", 1.0)
    recency_48h = weights.get("recency_bonus_48h", 0.5)
    tag_w = weights.get("tag_affinity_weight", 1.0)

    # Content filters (user-editable in digest.yaml)
    filters = digest_cfg.get("filters", {})
    blocked_kw = [t.lower() for t in filters.get("blocked_topics", [])]
    blocked_domains = [d.lower() for d in filters.get("blocked_domains", [])]
    deprioritized_sources = set(filters.get("deprioritized_sources", []))

    # Source affinity scores (runtime key injected by select_stories)
    source_scores = digest_cfg.get("_source_scores", {})

    now = datetime.now(timezone.utc)
    for story in stories:
        # Block matching domains before scoring
        if blocked_domains:
            url = (story.get("url") or "").lower()
            if any(d in url for d in blocked_domains):
                story["heuristic_score"] = -999
                continue

        # Block matching topics before scoring
        if blocked_kw:
            combined = (
                (story.get("title") or "") + " " + (story.get("description") or "")
            ).lower()
            if any(kw in combined for kw in blocked_kw):
                story["heuristic_score"] = -999
                continue

        recency = 0.0
        pub = story.get("published_iso")
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                age_hours = (now - pub_dt).total_seconds() / 3600
                if age_hours < 24:
                    recency = recency_24h
                elif age_hours < 48:
                    recency = recency_48h
            except (ValueError, TypeError):
                pass

        affinity = sum(tag_weights.get(tag, 0.0) for tag in story.get("tags", []))
        source_bonus = min(0.5, source_scores.get(story.get("source", ""), 0.0))

        if story.get("category") in _EDITORIAL_CATS:
            score = recency + affinity * tag_w + source_bonus
        else:
            authority = story.get("source_weight", 1.0)
            eng = 0.0
            if story.get("hn_points"):
                eng += story["hn_points"] / 100
            if story.get("reddit_upvotes"):
                eng += story["reddit_upvotes"] / 200
            if story.get("github_stars"):
                eng += story["github_stars"] / 500
            cluster_bonus = math.log(max(1, cluster_sizes.get(story.get("url", ""), 1))) * 0.5
            score = (
                authority
                + eng * engagement_w
                + recency
                + affinity * tag_w
                + cluster_bonus
                + source_bonus
            )

        # Deprioritized sources get a penalty (not blocked entirely)
        if story.get("source", "") in deprioritized_sources:
            score -= 2.0

        story["heuristic_score"] = score

    # Remove blocked stories before sorting
    active = [s for s in stories if s.get("heuristic_score", 0) > -900]
    return sorted(active, key=lambda x: x.get("heuristic_score", 0), reverse=True)


def _rerank_with_llm(candidates, llm, feedback_summary, system_prompt, provider="unknown",
                     filters=None):
    """Rank top candidates via a single LLM call (pairwise-style relative ranking).

    More reliable than independent 1–10 scoring: the LLM is comparing pre-filtered
    strong candidates, not deciding what's a 5 vs a 6.

    Returns list of dicts [{id, rank, category, reason}] ordered best-first.
    """
    import re

    filters = filters or {}
    blocked_topics = filters.get("blocked_topics", [])
    deprioritized_sources = filters.get("deprioritized_sources", [])

    stories_json = json.dumps(
        [
            {
                "id": i,
                "title": s.get("title", ""),
                "source": s.get("source", ""),
                "category": s.get("category", ""),
                "published_iso": s.get("published_iso", ""),
                "description": (s.get("description") or "")[:150],
            }
            for i, s in enumerate(candidates)
        ],
        indent=2,
    )

    filter_block = ""
    if blocked_topics or deprioritized_sources:
        filter_lines = ["CONTENT FILTERS (rank these lowest or exclude):"]
        if blocked_topics:
            filter_lines.append(f"  - Blocked topics/keywords: {', '.join(blocked_topics)}")
        if deprioritized_sources:
            filter_lines.append(f"  - Deprioritized sources: {', '.join(deprioritized_sources)}")
        filter_block = "\n".join(filter_lines) + "\n\n"

    user_prompt = f"""You are the newsroom editor for Broadsheet, a daily AI news digest.

TOPIC PREFERENCES (from last 7 days of reader click-throughs):
{feedback_summary}

{filter_block}CANDIDATE STORIES ({len(candidates)} total):
{stories_json}

Task: Rank these stories by importance and reader interest, best first.
Each story includes a published_iso date — prefer fresher stories when quality is otherwise equal.
Exclude any job postings, hiring announcements or recruitment content even if not matched by keyword filters.
Also assign each story its most accurate category: big_news, laws_ethics, builder, critical_voices.

Return ONLY a JSON array ordered best-first. Provide a reason only for the top 10 and bottom 5.
For every story, write one sentence in ai_subject naming the specific AI event, model, paper,
regulation, or release that is the SUBJECT of the story (not what it implies):
[
  {{"id": <int>, "rank": <int>, "category": "<category_key>", "reason": "<one line or empty string>", "ai_subject": "<one sentence naming the AI subject>"}},
  ...
]

Include every story ID exactly once.
"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response = llm.call(messages)

    try:
        from tools.token_tracker import log_call
        used = log_call(provider, system_prompt + user_prompt, response)
        logger.debug(f"Token tracker: {provider} ~{used:,} tokens this call")
    except Exception:
        pass  # token tracking is best-effort

    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        raise ValueError("No JSON array in LLM rerank response")
    return json.loads(match.group())
