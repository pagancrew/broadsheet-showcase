"""
tools/source_monitor.py

Tracks source fetch failures across runs.
- Logs failures to logs/source_errors.log
- Maintains a consecutive-failure count per source
- Returns a summary of alerts for inclusion in the digest
"""

import json
import logging
import os
import threading
from datetime import datetime, date
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "source_errors.log"
STATE_PATH = Path(__file__).parent.parent / "logs" / "source_state.json"
DEAD_THRESHOLD = 3  # consecutive failures before "likely dead" flag

_state_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _load_state() -> dict:
    """Load persistent failure state from disk."""
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    """Write state atomically: write to a temp file then rename into place."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_PATH)


def _append_log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now().isoformat()} {message}\n")


def record_success(source_name: str) -> None:
    """Reset failure and zero-yield counts for a source that returned results."""
    with _state_lock:
        state = _load_state()
        if source_name in state and state[source_name]["consecutive_failures"] > 0:
            logger.info(f"Source recovered: {source_name}")
            _append_log(f"RECOVERED {source_name}")
        state[source_name] = {
            "consecutive_failures": 0,
            "consecutive_zero_yield": 0,
            "last_success": date.today().isoformat(),
            "last_error": state.get(source_name, {}).get("last_error"),
        }
        _save_state(state)


def record_zero_yield(source_name: str) -> None:
    """Record that a source fetched OK but returned 0 stories.

    Distinct from record_failure (no network/API error occurred — the source
    is reachable but silent). Resets on the next non-zero fetch via record_success.
    Surfaces in get_alerts() after 2 consecutive zero-yield runs.
    """
    with _state_lock:
        state = _load_state()
        current = state.get(source_name, {"consecutive_failures": 0})
        consecutive_zero = current.get("consecutive_zero_yield", 0) + 1
        state[source_name] = {
            **current,
            "consecutive_zero_yield": consecutive_zero,
            "last_zero_date": date.today().isoformat(),
        }
        _save_state(state)

    _append_log(
        f"ZERO_YIELD {source_name} (#{consecutive_zero}): fetched OK but 0 stories returned"
    )
    logger.warning(
        f"Source zero yield #{consecutive_zero}: {source_name} — fetched OK but 0 stories"
    )


def record_failure(source_name: str, error: str) -> None:
    """Record a fetch failure for a source."""
    with _state_lock:
        state = _load_state()
        current = state.get(source_name, {"consecutive_failures": 0})
        consecutive = current["consecutive_failures"] + 1

        state[source_name] = {
            "consecutive_failures": consecutive,
            "last_success": current.get("last_success"),
            "last_error": error,
            "last_failure_date": date.today().isoformat(),
        }
        _save_state(state)

    log_level = "DEAD" if consecutive >= DEAD_THRESHOLD else "FAIL"
    _append_log(f"{log_level} {source_name} (#{consecutive}): {error}")
    logger.warning(f"Source failure #{consecutive}: {source_name} — {error}")


def get_alerts() -> list[dict]:
    """
    Return a list of alert dicts for sources that have failed or gone silent.
    Used by the Editor agent to include in the digest.

    Returns:
        List of dicts with keys: source_name, consecutive_failures,
        likely_dead, last_error, last_success
    """
    state = _load_state()
    alerts = []
    for source_name, info in state.items():
        if info["consecutive_failures"] > 0 and not (info.get("last_error") or "").startswith("[TAVILY-QUOTA]"):
            alerts.append(
                {
                    "source_name": source_name,
                    "consecutive_failures": info["consecutive_failures"],
                    "likely_dead": info["consecutive_failures"] >= DEAD_THRESHOLD,
                    "last_error": info.get("last_error", "unknown"),
                    "last_success": info.get("last_success", "never"),
                }
            )
        # Also surface sources that fetch OK but silently return nothing
        consecutive_zero = info.get("consecutive_zero_yield", 0)
        if consecutive_zero >= 2 and info["consecutive_failures"] == 0:
            alerts.append(
                {
                    "source_name": source_name,
                    "consecutive_failures": 0,
                    "likely_dead": consecutive_zero >= DEAD_THRESHOLD,
                    "last_error": f"returned 0 stories ({consecutive_zero} runs in a row)",
                    "last_success": info.get("last_success", "never"),
                }
            )
    return sorted(alerts, key=lambda x: x["consecutive_failures"], reverse=True)


def format_alerts_for_digest(alerts: list[dict]) -> str:
    """Format source alerts as a plain-text section for the digest."""
    if not alerts:
        return ""

    lines = ["⚠ Source Alerts", ""]
    for a in alerts:
        status = "🔴 Likely dead" if a["likely_dead"] else "🟡 Failing"
        lines.append(
            f"{status} — {a['source_name']} "
            f"({a['consecutive_failures']} consecutive failure(s)). "
            f"Last error: {a['last_error']}"
        )
    lines.append("")
    lines.append(
        "To remove a dead source: edit config/sources.yaml "
        "and set enabled: false or delete the entry."
    )
    return "\n".join(lines)
