"""
scripts/test_cerebras_chunk_size.py

One-off script to find the safe chunk size for Cerebras (qwen-3-235b).
Cerebras free tier has an ~8K-token context cap per request.
This script sends progressively larger batches until Cerebras errors,
then reports the largest batch that succeeded.

Usage:
    cd broadsheet-showcase
    python scripts/test_cerebras_chunk_size.py

Requires CEREBRAS_API_KEY in your .env.
"""

import os
import sys
import json

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


def make_fake_stories(n: int) -> list[dict]:
    return [
        {
            "id": i,
            "title": f"AI story number {i}: a moderately long headline about artificial intelligence",
            "source": "test-source",
            "category": "big_news",
            "description": "This is a test story description. " * 5,
        }
        for i in range(n)
    ]


def build_prompt(stories: list[dict]) -> str:
    stories_json = json.dumps(stories, indent=2)
    return f"""You are a newsroom editor. Rank these {len(stories)} AI news stories by importance.
Return ONLY a JSON array ordered best-first:
[{{"id": <int>, "rank": <int>, "category": "big_news", "reason": ""}}]

Stories:
{stories_json}
"""


def try_batch(llm, n: int) -> bool:
    stories = make_fake_stories(n)
    user_prompt = build_prompt(stories)
    messages = [
        {"role": "system", "content": "You are a news editor."},
        {"role": "user", "content": user_prompt},
    ]
    approx_tokens = len(user_prompt) // 4
    print(f"  Trying {n} stories (~{approx_tokens:,} estimated tokens)... ", end="", flush=True)
    try:
        response = llm.call(messages)
        print(f"OK ({len(response)} chars response)")
        return True
    except Exception as e:
        print(f"FAILED: {e}")
        return False


def main():
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        print("ERROR: CEREBRAS_API_KEY not set in .env")
        sys.exit(1)

    try:
        from litellm import LLM
        llm = LLM(
            model="cerebras/qwen-3-235b-a22b-instruct-2507",
            api_key=key,
            base_url="https://api.cerebras.ai/v1",
            num_retries=0,
        )
    except Exception as e:
        print(f"ERROR building Cerebras LLM: {e}")
        sys.exit(1)

    print("Cerebras chunk size test")
    print("========================")
    print(f"Model: qwen-3-235b-a22b-instruct-2507")
    print()

    last_success = 0
    for n in [10, 20, 30, 40, 50, 60, 70, 80]:
        success = try_batch(llm, n)
        if success:
            last_success = n
        else:
            break

    print()
    if last_success:
        print(f"Safe chunk size: {last_success} stories")
        print(f"Estimated tokens at {last_success} stories: ~{len(build_prompt(make_fake_stories(last_success))) // 4:,}")
        print()
        current = 60
        if last_success < current:
            print(f"ACTION: Update chunk_size in newsroom_lead.py from {current} to {last_success}")
        else:
            print(f"Current chunk_size={current} is within the safe limit.")
    else:
        print("All batch sizes failed — check your CEREBRAS_API_KEY and connectivity.")


if __name__ == "__main__":
    main()
