"""
tools/notion_delivery.py

Creates story rows in the Notion database and reads feedback.

Each story = one database row. Properties match the schema in README.md.
Feedback is read by NewsroomLeadAgent to calibrate story selection.

Notion API docs: https://developers.notion.com
Free, no rate limit issues for personal use (3 req/sec).
"""

import logging
import os
import re
import requests
from datetime import date, timedelta
from pathlib import Path

from tools.source_monitor import record_failure, record_success

logger = logging.getLogger(__name__)
SOURCE_NAME = "Notion API"

# Feedback property values (must match your Notion select options exactly)
FEEDBACK_MORE = "👍 More like this"
FEEDBACK_LESS = "👎 Less of this"
FEEDBACK_TOP = "⭐ Top pick"


def _get_client():
    """Return an authenticated Notion client."""
    try:
        from notion_client import Client
    except ImportError:
        raise ImportError("notion-client not installed. Run: pip install notion-client")

    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        raise EnvironmentError("NOTION_API_KEY not set in environment")
    return Client(auth=api_key)


def _get_database_id() -> str:
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not db_id:
        raise EnvironmentError("NOTION_DATABASE_ID not set in environment")
    return db_id


def create_story_row(story: dict) -> str | bool:
    """
    Create one row in the Notion database for a story.

    Args:
        story: Dict with keys: title, url, category, summary, source,
               published_iso, confidence_score

    Returns:
        Notion page ID string if created successfully, False otherwise
    """
    try:
        notion = _get_client()
        db_id = _get_database_id()

        # Build Notion property payload
        properties = {
            "Title": {
                "title": [{"text": {"content": story.get("title", "Untitled")[:2000]}}]
            },
            "URL": {"url": story.get("url") or None},
            "Category": {
                "select": {"name": story.get("category_label", story.get("category", ""))}
            },
            "Source": {
                "rich_text": [{"text": {"content": story.get("source", "")[:200]}}]
            },
            "Summary": {
                "rich_text": [{"text": {"content": story.get("summary", "")[:2000]}}]
            },
            "Date": {
                "date": {"start": date.today().isoformat()}
            },
            "Confidence": {
                "number": story.get("confidence_score")
            },
            "Feedback": {
                "select": None  # blank by default — user fills this in
            },
        }

        # Remove None values Notion won't accept
        if properties["URL"]["url"] is None:
            del properties["URL"]
        if properties["Confidence"]["number"] is None:
            del properties["Confidence"]
        properties["Feedback"] = {}  # blank select — omit entirely
        del properties["Feedback"]

        tags = [t.strip() for t in story.get("tags", []) if t.strip()]
        if tags:
            properties["Tags"] = {
                "multi_select": [{"name": t[:100]} for t in tags[:5]]
            }

        try:
            page = notion.pages.create(parent={"database_id": db_id}, properties=properties)
        except Exception as e:
            if tags and ("Tags" in str(e) or "multi_select" in str(e)):
                logger.warning(
                    "Notion 'Tags' property missing — add a multi_select 'Tags' property "
                    "to your database to enable topic-affinity feedback. Creating row without tags."
                )
                properties.pop("Tags", None)
                page = notion.pages.create(parent={"database_id": db_id}, properties=properties)
            else:
                raise

        record_success(SOURCE_NAME)
        return page["id"]

    except Exception as e:
        record_failure(SOURCE_NAME, str(e))
        logger.error(f"Failed to create Notion row for '{story.get('title')}': {e}")
        return False


def publish_digest(stories: list[dict], alerts_text: str = "") -> int:
    """
    Write all stories to Notion. Returns count of successfully created rows.

    Args:
        stories: List of story dicts (with 'summary' written by EditorAgent)
        alerts_text: Source alert text to prepend as a header row (optional)

    Returns:
        Number of rows created
    """
    if alerts_text:
        # Create a header row for alerts
        try:
            notion = _get_client()
            db_id = _get_database_id()
            notion.pages.create(
                parent={"database_id": db_id},
                properties={
                    "Title": {
                        "title": [{"text": {"content": f"⚠ Source Alerts — {date.today().isoformat()}"}}]
                    },
                    "Summary": {
                        "rich_text": [{"text": {"content": alerts_text[:2000]}}]
                    },
                    "Category": {"select": {"name": "Admin"}},
                    "Date": {"date": {"start": date.today().isoformat()}},
                },
            )
        except Exception as e:
            logger.warning(f"Failed to create alerts row: {e}")

    created = 0
    for story in stories:
        page_id = create_story_row(story)
        if page_id:
            story["notion_page_id"] = page_id
            created += 1

    logger.info(f"Notion: created {created}/{len(stories)} story rows")
    return created


def read_feedback(days_back: int = 7) -> dict:
    """
    Read feedback from Notion stories over the last N days.

    Returns a calibration dict:
        {
            "tag_feedback": {"AI regulation": {"more": 2, "less": 1, "top": 0}, ...},
            "total_items": 42,
        }

    Feedback signals are attributed to the story's topic tags, not its category or source.
    Stories without tags (created before tagging was introduced) are skipped.
    """
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not api_key or not db_id:
        logger.warning("Cannot read Notion feedback: NOTION_API_KEY or NOTION_DATABASE_ID not set")
        return {}

    from_date = (date.today() - timedelta(days=days_back)).isoformat()

    try:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={
                "filter": {
                    "and": [
                        {"property": "Date", "date": {"on_or_after": from_date}},
                        {"property": "Feedback", "select": {"is_not_empty": True}},
                    ]
                }
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        record_success(SOURCE_NAME)
    except Exception as e:
        record_failure(SOURCE_NAME, str(e))
        logger.warning(f"Failed to query Notion feedback: {e}")
        return {}

    tag_feedback: dict[str, dict[str, int]] = {}

    for page in results:
        props = page.get("properties", {})

        feedback_val = (
            props.get("Feedback", {}).get("select") or {}
        ).get("name", "")

        signal = _feedback_signal(feedback_val)
        if not signal:
            continue

        tags = [
            t.get("name", "")
            for t in props.get("Tags", {}).get("multi_select", [])
            if t.get("name")
        ]
        for tag in tags:
            entry = tag_feedback.setdefault(tag, {"more": 0, "less": 0, "top": 0})
            entry[signal] += 1

    tagged_items = sum(1 for p in results if p.get("properties", {}).get("Tags", {}).get("multi_select"))
    logger.info(f"Feedback: {len(results)} rated stories, {tagged_items} with tags")

    return {
        "tag_feedback": tag_feedback,
        "total_items": len(results),
    }


def read_published_urls(days_back: int = 7) -> set:
    """
    Return the set of story URLs already published to Notion in the last N days.

    Used for cross-day deduplication: any candidate URL in this set was already
    delivered in a previous digest and should be dropped before scoring.

    Returns an empty set on any error so the caller degrades gracefully.
    """
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not api_key or not db_id:
        return set()

    from_date = (date.today() - timedelta(days=days_back)).isoformat()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "filter": {"property": "Date", "date": {"on_or_after": from_date}},
        "page_size": 100,
    }

    urls: set = set()
    try:
        while True:
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{db_id}/query",
                headers=headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            for page in body.get("results", []):
                url = page.get("properties", {}).get("URL", {}).get("url")
                if url:
                    urls.add(url)
            if body.get("has_more"):
                payload["start_cursor"] = body["next_cursor"]
            else:
                break
    except Exception as e:
        logger.warning(f"Failed to query Notion for published URLs: {e}")
        return set()

    logger.info(f"Cross-day dedup: {len(urls)} previously-published URLs (last {days_back} days)")
    return urls


def read_rejected_urls(days_back: int = 30, reasons: list[str] | None = None) -> set:
    """
    Return the set of candidate URLs that were previously dropped for age (or other
    specified reasons) in the last N days.  Queried from the Notion Candidates database.

    Avoids re-spending gather and LLM-rerank tokens on the same evergreen/stale URLs
    every run.  Only ``recency_cutoff`` is remembered by default — confidence_floor
    and not_selected are excluded because a story's relevance can change day-to-day.

    Returns an empty set on any error so the caller degrades gracefully.
    """
    if reasons is None:
        reasons = ["recency_cutoff"]

    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_CANDIDATES_DATABASE_ID")
    if not api_key or not db_id:
        return set()

    from_date = (date.today() - timedelta(days=days_back)).isoformat()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    # Build OR filter across requested drop reasons
    reason_filters = [
        {"property": "Stage Dropped", "rich_text": {"contains": r}}
        for r in reasons
    ]
    date_filter = {"property": "Run Date", "date": {"on_or_after": from_date}}

    if len(reason_filters) == 1:
        payload = {
            "filter": {"and": [date_filter, reason_filters[0]]},
            "page_size": 100,
        }
    else:
        payload = {
            "filter": {"and": [date_filter, {"or": reason_filters}]},
            "page_size": 100,
        }

    urls: set = set()
    try:
        while True:
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{db_id}/query",
                headers=headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            for page in body.get("results", []):
                url_prop = page.get("properties", {}).get("URL", {})
                url = url_prop.get("url") or (
                    url_prop.get("rich_text", [{}])[0].get("plain_text", "")
                    if url_prop.get("rich_text") else ""
                )
                if url:
                    urls.add(url)
            if body.get("has_more"):
                payload["start_cursor"] = body["next_cursor"]
            else:
                break
    except Exception as e:
        logger.warning(f"Failed to query Notion for rejected URLs: {e}")
        return set()

    logger.info(
        f"Rejected-URL memory: {len(urls)} previously-dropped URLs "
        f"(last {days_back}d, reasons={reasons})"
    )
    return urls


def read_multi_feedback(days_back: int = 7) -> dict:
    """
    Read per-subscriber click-through votes (v=top) from the Votes database
    over the last N days.

    Aggregates across all subscribers by tag and by source. Returns:
        {
            "tag_feedback": {"AI regulation": {"more": 0, "less": 0, "top": N}, ...},
            "source_feedback": {"TechCrunch AI": N, ...},
            "total_items": N,
        }

    Returns {} gracefully if NOTION_VOTES_DATABASE_ID is unset (backwards compat).
    """
    api_key = os.environ.get("NOTION_API_KEY")
    votes_db_id = os.environ.get("NOTION_VOTES_DATABASE_ID")
    if not api_key or not votes_db_id:
        logger.info("NOTION_VOTES_DATABASE_ID not set — skipping multi-subscriber feedback")
        return {}

    from_date = (date.today() - timedelta(days=days_back)).isoformat()

    try:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{votes_db_id}/query",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={
                "filter": {
                    "and": [
                        {"property": "Voted At", "date": {"on_or_after": from_date}},
                        {"property": "Vote", "select": {"equals": "top"}},
                    ]
                }
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        record_success(SOURCE_NAME)
    except Exception as e:
        record_failure(SOURCE_NAME, str(e))
        logger.warning(f"Failed to query Votes database: {e}")
        return {}

    tag_feedback: dict[str, dict[str, int]] = {}
    source_feedback: dict[str, int] = {}

    for row in results:
        props = row.get("properties", {})

        # Tag affinity — attribute click to story's topic tags
        tags = [
            t.get("name", "")
            for t in props.get("Story Tags", {}).get("multi_select", [])
            if t.get("name")
        ]
        for tag in tags:
            entry = tag_feedback.setdefault(tag, {"more": 0, "less": 0, "top": 0})
            entry["top"] += 1

        # Source affinity — attribute click to story's source
        source_items = props.get("Story Source", {}).get("rich_text", [])
        source_name = source_items[0].get("plain_text", "") if source_items else ""
        if source_name:
            source_feedback[source_name] = source_feedback.get(source_name, 0) + 1

    logger.info(f"Multi-subscriber feedback: {len(results)} top-click votes from Votes database")
    return {
        "tag_feedback": tag_feedback,
        "source_feedback": source_feedback,
        "total_items": len(results),
    }


def write_candidates_log(all_stories: list[dict], selected_set: set, run_date: str, stage_dropped: dict) -> None:
    """
    Write today's heuristic top-N candidates to the Notion Candidates database.

    Each row shows: title, source, category, heuristic score, LLM rank,
    confidence score, tags, whether selected, and the LLM's selection reason.
    Rows are purged daily at the start of each run.

    Skipped silently if NOTION_CANDIDATES_DATABASE_ID is not set.
    """
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_CANDIDATES_DATABASE_ID")
    if not api_key or not db_id:
        logger.info("NOTION_CANDIDATES_DATABASE_ID not set — skipping candidate log")
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    # Clear all existing rows
    has_more = True
    next_cursor = None
    while has_more:
        body = {"page_size": 100}
        if next_cursor:
            body["start_cursor"] = next_cursor
        try:
            resp = requests.post(
                f"https://api.notion.com/v1/databases/{db_id}/query",
                headers=headers, json=body, timeout=15,
            )
            if not resp.ok:
                break
            data = resp.json()
            for row in data.get("results", []):
                props = row.get("properties", {})
                save_flag = props.get("Save for Review", {}).get("checkbox", False)
                if not save_flag:
                    requests.patch(
                        f"https://api.notion.com/v1/pages/{row['id']}",
                        headers=headers, json={"archived": True}, timeout=10,
                    )
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
        except Exception as e:
            logger.warning(f"Candidate log clear failed (non-fatal): {e}")
            break

    # Write new rows
    written = 0
    for s in all_stories:
        try:
            llm_rank = s.get("llm_rank")
            payload = {
                "parent": {"database_id": db_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": (s.get("title") or "")[:200]}}]},
                    "URL": {"url": s.get("url") or None},
                    "Source": {"rich_text": [{"text": {"content": (s.get("source") or "")[:200]}}]},
                    "Category": {"select": {"name": s.get("category", "unknown")}},
                    "Run Date": {"date": {"start": run_date}},
                    "Heuristic Score": {"number": round(s.get("heuristic_score", 0.0), 3)},
                    "LLM Rank": {"number": llm_rank},
                    "Confidence Score": {"number": s.get("confidence_score", 0)},
                    "Selected": {"checkbox": (s.get("url") or "") in selected_set},
                    "Reason": {"rich_text": [{"text": {"content": (s.get("selection_reason") or "")[:200]}}]},
                    "Stage Dropped": {"rich_text": [{"text": {"content": stage_dropped.get(s.get("url"), "")}}]},
                    "Paywalled": {"checkbox": bool(s.get("paywalled"))},
                },
            }
            # Notion rejects null for number properties — omit if missing
            if llm_rank is None:
                del payload["properties"]["LLM Rank"]
            if payload["properties"]["URL"]["url"] is None:
                del payload["properties"]["URL"]
            requests.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json=payload,
                timeout=10,
            ).raise_for_status()
            written += 1
        except Exception as e:
            logger.warning(f"Failed to write candidate row for '{s.get('title', '')}': {e}")

    logger.info(f"Candidate log: {written}/{len(all_stories)} entries written to Notion")


# ---------------------------------------------------------------------------
# Per-run diagnostic page (Run Log)
# ---------------------------------------------------------------------------

# Notion paragraph blocks have a 2000-char limit on their rich-text content.
_NOTION_TEXT_LIMIT = 1900  # safety margin


def _truncate(text: str, limit: int = _NOTION_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _paragraph_block(text: str) -> dict:
    """Notion 'paragraph' block with a single rich-text run."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": _truncate(text)}}],
        },
    }


def _toggle_block(heading: str, children: list[dict]) -> dict:
    """Notion 'toggle' (collapsible) block containing child blocks."""
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": heading}}],
            "children": children,
        },
    }


def _today_lines(log_path: Path, run_date: str) -> list[str]:
    """Return lines from a log whose ISO-8601 timestamp falls on run_date."""
    if not log_path.exists():
        return []
    try:
        with open(log_path) as f:
            lines = f.readlines()
    except OSError:
        return []
    # Run date is YYYY-MM-DD; logs use full ISO timestamps somewhere in the
    # line. The editorial_drops and style_check logs prefix each entry with
    # [ISO-timestamp]; source_errors prefixes with bare ISO-timestamp.
    return [ln.rstrip("\n") for ln in lines if run_date in ln]


def _build_editor_drops_section(log_path: Path, run_date: str) -> dict | None:
    """One toggle block listing today's editor drops; None if nothing to show."""
    lines = _today_lines(log_path, run_date)
    if not lines:
        return None
    children = [_paragraph_block(ln) for ln in lines]
    return _toggle_block(f"Editor drops ({len(lines)})", children)


def _build_style_section(log_path: Path, run_date: str) -> dict | None:
    """One toggle block summarising today's style-check rewrites and drops.

    The style_check log writes multi-line entries (header + BEFORE + AFTER).
    We group consecutive lines into one paragraph block per entry so the
    BEFORE/AFTER pair stays visually together.
    """
    if not log_path.exists():
        return None
    try:
        with open(log_path) as f:
            raw = f.read()
    except OSError:
        return None

    # Split on entries that start with [<date>...
    entries: list[str] = []
    current: list[str] = []
    in_today = False
    for line in raw.splitlines():
        if line.startswith("["):
            if current and in_today:
                entries.append("\n".join(current).strip())
            current = [line]
            in_today = run_date in line
        else:
            current.append(line)
    if current and in_today:
        entries.append("\n".join(current).strip())

    if not entries:
        return None

    children = [_paragraph_block(entry) for entry in entries]
    return _toggle_block(f"Style edits ({len(entries)})", children)


def _build_sources_section(log_path: Path, run_date: str) -> dict | None:
    """Toggle block with today's source FAIL / DEAD / RECOVERED lines."""
    lines = _today_lines(log_path, run_date)
    if not lines:
        return None
    children = [_paragraph_block(ln) for ln in lines]
    return _toggle_block(f"Source events ({len(lines)})", children)


def write_run_log_page(
    run_stats: dict,
    log_paths: dict,
    run_date: str,
) -> str | None:
    """Create one row in the Notion Run Logs database for today's run.

    Returns the URL of the created page, or None if Notion writes were
    skipped (no database ID) or failed.

    Args:
        run_stats: counts and labels for the run. Recognised keys:
          selected, editor_drops, published, style_rewrites, style_drops,
          source_failures, llm_tier, status.
        log_paths: dict mapping section name to absolute log file Path.
          Recognised keys: 'editor_drops', 'style', 'sources'.
        run_date: ISO date string (YYYY-MM-DD).
    """
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_RUN_LOG_DATABASE_ID")
    if not api_key or not db_id:
        logger.info("NOTION_RUN_LOG_DATABASE_ID not set — skipping run log")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    children: list[dict] = []
    sec = _build_editor_drops_section(log_paths.get("editor_drops"), run_date)
    if sec:
        children.append(sec)
    sec = _build_style_section(log_paths.get("style"), run_date)
    if sec:
        children.append(sec)
    sec = _build_sources_section(log_paths.get("sources"), run_date)
    if sec:
        children.append(sec)

    properties = {
        "Name": {"title": [{"text": {"content": f"Run log — {run_date}"}}]},
        "Run Date": {"date": {"start": run_date}},
    }
    # Number / select properties — only add if a value was provided so the
    # row stays clean when stats are missing.
    for prop, key in [
        ("Selected", "selected"),
        ("Editor drops", "editor_drops"),
        ("Published", "published"),
        ("Style rewrites", "style_rewrites"),
        ("Style drops", "style_drops"),
        ("Source failures", "source_failures"),
    ]:
        if key in run_stats and run_stats[key] is not None:
            properties[prop] = {"number": int(run_stats[key])}
    if run_stats.get("llm_tier"):
        properties["LLM tier"] = {"select": {"name": str(run_stats["llm_tier"])}}
    if run_stats.get("status"):
        properties["Status"] = {"select": {"name": str(run_stats["status"])}}

    payload = {
        "parent": {"database_id": db_id},
        "properties": properties,
        "children": children,
    }

    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        url = page.get("url")
        logger.info(f"Run log page created in Notion ({url})")
        return url
    except Exception as e:
        logger.warning(f"Run log page write failed (non-fatal): {e}")
        return None


def _feedback_signal(feedback_value: str) -> str | None:
    """Map Notion feedback value to a calibration signal.

    Handles both the legacy Stories.Feedback emoji labels and the plain-text
    values written to the Votes database by feedback_server/api/feedback.py.
    """
    if FEEDBACK_MORE in feedback_value or feedback_value in ("👍", "good"):
        return "more"
    if FEEDBACK_LESS in feedback_value or feedback_value in ("👎", "less"):
        return "less"
    if FEEDBACK_TOP in feedback_value or feedback_value in ("⭐", "top"):
        return "top"
    return None
