"""
One-shot script to archive all pages in the Notion Stories database.
Run this locally to reset the cross-day dedup history.

Usage:
    cd broadsheet-showcase
    python tools/reset_stories_db.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("NOTION_API_KEY")
DB_ID = os.environ.get("NOTION_DATABASE_ID")

if not API_KEY or not DB_ID:
    print("Error: NOTION_API_KEY and NOTION_DATABASE_ID must be set in .env")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def fetch_all_page_ids():
    ids = []
    payload = {"page_size": 100}
    while True:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{DB_ID}/query",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        for page in body.get("results", []):
            ids.append(page["id"])
        if body.get("has_more"):
            payload["start_cursor"] = body["next_cursor"]
        else:
            break
    return ids


def archive_page(page_id):
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"archived": True},
        timeout=15,
    )
    resp.raise_for_status()


if __name__ == "__main__":
    print(f"Fetching all pages from Stories database ({DB_ID[:8]}...)...")
    page_ids = fetch_all_page_ids()
    print(f"Found {len(page_ids)} pages. Archiving...")

    archived = 0
    errors = 0
    for i, pid in enumerate(page_ids, 1):
        try:
            archive_page(pid)
            archived += 1
            if i % 20 == 0:
                print(f"  {i}/{len(page_ids)}...")
        except Exception as e:
            print(f"  Failed to archive {pid}: {e}")
            errors += 1

    print(f"\nDone. Archived {archived} pages, {errors} errors.")
    print("Cross-day dedup history is now clear.")
