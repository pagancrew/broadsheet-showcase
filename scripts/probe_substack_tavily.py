"""
scripts/probe_substack_tavily.py

Probes whether Tavily can replace Gmail IMAP for fetching Substack newsletters.
Substack blocks cloud IPs (GitHub Actions / AWS) with 403 on RSS feeds directly.
Tavily fetches from its own infrastructure, bypassing that block.

Tests two approaches for each of the 4 affected sources:
  Flavour B — client.extract(urls=[rss_url]) -> feedparser.parse(raw)
  Flavour A — client.search(query, include_domains=[domain], days=2)

Decision rule:
  Extract works for >=3 sources  -> recommend Flavour B
  Otherwise                       -> recommend Flavour A
  Both fail for all sources       -> report failure, do not migrate

Usage:
  cd broadsheet-showcase
  python scripts/probe_substack_tavily.py
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load .env so TAVILY_API_KEY is available
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

SOURCES = [
    {"name": "Import AI (Jack Clark)",      "feed": "https://importai.substack.com/feed",  "domain": "importai.substack.com",  "category": "Big News"},
    {"name": "Interconnects (N. Lambert)",  "feed": "https://www.interconnects.ai/feed",   "domain": "interconnects.ai",        "category": "Built & Released"},
    {"name": "Latent Space (Swyx)",         "feed": "https://www.latent.space/feed",       "domain": "latent.space",            "category": "Built & Released"},
    {"name": "Gary Marcus",                 "feed": "https://garymarcus.substack.com/feed","domain": "garymarcus.substack.com", "category": "Critical Voices"},
]

CUTOFF = datetime.now(timezone.utc) - timedelta(hours=48)
COL = 40


def _age_label(date_str: str | None) -> str:
    if not date_str:
        return "no date"
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hrs = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return f"{hrs:.0f}h ago"
    except Exception:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            hrs = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            return f"{hrs:.0f}h ago"
        except Exception:
            return date_str[:16]


def probe_extract(client, source: dict) -> dict:
    """Flavour B: extract RSS XML via Tavily, parse with feedparser."""
    try:
        import feedparser
    except ImportError:
        return {"ok": False, "error": "feedparser not installed"}

    try:
        resp = client.extract(urls=[source["feed"]])
    except AttributeError:
        return {"ok": False, "error": "client.extract() not available in this tavily-python version"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Tavily extract returns {"results": [{"url": ..., "raw_content": ...}]}
    results = resp.get("results", []) if isinstance(resp, dict) else []
    if not results:
        return {"ok": False, "error": "empty results from extract()"}

    raw = results[0].get("raw_content", "") or results[0].get("content", "")
    if not raw:
        return {"ok": False, "error": "no content in extract result"}

    feed = feedparser.parse(raw)
    entries = feed.get("entries", [])
    if not entries:
        # feedparser may not parse if content is HTML rather than RSS XML
        return {"ok": False, "error": f"feedparser got 0 entries (content starts: {raw[:80]!r})"}

    recent = [e for e in entries if _is_recent(e)]
    first = entries[0]
    return {
        "ok": True,
        "total_entries": len(entries),
        "recent_entries": len(recent),
        "first_title": first.get("title", "")[:COL],
        "first_age": _age_label(first.get("published") or first.get("updated")),
    }


def probe_search(client, source: dict) -> dict:
    """Flavour A: search Tavily for recent posts on the source domain."""
    try:
        resp = client.search(
            query="new post",
            search_depth="basic",
            max_results=3,
            include_domains=[source["domain"]],
            days=2,
            include_answer=False,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    results = resp.get("results", [])
    if not results:
        return {"ok": False, "error": "0 search results"}

    first = results[0]
    return {
        "ok": True,
        "total_results": len(results),
        "first_title": (first.get("title") or "")[:COL],
        "first_age": _age_label(first.get("published_date")),
        "first_url": (first.get("url") or "")[:COL],
    }


def _is_recent(entry) -> bool:
    for field in ("published", "updated"):
        val = entry.get(field)
        if val:
            try:
                import email.utils
                dt = email.utils.parsedate_to_datetime(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt >= CUTOFF
            except Exception:
                pass
    return False


def main():
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        print("ERROR: TAVILY_API_KEY not set")
        sys.exit(1)

    try:
        from tavily import TavilyClient
    except ImportError:
        print("ERROR: tavily-python not installed. Run: pip install tavily-python")
        sys.exit(1)

    client = TavilyClient(api_key=api_key)

    print(f"\n{'='*70}")
    print("Substack Tavily Probe")
    print(f"Window: last 48h (since {CUTOFF.strftime('%Y-%m-%d %H:%M UTC')})")
    print(f"{'='*70}\n")

    extract_successes = 0
    search_successes = 0
    rows = []

    for src in SOURCES:
        print(f"  Testing: {src['name']}")

        b = probe_extract(client, src)
        a = probe_search(client, src)

        if b["ok"]:
            extract_successes += 1
        if a["ok"]:
            search_successes += 1

        rows.append((src["name"], b, a))

    # Print results table
    print(f"\n{'─'*70}")
    print(f"{'Source':<30} {'EXTRACT (B)':<20} {'SEARCH (A)':<20}")
    print(f"{'─'*70}")
    for name, b, a in rows:
        b_cell = f"OK ({b['recent_entries']} recent)" if b.get("ok") else f"FAIL: {b.get('error','')[:18]}"
        a_cell = f"OK ({a['total_results']} results)" if a.get("ok") else f"FAIL: {a.get('error','')[:18]}"
        print(f"  {name:<28} {b_cell:<20} {a_cell:<20}")

    print(f"{'─'*70}")
    print(f"  Extract successes: {extract_successes}/4")
    print(f"  Search successes:  {search_successes}/4")

    # Sample output
    print(f"\n{'Sample results':─<70}")
    for name, b, a in rows:
        print(f"\n  {name}")
        if b.get("ok"):
            print(f"    Extract → '{b['first_title']}' ({b['first_age']}) [{b['total_entries']} total entries]")
        else:
            print(f"    Extract → {b['error']}")
        if a.get("ok"):
            print(f"    Search  → '{a['first_title']}' ({a['first_age']})")
        else:
            print(f"    Search  → {a['error']}")

    # Decision
    print(f"\n{'Decision':─<70}")
    if extract_successes >= 3:
        print("  VERDICT: Flavour B (Extract) viable — proceed with migration")
        print("  Tavily extract returns RSS XML that feedparser can parse.")
        print("  inbox_fetcher.py will be replaced with extract-based fetcher.")
        sys.exit(0)
    elif search_successes >= 3:
        print("  VERDICT: Flavour A (Search) viable — proceed with migration")
        print("  Extract did not work; search returns adequate results.")
        print("  inbox_fetcher.py will be replaced with search-based fetcher.")
        sys.exit(2)  # exit 2 = use Flavour A
    else:
        print("  VERDICT: Neither approach works reliably. Do not migrate.")
        print("  Manual investigation required before proceeding.")
        sys.exit(1)


if __name__ == "__main__":
    main()
