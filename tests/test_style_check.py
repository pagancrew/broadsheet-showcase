"""
tests/test_style_check.py

Tests for tools/style_check.py.

Tests against the 2026-05-12 digest fixture and synthetic edge cases.
Run: pytest tests/test_style_check.py -v
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.style_check import (
    BANNED_PATTERNS,
    _check_and_fix,
    _first_match,
    _split_sentences,
    _join_sentences,
    run,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "digest-2026-05-12.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_llm(rewrite_fn=None):
    """Create a mock LLM that returns a rewritten sentence from rewrite_fn, or empty."""
    llm = MagicMock()
    if rewrite_fn:
        llm.call.side_effect = lambda msgs: rewrite_fn(msgs[0]["content"])
    else:
        llm.call.return_value = ""
    return llm


def _load_fixture() -> list[dict]:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

class TestBannedPatterns:
    def test_for_those_tracking_formula(self):
        text = "For those tracking the evolution of AI interfaces, this transition signals a shift."
        assert _first_match(text) is not None

    def test_for_anyone_formula(self):
        # The formula requires "this [signal verb]" in the second half
        text = "For anyone adopting AI coding assistants, this finding means verification is essential."
        assert _first_match(text) is not None

    def test_for_observers_formula(self):
        text = "For observers, this critique serves as a stark warning about the pace of deployment."
        assert _first_match(text) is not None

    def test_for_developers_formula(self):
        text = "For developers building production systems, this represents the first viable path."
        assert _first_match(text) is not None

    def test_for_researchers_formula(self):
        text = "For researchers in drug discovery, this means structural biology workflows can now include AI."
        assert _first_match(text) is not None

    def test_for_those_following_formula(self):
        text = "For those following AI governance, this signals growing appetite for regulation."
        assert _first_match(text) is not None

    def test_this_matters_because(self):
        text = "This matters because the legal frameworks were written before AI made bulk ingestion viable."
        assert _first_match(text) is not None

    def test_that_matters_because(self):
        text = "That matters because the compliance window has now closed for most organisations."
        assert _first_match(text) is not None

    def test_in_a_world_where(self):
        text = "In a world where AI voice synthesis has made audio authentication difficult, AudioSeal offers a path."
        assert _first_match(text) is not None

    def test_as_ai_continues_to(self):
        text = "As AI continues to reshape developer tooling, Willison's practical patterns stand out."
        assert _first_match(text) is not None

    def test_according_to_the_source(self):
        text = "According to the source, the model scored 90% on AIME 2026."
        assert _first_match(text) is not None

    def test_the_source_suggests(self):
        text = "The source suggests that OpenAI plans further cuts in Q3."
        assert _first_match(text) is not None

    def test_for_ai_development_tail(self):
        text = "This finding has significant implications for AI development."
        assert _first_match(text) is not None

    def test_clean_sentence_no_match(self):
        text = "Anthropic released Claude 4 with a 500k-token context window."
        assert _first_match(text) is None

    def test_clean_sentence_for_not_formula(self):
        # "for" inside a normal sentence — should not trigger
        text = "The model is designed for long-document tasks requiring sustained context."
        assert _first_match(text) is None

    def test_word_boundary_ai_keyword(self):
        # "aim" should not match "ai" keyword in _is_ai_relevant but this tests patterns
        text = "The researchers aim to publish results next quarter."
        assert _first_match(text) is None


# ---------------------------------------------------------------------------
# Fixture-level tests
# ---------------------------------------------------------------------------

class TestFixtureViolations:
    def setup_method(self):
        self.stories = _load_fixture()

    def test_fixture_loads(self):
        assert len(self.stories) == 32

    def test_at_least_20_summaries_flagged_on_first_pass(self):
        """At least 20 paragraphs in today's fixture contain a banned pattern."""
        flagged = 0
        for story in self.stories:
            summary = story.get("summary", "")
            sentences = _split_sentences(summary)
            for sentence in sentences:
                if _first_match(sentence) is not None:
                    flagged += 1
                    break  # count stories, not sentences
        assert flagged >= 20, f"Expected ≥20 flagged paragraphs, got {flagged}"

    def test_non_flagged_sentences_preserved(self):
        """Sentences without violations must be byte-identical after a run with no rewrites."""
        def null_rewrite(prompt):
            return ""  # LLM always fails — drops flagged sentence, keeps clean ones

        llm = _mock_llm(rewrite_fn=lambda p: "")
        stories_copy = json.loads(json.dumps(self.stories))  # deep copy

        result, _ = run(stories_copy, llm)

        for orig, after in zip(self.stories, result):
            orig_sentences = set(_split_sentences(orig.get("summary", "")))
            after_sentences = set(_split_sentences(after.get("summary", "")))
            clean_orig = {s for s in orig_sentences if _first_match(s) is None}
            # All originally-clean sentences must still be present
            for s in clean_orig:
                assert s in after_sentences, f"Clean sentence was unexpectedly removed: {s!r}"

    def test_zero_violations_after_rewrite(self):
        """After a successful rewrite pass, no paragraph matches any banned pattern."""
        def clean_rewrite(prompt):
            # Extract the banned sentence from the prompt and return a clean version
            return "The change represents a meaningful shift in how companies approach this problem."

        llm = _mock_llm(rewrite_fn=clean_rewrite)
        stories_copy = json.loads(json.dumps(self.stories))
        result, _ = run(stories_copy, llm)

        violations = []
        for story in result:
            summary = story.get("summary", "")
            sentences = _split_sentences(summary)
            for sentence in sentences:
                if _first_match(sentence) is not None:
                    violations.append((story.get("title", ""), sentence))

        assert violations == [], f"Remaining violations after rewrite: {violations}"


# ---------------------------------------------------------------------------
# Rewrite prompt isolation tests
# ---------------------------------------------------------------------------

class TestRewritePromptIsolation:
    def test_rewrite_prompt_contains_only_flagged_sentence_and_rule(self):
        """The rewrite prompt must contain the flagged sentence and the rule — not the whole paragraph."""
        captured_prompts = []

        def capture_llm(prompt):
            captured_prompts.append(prompt)
            return "The change represents a meaningful shift for developers."

        llm = _mock_llm(rewrite_fn=capture_llm)

        banned_sentence = "For those tracking the evolution of AI interfaces, this transition signals a shift."
        clean_sentence = "Anthropic released Claude 4 with a 500k-token context window."
        paragraph = f"{clean_sentence} {banned_sentence}"

        story = [{"id": 0, "title": "Test", "summary": paragraph}]
        run(story, llm)

        assert len(captured_prompts) >= 1
        prompt = captured_prompts[0]
        assert banned_sentence in prompt
        assert clean_sentence not in prompt, "Rewrite prompt must not contain the whole paragraph"


# ---------------------------------------------------------------------------
# Drop-on-failure tests
# ---------------------------------------------------------------------------

class TestDropOnFailure:
    def test_sentence_dropped_if_rewrite_still_banned(self):
        """If the LLM returns another banned sentence, the original must be dropped."""
        def return_banned(_prompt):
            return "For those tracking AI, this matters because the stakes are high."

        llm = _mock_llm(rewrite_fn=return_banned)
        banned = "For those tracking the evolution of AI, this signals a fundamental change."
        story = [{"id": 0, "title": "Test", "summary": f"Anthropic released Claude 4. {banned}"}]

        result, _ = run(story, llm)
        summary = result[0]["summary"]
        assert _first_match(summary) is None, f"Banned pattern survived drop: {summary!r}"
        assert "Anthropic released Claude 4" in summary, "Clean sentence should remain"

    def test_sentence_dropped_if_llm_fails(self):
        """If all LLMs fail (return empty), the banned sentence must be dropped."""
        llm = _mock_llm(rewrite_fn=lambda _: "")
        banned = "For observers, this critique serves as a stark warning."
        story = [{"id": 0, "title": "Test", "summary": f"Google cut Gemini pricing. {banned}"}]

        result, _ = run(story, llm)
        summary = result[0]["summary"]
        assert _first_match(summary) is None
        assert "Google cut Gemini pricing" in summary


# ---------------------------------------------------------------------------
# Specific banned phrase tests
# ---------------------------------------------------------------------------

class TestSpecificPhrases:
    def _run_single(self, text, rewrite_return="Clean rewritten sentence."):
        llm = _mock_llm(rewrite_fn=lambda _: rewrite_return)
        story = [{"id": 0, "title": "Test", "summary": text}]
        result, _ = run(story, llm)
        return result[0]["summary"]

    def test_for_those_tracking_rewritten(self):
        text = "Anthropic shipped Claude 4. For those tracking AI interfaces, this signals a shift."
        result = self._run_single(text)
        assert _first_match(result) is None

    def test_in_a_world_where_rewritten(self):
        text = "Meta released AudioSeal. In a world where AI voice synthesis complicates audio auth, this helps."
        result = self._run_single(text)
        assert _first_match(result) is None

    def test_this_matters_because_rewritten(self):
        text = "OpenAI cut pricing by 40%. This matters because production AI features are now affordable."
        result = self._run_single(text)
        assert _first_match(result) is None

    def test_as_ai_continues_rewritten(self):
        text = "Simon Willison published an LLM CLI guide. As AI continues to reshape tooling, this is useful."
        result = self._run_single(text)
        assert _first_match(result) is None


# ---------------------------------------------------------------------------
# Inverted-formula tests (6th BANNED_PATTERNS entry — Brief 18)
# All 20 violations in the 2026-05-14 digest used this word-order flip to
# evade the existing "For those…" regex.
# ---------------------------------------------------------------------------

class TestInvertedFormula:
    """Assert the inverted 'This X for/to [actor]' pattern is caught."""

    def test_this_is_relevant_for_anyone(self):
        text = "This is relevant for anyone assessing the long-term operational costs of AI infrastructure."
        assert _first_match(text) is not None

    def test_this_shift_matters_to_those(self):
        text = "This shift matters to those tracking how companies manage the tension between privacy and AI capability."
        assert _first_match(text) is not None

    def test_this_is_a_signal_to_those(self):
        text = "This is a signal to those watching how companies are embedding AI across the browser layer."
        assert _first_match(text) is not None

    def test_this_matters_for_those(self):
        text = "This matters for those tracking the governance challenges of AI-generated content delivery."
        assert _first_match(text) is not None

    def test_this_is_critical_development_for_those(self):
        text = "This is a critical development for those monitoring how alignment strategies are evolving."
        assert _first_match(text) is not None

    def test_this_is_vital_for_those(self):
        text = "This is vital for those tracking the gap between marketing claims and measurable health impact."
        assert _first_match(text) is not None

    def test_this_friction_matters_for_anyone(self):
        text = "This friction matters for anyone attempting to discern the real priorities of AI labs."
        assert _first_match(text) is not None

    def test_this_list_is_essential_for_anyone(self):
        text = "This list is essential for anyone building applications that require fast local inference."
        assert _first_match(text) is not None

    def test_this_is_useful_for_anyone(self):
        text = "This is useful for anyone looking to incorporate AI tools into existing data pipelines."
        assert _first_match(text) is not None

    def test_this_is_development_of_note_for_anyone(self):
        text = "This is a development of note for anyone following the commoditisation of large language models."
        assert _first_match(text) is not None

    def test_clean_sentence_not_flagged(self):
        # Normal "for" usage — no actor group, no banned formula
        text = "The model is optimised for long-document tasks requiring sustained context."
        assert _first_match(text) is None

    def test_factual_sentence_not_flagged(self):
        # Two-sentence factual summary — must not be caught
        text = (
            "Anthropic released Claude 4 with a 500k-token context window and improved tool use. "
            "The model scores 23% higher than its predecessor on mathematical reasoning benchmarks."
        )
        assert _first_match(text) is None

    def test_inverted_rewritten_after_run(self):
        """An inverted-formula sentence must be rewritten or dropped by run()."""
        llm = _mock_llm(rewrite_fn=lambda _: "Anthropic's new model reduces inference cost by 40%.")
        banned = "This is a significant efficiency gain for researchers aiming to maximise throughput."
        story = [{"id": 0, "title": "Test", "summary": f"Nous Research shipped Token Superposition. {banned}"}]
        result, _ = run(story, llm)
        assert _first_match(result[0]["summary"]) is None


# ---------------------------------------------------------------------------
# Word-count tightening tests (recalibrated to ≤60-word limit — Brief 18)
# ---------------------------------------------------------------------------

class TestWordCountTightening:
    # 65-word summary with no banned patterns — should trigger tightening
    _LONG_SUMMARY = (
        "Anthropic released Claude 4 this week, a flagship large language model designed for complex "
        "reasoning tasks across enterprise applications. The model features a 500,000-token context "
        "window, allowing it to process entire codebases in a single pass. It supports multi-step "
        "tool use with improved reliability over prior versions and adds native support for structured "
        "output. Researchers at Stanford independently confirmed a 23% improvement on mathematical "
        "reasoning benchmarks."
    )
    _SHORT_RESULT = (
        "Anthropic released Claude 4, featuring a 500,000-token context window and improved "
        "multi-step tool use. Stanford researchers confirmed a 23% improvement on mathematical "
        "reasoning benchmarks."
    )

    def test_long_summary_is_over_60_words(self):
        assert len(self._LONG_SUMMARY.split()) > 60

    def test_short_result_is_under_60_words(self):
        assert len(self._SHORT_RESULT.split()) <= 60

    def test_tightening_fires_at_60_words(self):
        """A >60-word summary must be passed to the LLM tightener."""
        tighten_called = []

        def capture_tighten(prompt):
            tighten_called.append(prompt)
            return self._SHORT_RESULT

        llm = _mock_llm(rewrite_fn=capture_tighten)
        story = [{"id": 0, "title": "Test", "summary": self._LONG_SUMMARY}]
        result, _ = run(story, llm)

        assert tighten_called, "Tightening LLM was not called for a >60-word summary"
        assert len(result[0]["summary"].split()) <= 60

    def test_tightening_does_not_fire_at_60_words(self):
        """A summary at exactly the limit must not be passed to the tightener."""
        # Build a 60-word summary with no banned patterns
        sixty_words = " ".join(["word"] * 59) + " end."
        assert len(sixty_words.split()) == 60

        tighten_called = []

        def capture_tighten(prompt):
            tighten_called.append(prompt)
            return "should not be called"

        llm = _mock_llm(rewrite_fn=capture_tighten)
        story = [{"id": 0, "title": "Test", "summary": sixty_words}]
        run(story, llm)

        assert not tighten_called, "Tightening fired on a ≤60-word summary"
