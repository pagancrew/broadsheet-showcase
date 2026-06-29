"""
agents/editor.py

EditorAgent: writes the final digest.
For each selected story, writes one paragraph (60-85 words) that tells
the reader what happened and why it matters.
QA-drops stories whose ai_subject describes an implication rather than
a subject; logs drops to logs/editorial_drops.log.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config"


def _load_agents_config() -> dict:
    with open(CONFIG_PATH / "agents.yaml") as f:
        return yaml.safe_load(f)


def _load_digest_config() -> dict:
    with open(CONFIG_PATH / "digest.yaml") as f:
        return yaml.safe_load(f)


def write_digest(
    selected_stories: list[dict],
    llm,
    fallback_model_used: bool = False,
    alerts_text: str = "",
    fallback_llm=None,
    tertiary_llm=None,
) -> tuple[list[dict], str, int]:
    """
    Write one-paragraph summaries for each selected story.

    Args:
        selected_stories: Ranked list from newsroom_lead.select_stories()
        llm: Primary LLM instance
        fallback_model_used: Flag to include model fallback notice
        alerts_text: Source alert text (included in digest structure)
        fallback_llm: Secondary LLM to try if primary fails
        tertiary_llm: Third-tier LLM to try if both primary and fallback fail

    Returns:
        (stories, editor_label, drop_count):
          - stories: story dicts with 'summary' field added, ready for delivery
          - editor_label: which LLM tier produced summaries ("primary"/"fallback"/"tertiary")
          - drop_count: how many stories the editor QA pass dropped
    """
    agents_cfg = _load_agents_config()
    editor_cfg = agents_cfg.get("editor_agent", {})
    digest_cfg = _load_digest_config()

    system_prompt = editor_cfg.get("backstory", "") + "\n\n" + editor_cfg.get("instructions", "")

    # Build the story list for the LLM
    stories_input = [
        {
            "id": i,
            "title": s.get("title", ""),
            "url": s.get("url", ""),
            "source": s.get("source", ""),
            "category": s.get("category_label", s.get("category", "")),
            "description": s.get("description", ""),
            "confidence_score": s.get("confidence_score", 0),
            "ai_subject": s.get("ai_subject", ""),
            "selection_reason": s.get("selection_reason", ""),
            "score_or_stars": s.get("score") or s.get("stars", ""),
        }
        for i, s in enumerate(selected_stories)
    ]

    user_prompt = f"""
QA PASS FIRST: Before writing, review each story's ai_subject field.
If ai_subject describes an implication rather than a subject — e.g.
"AI is mentioned as one of several economic distractions" — drop the story.
List dropped story IDs in the editorial_drops array with a one-line reason.

Then write one clear, readable paragraph for each story that passes QA.
Each paragraph should be 60-85 words. Lead with the most important fact.
Include the source name naturally. End with why this matters to someone
following AI closely.

Your editorial voice: direct, intelligent, occasionally dry. No hype.
No filler. Write for a smart reader who doesn't have much time.

Also assign 2-3 short topic tags to each story describing the story TYPE —
not the source name or category. Good examples: "model release", "AI regulation",
"open-source release", "benchmark result", "ethics critique", "funding round",
"developer tool", "safety research", "policy proposal".

{"⚠ NOTE: Today's digest used the fallback model." if fallback_model_used else ""}

STORIES:
{json.dumps(stories_input, indent=2)}

Return ONLY a JSON object with this exact structure:
{{
  "summaries": [
    {{
      "id": <int>,
      "summary": "<your one paragraph, 60-85 words>",
      "tags": ["<tag1>", "<tag2>"]
    }},
    ...
  ],
  "editorial_drops": [
    {{
      "id": <int>,
      "reason": "<one-line reason for drop>"
    }},
    ...
  ]
}}

Write a summary for every story that passes QA (scores 6+, valid ai_subject).
The editorial_drops array may be empty if all stories pass.
"""

    summaries = None
    editorial_drops_raw: list[dict] = []
    editor_label = "primary"
    for attempt_llm, label in [
        (llm, "primary"),
        (fallback_llm, "fallback"),
        (tertiary_llm, "tertiary"),
    ]:
        if attempt_llm is None:
            continue
        try:
            response = attempt_llm.call([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])
            summaries, editorial_drops_raw = _parse_summaries(response)
            editor_label = label
            break
        except Exception as e:
            logger.warning(f"Editor LLM ({label}) failed: {e}")

    editorial_drops: list[dict] = editorial_drops_raw

    if summaries is None:
        logger.error("Editor LLM failed on all tiers — using raw descriptions")
        summaries = [
            {"id": i, "summary": s.get("description", "No summary available.")}
            for i, s in enumerate(selected_stories)
        ]
        editor_label = "raw descriptions (all tiers failed)"

    # Map summaries and tags back to stories; skip editorial drops
    dropped_ids = {d["id"] for d in editorial_drops}
    summary_map = {item["id"]: item for item in summaries}
    result = []
    for i, story in enumerate(selected_stories):
        if i in dropped_ids:
            continue
        story = dict(story)  # don't mutate original
        item = summary_map.get(i, {})
        story["summary"] = item.get("summary", story.get("description", ""))
        story["tags"] = item.get("tags", [])
        result.append(story)

    if editorial_drops:
        _write_editorial_drops(editorial_drops, selected_stories)

    # Apply category order from config
    result = _sort_by_category_order(result, digest_cfg)

    logger.info(f"Editor: wrote summaries for {len(result)} stories")
    return result, editor_label, len(editorial_drops)


def _sort_by_category_order(stories: list[dict], digest_cfg: dict) -> list[dict]:
    """Sort stories by the category_order defined in digest.yaml."""
    order = digest_cfg.get("format", {}).get("category_order", [])
    if not order:
        return stories

    def sort_key(s):
        cat = s.get("category", "")
        try:
            return order.index(cat)
        except ValueError:
            return len(order)  # unknown categories go last

    return sorted(stories, key=sort_key)


def _parse_summaries(response: str) -> tuple[list[dict], list[dict]]:
    """
    Extract summaries and editorial_drops from LLM response.

    Accepts both the new object format {"summaries": [...], "editorial_drops": [...]}
    and the legacy array format [...] for backwards-compatibility.

    Returns (summaries_list, drops_list).
    """
    import re
    # Try object format first
    obj_match = re.search(r"\{.*\}", response, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group())
            if "summaries" in parsed:
                return parsed["summaries"], parsed.get("editorial_drops", [])
        except (json.JSONDecodeError, KeyError):
            pass
    # Fall back to legacy array format
    arr_match = re.search(r"\[.*\]", response, re.DOTALL)
    if not arr_match:
        raise ValueError("No JSON found in editor LLM response")
    return json.loads(arr_match.group()), []


_EDITORIAL_DROPS_LOG = Path(__file__).parent.parent / "logs" / "editorial_drops.log"


def _write_editorial_drops(drops: list[dict], stories: list[dict]) -> None:
    """Log editorial drops to logs/editorial_drops.log (no email footer)."""
    _EDITORIAL_DROPS_LOG.parent.mkdir(parents=True, exist_ok=True)
    id_to_title = {i: s.get("title", f"story {i}") for i, s in enumerate(stories)}
    ts = datetime.now().isoformat(timespec="seconds")
    with open(_EDITORIAL_DROPS_LOG, "a") as f:
        for drop in drops:
            sid = drop.get("id", "?")
            title = id_to_title.get(sid, f"id={sid}")
            reason = drop.get("reason", "no reason given")
            f.write(f"[{ts}] DROP id={sid} | {title!r} | {reason}\n")
    logger.info(f"Editor QA: dropped {len(drops)} stories — see logs/editorial_drops.log")
