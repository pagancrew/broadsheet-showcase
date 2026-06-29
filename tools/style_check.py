"""
tools/style_check.py

Post-write style enforcement. Runs after the Editor and before delivery.

Stage A: Regex scan for banned phrases.
Stage B: Surgical LLM rewrite of flagged sentences. If still banned after
         one rewrite, the sentence is dropped entirely.

Also flags paragraphs over 60 words for LLM tightening.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_PATH = Path(__file__).parent.parent / "logs" / "style_check.log"

# ---------------------------------------------------------------------------
# Banned patterns — safety net for editor style violations
# ---------------------------------------------------------------------------

BANNED_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "For those tracking X, this signals Y" formula — dominant violation
    (
        re.compile(
            r"\bFor\s+(those|anyone|developers|engineers|observers|researchers|"
            r"practitioners|readers|software\s+engineers|software\s+developers)\b"
            r"[^.]{0,80}\b(this|the\s+(release|finding|critique|analysis|shift|move|trend))\b",
            re.IGNORECASE,
        ),
        'Avoid "For those tracking X, this signals/represents Y" — state the implication directly from the facts.',
    ),
    (
        re.compile(r"\b(This|That)\s+matters\s+because\b", re.IGNORECASE),
        'Avoid "This matters because" — let the implication follow from the facts.',
    ),
    (
        re.compile(r"\bIn\s+a\s+world\s+where\b", re.IGNORECASE),
        'Avoid "In a world where" — too generic.',
    ),
    (
        re.compile(r"\bAs\s+AI\s+continues\s+to\b", re.IGNORECASE),
        'Avoid "As AI continues to" — filler phrase.',
    ),
    (
        re.compile(r"\b(according\s+to\s+the\s+source|the\s+source\s+suggests)\b", re.IGNORECASE),
        'Avoid "according to the source" / "the source suggests" — attribute by publication name.',
    ),
    (
        re.compile(r"\bfor\s+AI\s+development\b\.?\s*$", re.IGNORECASE | re.MULTILINE),
        'Avoid ending sentences with "for AI development" as a generic relevance signal.',
    ),
    # Inverted "For those tracking X, this signals Y" — word order flipped to evade the pattern above.
    # e.g. "This is relevant for anyone…" / "This matters to those tracking…"
    (
        re.compile(
            r"\bThis\s+\w+(?:\s+\w+){0,5}?\s+(?:for|to)\s+"
            r"(those|anyone|developers|engineers|observers|researchers|"
            r"practitioners|readers|software\s+engineers|software\s+developers)\b",
            re.IGNORECASE,
        ),
        'Avoid "This [is/matters/signals X] for/to those tracking…" — same banned formula, inverted. Drop the implication sentence entirely.',
    ),
]

_WORD_LIMIT = 60

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(stories: list[dict], llm, fallback_llm=None, tertiary_llm=None) -> tuple[list[dict], dict]:
    """
    Scan and fix style violations in story summaries.

    Returns (stories, counts):
      - stories: same list with modified 'summary' fields where violations
        were found. Non-violating sentences are byte-identical.
      - counts: dict with 'flagged', 'rewritten', 'dropped' integer totals
        for the whole digest. Consumed by the run-log page.
    """
    _init_log()
    llms = [c for c in (llm, fallback_llm, tertiary_llm) if c is not None]
    total_flagged = total_rewritten = total_dropped = 0

    for story in stories:
        summary = story.get("summary", "")
        if not summary:
            continue

        summary, counts = _check_and_fix(summary, llms, title=story.get("title", ""))
        total_flagged += counts["flagged"]
        total_rewritten += counts["rewritten"]
        total_dropped += counts["dropped"]

        words = summary.split()
        if len(words) > _WORD_LIMIT:
            tightened = _tighten(summary, llms)
            if tightened:
                _log(
                    f"TIGHTEN [{story.get('title', '')[:50]}]: "
                    f"{len(words)}w → {len(tightened.split())}w"
                )
                summary = tightened

        story["summary"] = summary

    logger.info(
        f"Style check complete: {total_flagged} sentences flagged, "
        f"{total_rewritten} rewritten, {total_dropped} dropped"
    )
    return stories, {
        "flagged": total_flagged,
        "rewritten": total_rewritten,
        "dropped": total_dropped,
    }


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _check_and_fix(text: str, llms: list, title: str = "") -> tuple[str, dict]:
    """Scan text for banned patterns; rewrite or drop violating sentences."""
    sentences = _split_sentences(text)
    counts = {"flagged": 0, "rewritten": 0, "dropped": 0}
    result: list[str] = []

    for sentence in sentences:
        hit = _first_match(sentence)
        if hit is None:
            result.append(sentence)
            continue

        counts["flagged"] += 1
        _, rule_text = hit
        rewritten = _rewrite_sentence(sentence, rule_text, llms)

        if rewritten and _first_match(rewritten) is None:
            _log(f"REWRITE [{title[:40]}]\n  BEFORE: {sentence!r}\n  AFTER:  {rewritten!r}")
            result.append(rewritten)
            counts["rewritten"] += 1
        else:
            _log(f"DROP    [{title[:40]}]\n  SENTENCE: {sentence!r}")
            counts["dropped"] += 1
            # sentence omitted from output

    return _join_sentences(result), counts


def _first_match(text: str) -> tuple[re.Pattern, str] | None:
    """Return the first (pattern, rule_text) that matches, or None."""
    for pattern, rule_text in BANNED_PATTERNS:
        if pattern.search(text):
            return pattern, rule_text
    return None


def _rewrite_sentence(sentence: str, rule_text: str, llms: list) -> str | None:
    """Ask the LLM to rewrite a banned sentence. Returns result or None on failure."""
    prompt = (
        f'The following sentence violates a style rule: "{sentence}"\n'
        f"The rule is: {rule_text}\n"
        "Rewrite the sentence so the implication follows naturally from the facts, "
        "without using the banned construction. Keep it under 25 words. "
        "Return only the rewritten sentence — no explanation, no quotation marks."
    )
    for client in llms:
        try:
            response = client.call([{"role": "user", "content": prompt}])
            rewritten = response.strip().strip('"').strip("'").strip()
            if rewritten:
                return rewritten
        except Exception as e:
            logger.debug(f"Style rewrite LLM call failed: {e}")
    return None


def _tighten(text: str, llms: list) -> str | None:
    """Ask the LLM to rewrite a paragraph to 2 sentences, ≤60 words."""
    word_count = len(text.split())
    prompt = (
        f"This paragraph is {word_count} words, over the 60-word limit. "
        "Rewrite as exactly 2 sentences, ≤60 words total. "
        "No implication sentence. No source attribution in prose "
        "('according to…', 'TechCrunch reports…'). "
        "Preserve the lead actor and the one concrete specific (number, named thing, or quote). "
        "Return only the rewritten paragraph — no explanation.\n\n"
        f"{text}"
    )
    for client in llms:
        try:
            response = client.call([{"role": "user", "content": prompt}])
            tightened = response.strip()
            if tightened:
                return tightened
        except Exception as e:
            logger.debug(f"Style tighten LLM call failed: {e}")
    return None


def _split_sentences(text: str) -> list[str]:
    """Split on sentence-ending punctuation followed by whitespace."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _join_sentences(sentences: list[str]) -> str:
    return " ".join(sentences)


def _init_log() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _log(msg: str) -> None:
    from datetime import datetime
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
