"""
tools/token_tracker.py

Lightweight token budget tracking for Broadsheet LLM calls.

Estimates tokens as len(text) / 4 (rough but consistent). Logs daily totals
per provider to logs/token_usage.json. At startup, warns if any provider was
within 20% of its known daily cap.

Provider caps (free tier):
  gemini:   250 RPD / no published daily token cap → warn at 200K estimated tokens
  cerebras: ~1M tokens/day
  groq:     ~500K tokens/day (12K TPM × 700 min / 60 * some headroom)
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_PATH = Path(__file__).parent.parent / "logs"
TOKEN_LOG = LOGS_PATH / "token_usage.json"

# Rough free-tier daily limits in estimated tokens
PROVIDER_CAPS = {
    "gemini": 200_000,
    "cerebras": 1_000_000,
    "groq": 500_000,
}

WARNING_THRESHOLD = 0.80  # warn if previous day was above this fraction of cap


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict:
    if TOKEN_LOG.exists():
        try:
            with open(TOKEN_LOG) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict) -> None:
    LOGS_PATH.mkdir(exist_ok=True)
    tmp = TOKEN_LOG.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(TOKEN_LOG)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 characters ≈ 1 token."""
    return max(1, len(text) // 4)


def log_call(provider: str, request_text: str, response_text: str = "") -> int:
    """Record a single LLM call. Returns estimated tokens used."""
    tokens = estimate_tokens(request_text) + estimate_tokens(response_text)
    data = _load()
    today = _today()
    if today not in data:
        data[today] = {}
    data[today][provider] = data[today].get(provider, 0) + tokens
    _save(data)
    return tokens


def check_yesterday_usage() -> None:
    """Log warnings if any provider was near its daily cap yesterday."""
    data = _load()
    if not data:
        return
    dates = sorted(data.keys())
    today = _today()
    previous_days = [d for d in dates if d < today]
    if not previous_days:
        return
    yesterday = previous_days[-1]
    usage = data[yesterday]
    for provider, cap in PROVIDER_CAPS.items():
        used = usage.get(provider, 0)
        if used >= cap * WARNING_THRESHOLD:
            pct = round(used / cap * 100)
            logger.warning(
                f"Token budget warning: {provider} used ~{used:,} tokens on {yesterday} "
                f"({pct}% of ~{cap:,} daily cap)"
            )
