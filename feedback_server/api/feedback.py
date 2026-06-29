"""
Vercel serverless function — records story click-through feedback in the Votes database.

Routes:
  GET /api/feedback?id=PAGE_ID&sub=SUBSCRIBER_ID&v=top&url=ARTICLE_URL
      → records a ⭐ vote row, 302 redirects to the article URL

sub defaults to "owner" if absent (backwards compatible with single-subscriber setup).

Votes are written to a separate Notion "Votes" database (NOTION_VOTES_DATABASE_ID),
one row per click. The Stories database Feedback property is not written to.

Requires env vars NOTION_API_KEY and NOTION_VOTES_DATABASE_ID in Vercel dashboard.
"""

import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

NOTION_VERSION = "2022-06-28"

VOTE_LABEL = {
    "top": "top",
}


def _notion_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _create_vote_row(
    headers: dict,
    votes_db_id: str,
    subscriber_id: str,
    page_id: str,
    story_title: str,
    story_source: str,
    vote: str,
    tags: list[str],
) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json={
            "parent": {"database_id": votes_db_id},
            "properties": {
                "Subscriber":     {"title":      [{"text": {"content": subscriber_id}}]},
                "Story Page ID":  {"rich_text":  [{"text": {"content": page_id}}]},
                "Story Title":    {"rich_text":  [{"text": {"content": story_title[:2000]}}]},
                "Story Source":   {"rich_text":  [{"text": {"content": story_source[:200]}}]},
                "Vote":           {"select":     {"name": vote}},
                "Story Tags":     {"multi_select": [{"name": t[:100]} for t in tags[:5]]},
                "Voted At":       {"date":       {"start": now_iso}},
            },
        },
        timeout=10,
    ).raise_for_status()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params        = parse_qs(urlparse(self.path).query)
        page_id       = params.get("id",  [""])[0].strip()
        vote          = params.get("v",   [""])[0].strip()
        subscriber_id = params.get("sub", ["owner"])[0].strip() or "owner"

        ua  = self.headers.get("User-Agent", "")
        xff = self.headers.get("X-Forwarded-For", "")
        print(f"[FB] start id={page_id!r} sub={subscriber_id!r} v={vote!r} ua={ua!r} xff={xff!r}")

        if not page_id or vote not in VOTE_LABEL:
            print(f"[FB] 400 bad-request id={page_id!r} v={vote!r}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad request")
            return

        api_key      = os.environ["NOTION_API_KEY"]
        votes_db_id  = os.environ["NOTION_VOTES_DATABASE_ID"]
        headers      = _notion_headers(api_key)

        # Fetch the story page to get its title, source, and tags for the vote row
        story_title = ""
        story_source = ""
        story_tags: list[str] = []
        try:
            page_resp = requests.get(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                timeout=10,
            )
            print(f"[FB] story-fetch status={page_resp.status_code}")
            if page_resp.ok:
                page_data = page_resp.json()
                props = page_data.get("properties", {})
                title_items = props.get("Title", {}).get("title", [])
                if title_items:
                    story_title = title_items[0].get("plain_text", "")
                source_items = props.get("Source", {}).get("rich_text", [])
                if source_items:
                    story_source = source_items[0].get("plain_text", "")
                story_tags = [
                    t.get("name", "")
                    for t in props.get("Tags", {}).get("multi_select", [])
                    if t.get("name")
                ]
                print(f"[FB] story title={story_title!r} source={story_source!r} tags={story_tags}")
        except Exception as exc:
            print(f"[STORY-FETCH-ERROR] {exc}")

        # Write vote row and redirect to article
        print(f"[FB] top-pick: writing row sub={subscriber_id!r}")
        _create_vote_row(headers, votes_db_id, subscriber_id, page_id, story_title, story_source, "top", story_tags)
        print(f"[FB] WROTE top sub={subscriber_id!r} page={page_id!r}")
        article_url = params.get("url", [""])[0].strip()
        if article_url:
            self.send_response(302)
            self.send_header("Location", article_url)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
