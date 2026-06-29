"""
main.py — Broadsheet entry point

Runs the full pipeline:
  1. Gather raw stories from 4 search agents (in parallel)
  2. Read Notion feedback from previous days
  3. NewsroomLeadAgent scores and selects stories
  4. EditorAgent writes one-paragraph summaries
  5. Deliver to Notion database and email

Usage:
  python main.py                    # full run
  python main.py --dry-run          # run pipeline but don't deliver
  python main.py --no-email         # Notion only
  python main.py --no-notion        # email only
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env before any other imports that might need env vars
load_dotenv(Path(__file__).parent / ".env")

from agents.search_agents import (
    gather_big_news,
    gather_laws_ethics,
    gather_builder,
    gather_critical_voices,
)
from agents.newsroom_lead import select_stories
from agents.editor import write_digest
from tools.notion_delivery import read_feedback, read_multi_feedback, read_published_urls, read_rejected_urls, publish_digest, write_run_log_page
from tools.email_sender import send_digest
from tools.inbox_fetcher import fetch_substack_via_tavily
from tools.source_monitor import get_alerts, format_alerts_for_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("broadsheet")


def _load_digest_config() -> dict:
    with open(Path(__file__).parent / "config" / "digest.yaml") as f:
        return yaml.safe_load(f)


def _build_llm(config: dict, force_fallback: bool = False):
    """
    Build the LLM client. Tries primary (Gemini) first.
    Falls back to Cerebras on failure or if force_fallback=True.

    Returns: (llm_instance, fallback_used: bool)
    """
    from crewai import LLM

    digest_cfg = config
    primary_cfg = digest_cfg.get("models", {}).get("primary", {})
    fallback_cfg = digest_cfg.get("models", {}).get("fallback", {})

    # Primary: Gemini
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key and not force_fallback:
        try:
            llm = LLM(
                model=primary_cfg.get("model", "gemini/gemini-3.1-flash-lite-preview"),
                api_key=gemini_key,
                temperature=primary_cfg.get("temperature", 1.0),
                num_retries=1,
            )
            llm.call([{"role": "user", "content": "Reply with just: ok"}])
            logger.info(f"Using primary model: {primary_cfg.get('model')}")
            return llm, False
        except Exception as e:
            logger.warning(f"Primary model unavailable ({e}) — switching to fallback")
    elif not gemini_key:
        logger.warning("GEMINI_API_KEY not set — using fallback model")

    # Fallback: Cerebras
    cerebras_key = os.environ.get("CEREBRAS_API_KEY")
    if not cerebras_key:
        logger.error("CEREBRAS_API_KEY not set — cannot build fallback LLM")
        sys.exit(1)

    fallback_model = fallback_cfg.get("model", "qwen-3-235b-a22b-instruct-2507")
    llm = LLM(
        model=f"cerebras/{fallback_model}",
        api_key=cerebras_key,
        temperature=fallback_cfg.get("temperature", 1.0),
        num_retries=1,
    )
    logger.info(f"Using fallback model: cerebras/{fallback_model}")
    return llm, True


def _build_tertiary_llm(config: dict):
    """
    Build the tertiary LLM client (Groq Llama 3.3 70B by default).

    Used as a last-resort scoring tier when both primary (Gemini) and
    fallback (Cerebras) fail. Returns None if GROQ_API_KEY is not set
    (tertiary is optional — if absent, the system falls through to equal
    scoring as before).

    Returns: LLM instance, or None if no key is configured.
    """
    from crewai import LLM

    tertiary_cfg = config.get("models", {}).get("tertiary", {})
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        logger.info("GROQ_API_KEY not set — tertiary scoring tier disabled")
        return None

    tertiary_model = tertiary_cfg.get("model", "llama-3.3-70b-versatile")
    llm = LLM(
        model=f"groq/{tertiary_model}",
        api_key=groq_key,
        temperature=tertiary_cfg.get("temperature", 1.0),
        num_retries=1,
    )
    logger.info(f"Tertiary scoring model ready: groq/{tertiary_model}")
    return llm


def _build_llm_with_thinking(config: dict, thinking_level: str):
    """
    Gemini LLM variant with thinking_level set for deliberate multi-step reasoning.
    Only call after _build_llm() has confirmed Gemini is available — skips sanity check.
    """
    from crewai import LLM
    primary_cfg = config.get("models", {}).get("primary", {})
    gemini_key = os.environ.get("GEMINI_API_KEY")
    return LLM(
        model=primary_cfg.get("model", "gemini/gemini-3.1-flash-lite-preview"),
        api_key=gemini_key,
        temperature=primary_cfg.get("temperature", 1.0),
        num_retries=1,
        extra_body={"thinkingLevel": thinking_level},
    )


def _load_sources_config() -> dict:
    with open(Path(__file__).parent / "config" / "sources.yaml") as f:
        return yaml.safe_load(f)


def run(dry_run: bool = False, no_email: bool = False, no_notion: bool = False) -> None:
    logger.info(f"Broadsheet starting — {date.today().isoformat()}")

    try:
        from tools.token_tracker import check_yesterday_usage
        check_yesterday_usage()
    except Exception:
        pass  # token tracking is best-effort

    digest_cfg = _load_digest_config()
    sources_cfg = _load_sources_config()

    # -------------------------------------------------------------------------
    # 1. Build LLM (try primary, auto-fallback)
    # -------------------------------------------------------------------------
    llm, fallback_used = _build_llm(digest_cfg)
    if fallback_used:
        logger.warning("⚠ Running on fallback model today")

    # Build fallback LLM for mid-run recovery (no sanity-check call needed)
    fallback_llm = None
    if not fallback_used:
        try:
            fallback_llm, _ = _build_llm(digest_cfg, force_fallback=True)
        except Exception as e:
            logger.warning(f"Could not initialise fallback LLM: {e}")

    # Build tertiary LLM (Groq) — last-resort scoring tier before equal scoring
    tertiary_llm = None
    try:
        tertiary_llm = _build_tertiary_llm(digest_cfg)
    except Exception as e:
        logger.warning(f"Could not initialise tertiary LLM: {e}")

    # Build thinking-enabled LLM for newsroom selection (Gemini only).
    # Falls back to regular llm if already on fallback model.
    thinking_cfg = digest_cfg.get("agent_thinking", {})
    newsroom_llm = llm
    if not fallback_used and thinking_cfg.get("newsroom_lead"):
        try:
            newsroom_llm = _build_llm_with_thinking(digest_cfg, thinking_cfg["newsroom_lead"])
            logger.info(f"Newsroom LLM: thinking_level={thinking_cfg['newsroom_lead']}")
        except Exception as e:
            logger.warning(f"Could not build thinking LLM ({e}) — using standard LLM for newsroom")

    # -------------------------------------------------------------------------
    # 2. Gather raw stories from all 4 agents in parallel
    # -------------------------------------------------------------------------
    logger.info("Gathering stories from all sources...")
    all_stories = []
    gather_fns = {
        "big_news": gather_big_news,
        "laws_ethics": gather_laws_ethics,
        "builder": gather_builder,
        "critical_voices": gather_critical_voices,
    }

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fn, llm): name
            for name, fn in gather_fns.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                stories = future.result()
                all_stories.extend(stories)
                logger.info(f"  {name}: {len(stories)} stories")
            except Exception as e:
                logger.error(f"  {name} failed: {e}", exc_info=True)

    # Fetch Substack newsletters via Tavily (direct RSS blocked on cloud IPs)
    tavily_cfg = sources_cfg.get("tavily_substack", {})
    if tavily_cfg.get("enabled", False):
        tavily_stories = fetch_substack_via_tavily(
            sources=tavily_cfg.get("sources", []),
            default_category=tavily_cfg.get("default_category", "critical_voices"),
        )
        if tavily_stories:
            all_stories.extend(tavily_stories)
            logger.info(f"  tavily_substack: {len(tavily_stories)} stories")

    if not all_stories:
        logger.error("No stories gathered — aborting")
        sys.exit(1)

    logger.info(f"Total raw stories: {len(all_stories)}")

    # -------------------------------------------------------------------------
    # 3. Read Notion feedback for calibration
    # -------------------------------------------------------------------------
    feedback_days = digest_cfg.get("feedback_lookback_days", 7)
    logger.info(f"Reading Notion feedback (last {feedback_days} days)...")
    feedback = {}
    published_urls: set = set()
    rejected_urls: set = set()
    if not no_notion:
        try:
            feedback = read_multi_feedback(days_back=feedback_days)
            logger.info(f"  Feedback items: {feedback.get('total_items', 0)}")
        except Exception as e:
            logger.warning(f"Could not read Notion feedback: {e}")
        try:
            published_urls = read_published_urls(days_back=7)
        except Exception as e:
            logger.warning(f"Could not read published URLs for dedup: {e}")
        try:
            rejected_urls = read_rejected_urls(days_back=30, reasons=["recency_cutoff"])
        except Exception as e:
            logger.warning(f"Could not read rejected URLs: {e}")

    # -------------------------------------------------------------------------
    # 4. NewsroomLeadAgent: score and select
    # -------------------------------------------------------------------------
    min_confidence = digest_cfg.get("min_confidence_score", 6)
    logger.info(f"Newsroom selecting stories (min confidence: {min_confidence})...")
    selected, scoring_label = select_stories(
        all_stories=all_stories,
        feedback=feedback,
        llm=newsroom_llm,
        min_confidence=min_confidence,
        fallback_llm=fallback_llm,
        tertiary_llm=tertiary_llm,
        published_urls=published_urls,
        rejected_urls=rejected_urls,
    )
    logger.info(f"Selected: {len(selected)} stories")

    if not selected:
        logger.warning("No stories passed selection threshold — lowering to 4 and retrying")
        selected, scoring_label = select_stories(
            all_stories,
            feedback,
            newsroom_llm,
            min_confidence=4,
            fallback_llm=fallback_llm,
            tertiary_llm=tertiary_llm,
            published_urls=published_urls,
            rejected_urls=rejected_urls,
        )

    # -------------------------------------------------------------------------
    # 5. EditorAgent: write summaries
    # -------------------------------------------------------------------------
    # Get any source alerts to include in digest
    alerts = get_alerts()
    alerts_text = format_alerts_for_digest(alerts)
    if alerts_text:
        logger.warning(f"Source alerts: {len(alerts)} sources with issues")

    logger.info("Editor writing summaries...")
    final_stories, editor_label, editor_drops = write_digest(
        selected_stories=selected,
        llm=llm,
        fallback_model_used=fallback_used,
        alerts_text=alerts_text,
        fallback_llm=fallback_llm,
        tertiary_llm=tertiary_llm,
    )

    # Style check: regex scan + targeted rewrite for banned phrases
    from tools.style_check import run as _style_check
    final_stories, style_counts = _style_check(final_stories, llm, fallback_llm, tertiary_llm)

    primary_name = digest_cfg.get("models", {}).get("primary", {}).get("model", "gemini/gemini-3.1-flash-lite-preview")
    fallback_name = "cerebras/" + digest_cfg.get("models", {}).get("fallback", {}).get("model", "qwen-3-235b-a22b-instruct-2507")
    tertiary_name = "groq/" + digest_cfg.get("models", {}).get("tertiary", {}).get("model", "llama-3.3-70b-versatile")

    def _resolve(label):
        return (
            label.replace("primary", fallback_name if fallback_used else primary_name)
            .replace("fallback", fallback_name)
            .replace("tertiary", tertiary_name)
        )

    from tools import tavily_search as _tavily_mod
    tavily_usage = _tavily_mod.get_usage()
    if _tavily_mod.quota_exceeded:
        tavily_line = "Tavily searches skipped — monthly credit limit reached"
    elif tavily_usage:
        tavily_line = f"Tavily: {tavily_usage['pct']}% used ({tavily_usage['used']}/{tavily_usage['limit']})"
    else:
        tavily_line = ""

    run_report = {
        "startup": fallback_name if fallback_used else primary_name,
        "scoring": "None — equal scoring (LLM timed out)" if scoring_label == "equal" else _resolve(scoring_label),
        "summaries": _resolve(editor_label),
        "tavily": tavily_line,
    }

    # -------------------------------------------------------------------------
    # 6. Deliver
    # -------------------------------------------------------------------------
    if dry_run:
        logger.info("DRY RUN — printing digest to stdout:")
        _print_digest(final_stories, alerts_text, run_report)
        return

    notion_url = None
    delivery_cfg = digest_cfg.get("delivery", {})

    # Notion
    if delivery_cfg.get("notion", {}).get("enabled", True) and not no_notion:
        logger.info("Publishing to Notion...")
        created = publish_digest(final_stories, alerts_text)
        logger.info(f"  Notion: {created} rows created")
        # Build a filtered Notion URL for today
        db_id = os.environ.get("NOTION_DATABASE_ID", "")
        if db_id:
            notion_url = f"https://www.notion.so/{db_id.replace('-', '')}"

    # Per-run diagnostic page (Run Log) — written before email so we can
    # include its URL alongside the candidates DB link at the top of each email.
    run_log_url = None
    candidates_url = None
    candidates_db_id = os.environ.get("NOTION_CANDIDATES_DATABASE_ID", "")
    if candidates_db_id:
        candidates_url = f"https://www.notion.so/{candidates_db_id.replace('-', '')}"

    if not no_notion:
        run_stats = {
            "selected": len(selected),
            "editor_drops": editor_drops,
            "published": len(final_stories),
            "style_rewrites": style_counts.get("rewritten", 0),
            "style_drops": style_counts.get("dropped", 0),
            "source_failures": len(alerts) if alerts else 0,
            "llm_tier": editor_label,
            "status": "OK" if final_stories else "Failed",
        }
        log_paths = {
            "editor_drops": Path(__file__).parent / "logs" / "editorial_drops.log",
            "style": Path(__file__).parent / "logs" / "style_check.log",
            "sources": Path(__file__).parent / "logs" / "source_errors.log",
        }
        try:
            run_log_url = write_run_log_page(
                run_stats=run_stats,
                log_paths=log_paths,
                run_date=date.today().isoformat(),
            )
        except Exception as e:
            logger.warning(f"Run log write failed (non-fatal): {e}")

    # Email
    if delivery_cfg.get("email", {}).get("enabled", True) and not no_email:
        recipients = delivery_cfg.get("email", {}).get("recipients", [])
        subject_template = delivery_cfg.get("email", {}).get("subject", "Broadsheet — {date}")
        subject = subject_template.format(date=date.today().strftime("%A, %-d %B %Y"))

        feedback_endpoint = delivery_cfg.get("feedback_endpoint", "")
        logger.info(f"Sending email to {len(recipients)} recipient(s)...")
        sent = send_digest(
            stories=final_stories,
            subject=subject,
            recipients=recipients,
            notion_url=notion_url,
            alerts_text=alerts_text,
            feedback_endpoint=feedback_endpoint,
            run_report=run_report,
            candidates_url=candidates_url,
            run_log_url=run_log_url,
        )
        if sent:
            logger.info("  Email: sent")
        else:
            logger.error("  Email: failed")

    logger.info("Broadsheet done.")


def _print_digest(stories: list[dict], alerts_text: str, run_report: dict | None = None) -> None:
    """Print digest to stdout for dry-run inspection."""
    print(f"\n{'='*60}")
    print(f"BROADSHEET — {date.today().strftime('%A, %-d %B %Y')}")
    print(f"{'='*60}")
    if run_report:
        print(f"  Startup model : {run_report.get('startup', '?')}")
        print(f"  Scoring       : {run_report.get('scoring', '?')}")
        print(f"  Summaries     : {run_report.get('summaries', '?')}")
        if run_report.get("tavily"):
            print(f"  Tavily        : {run_report['tavily']}")
        print()

    current_cat = None
    for s in stories:
        cat = s.get("category_label", s.get("category", ""))
        if cat != current_cat:
            current_cat = cat
            print(f"\n--- {cat.upper()} ---\n")
        print(f"● {s.get('title', 'Untitled')}")
        print(f"  {s.get('url', '')}")
        print(f"  {s.get('summary', '')}")
        print()

    if alerts_text:
        print(f"\n{alerts_text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Broadsheet AI digest")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without delivering")
    parser.add_argument("--no-email", action="store_true", help="Skip email delivery")
    parser.add_argument("--no-notion", action="store_true", help="Skip Notion delivery")
    args = parser.parse_args()

    run(dry_run=args.dry_run, no_email=args.no_email, no_notion=args.no_notion)
