# Design Decisions — Broadsheet

## 2026-06-04 — Session 43

### ADR-051: Selection pipeline fixes — dedup-before-budget, heuristic starvation, label collision, Guardian-via-Tavily, must_include window (Brief 21)

**Fix 1 — Dedup before budget (newsroom_lead.py).**
`_semantic_dedup` now runs per-category on the `passing` pool *before* the budget loop. Previously it ran after, so near-duplicate events consumed the full per-category cap and freed slots were never reclaimed. A second dedup pass is kept post-selection as a safety net for pinned-story conflicts.

**Fix 2 — All categories onto editorial heuristic scoring (newsroom_lead.py).**
Added `big_news` and `builder` to `_EDITORIAL_CATS`. All four categories now use the editorial scoring formula (`recency + affinity * tag_w + source_bonus`) with no engagement dependency. The engagement formula is dead code but kept for reference. Side effect: `big_news` and `builder` now use `llm_rerank_top_n_editorial: 20` instead of the standard `12`, which was the separate config change the brief had proposed — it happened automatically.

**Fix 3 — Label normalization at ingestion (inbox_fetcher.py, main.py).**
Substack sources in `sources.yaml` use display labels ("Commentary", "Big News", "Built & Released") as their `category` field. The budget loop only recognises internal keys (`critical_voices`, `big_news`, `builder`, `laws_ethics`), so display-labelled stories formed phantom categories that escaped per-category caps. Added `_DISPLAY_TO_INTERNAL` map and `_normalize_category()` in `inbox_fetcher.py`; applied at story ingestion. Display labels are now only used at render time.

**Fix 4 — Guardian routed via Tavily; zero-yield visibility (search_agents.py, source_monitor.py, sources.yaml).**
The Guardian API was returning HTTP 200 but zero results because its query over-constrained `section=technology AND tag=law/law` (those two rarely co-occur). "Success" was logged on a 200 even with an empty result set — a silent failure. Decision: drop the bespoke Guardian API path entirely; add `theguardian.com` as a `no_feed_sites` entry under `laws_ethics` so it is fetched via the same Tavily news-mode pipeline every other no-feed source uses. Added `record_zero_yield()` to `source_monitor.py` and zero-yield tracking to all four `no_feed_sites` loops in `search_agents.py` so sources that fetch OK but return 0 stories surface in alerts after 2 consecutive silent runs.

**Fix 5 — must_include window raised to 72h (newsroom_lead.py, digest.yaml).**
`MUST_INCLUDE_HOURS = 48` was hardcoded. Promoted to `digest_cfg.get("must_include_max_hours", 48)` and set to 72 in `digest.yaml`. Curated author posts arriving late via Tavily or with slightly-off timestamps were falling out of the pin path into the normal pool.

---

## 2026-06-03 — Session 42

### ADR-050: Tavily queries rewritten — operators out of strings, into params (Brief 20); Critical Voices renamed "Commentary"
**Decision**: Executed Brief 20 as a coaching session and rewrote every `tavily_queries` and `no_feed_sites` query in `config/sources.yaml` from operator-laden keyword strings into plain natural-language phrases. All `OR` / `"quotes"` / `site:` operators were removed. Source-anchoring now lives entirely in `include_domains` (via the `no_feed_sites` mechanism); recency lives in the global `topic="news"` + `days=` already applied at the call sites. Topic-anchored queries follow "one intent per query" (Law & Ethics 4→8, Commentary 1→5); source-anchored `no_feed_sites` queries use `include_domains` + a light multi-aspect phrase (the domain carries the precision). Also: the Builder category was refocused from the "Show HN" project firehose (Hacker News paused) onto practitioner guidance / tooling / model comparisons, adding Nvidia, Cursor, Groq, Artificial Analysis, and Epoch AI as `no_feed_sites`; The Batch moved to Commentary; Mozilla.ai moved from Big News to Law & Ethics. The Critical Voices **display label** was renamed to "Commentary" (`config/digest.yaml` `category_labels` + the `tavily_substack` display strings) — the internal key `critical_voices` is unchanged, so no code paths move.
**Why**: Brief 14 (Settled 2026-05-18) codified a `site:`/`OR` query style, but Tavily does semantic retrieval and does not reliably parse those operators — the literal tokens get embedded into the query's meaning and blur it (e.g. a name-OR list matches old listicles *about* those people rather than fresh writing *by* them). This brief supersedes Brief 14's *mechanism* (operators in the string) while keeping its *instinct* (anchor by domain / object-type / person). Prerequisite ADR-049 (news mode) had to land first, or even well-formed queries would return relevance-ranked evergreen results. Config-only by design: of Tavily's param table, the code currently wires `include_domains` + `topic` + `days`; `exclude_domains` / `time_range` / `country` / `search_depth` were not needed by any stated intent and remain a future code brief. Live validation (2026-06-03): the rewritten topic queries returned 4/4 fresh, on-intent results in both Big News and Law & Ethics; the benchmark sites (Artificial Analysis/Epoch AI) returned some evergreen pages, which the downstream `recency_cutoff` handles. Category caps were deliberately held at 8 (+2 rollover) pending real-run evidence rather than raised on a guess.

---

## 2026-06-03 — Session 41

### ADR-049: `topic="news"` added to all Tavily calls; ADR-043 was a no-op
**Decision**: `search()` in `tools/tavily_search.py` now passes `topic="news"` unconditionally. Because all call sites route through `search()` — the 8 queries in `agents/search_agents.py` via `run_queries()` / `search_site()`, and the Substack path in `tools/inbox_fetcher.py` via `search_site()` — this single-parameter addition covers the entire codebase.
**Why**: Tavily's `days=` recency window and reliable `published_date` field are only honored in `topic="news"` mode. The default is `topic="general"`, where `days=` is silently ignored and `published_date` is unreliable. ADR-043 (Session 36) added `days=` arguments at all call sites while leaving `topic` unset, so those arguments had no effect. All recency filtering continued to fall to the post-hoc 14-day cutoff in `newsroom_lead.py`. The same bug caused trusted-voice Substack posts to arrive undated, be dropped at the recency cutoff, and never reach the `must_include` pin in ADR-045. This ADR does not rewrite or delete ADR-043 — the `days=` arguments it added are now actually enforced by the news-mode fix.

---

## 2026-05-27 — Session 39

### ADR-048: Tavily quota errors excluded from source-failure tracking
**Decision**: When Tavily returns a credit-limit error, `record_failure()` is no longer called. The `quota_exceeded` flag (drives the top-bar banner) and the warning log line are kept. A backstop filter in `get_alerts()` additionally skips any entry whose `last_error` starts with `[TAVILY-QUOTA]`, handling legacy state from before this fix.
**Why**: Quota exhaustion is a billing event, not a source failure. Recording it as a source failure had two bad effects: (1) it flooded the ⚠ Source Alerts section with noise — 10+ identical "credit limit reached" lines that told readers nothing they didn't already know from the top banner; (2) it inflated `consecutive_failures` counts for Tavily queries, threatening to permanently mark them "🔴 Likely dead" after 3 monthly credit cycles. The distinction between "this source is broken" (actionable) and "we ran out of budget" (already surfaced in the run report) deserves to be preserved in the failure-tracking layer.

---

## 2026-05-26 — Session 37

### ADR-046: Fallback-model banner removed rather than updated
**Decision**: The yellow `fallback_model_used` banner in email HTML and plaintext was deleted entirely, not reworded. The `fallback_model_used` parameter was removed from the `email_sender` call chain (`send_digest`, `_build_html`, `_build_plaintext`). It remains in `agents/editor.py` where it is used to inject a note into the writing prompt.
**Why**: The banner cited Nemotron/build.nvidia.com, providers removed months ago. Rewording it was the initial plan, but the run-report strip immediately above it already names the actual scoring and summary models — so the banner was redundant as well as wrong. Removing it reduces noise and eliminates a class of stale-text bug: there is no longer a second place to update when the model chain changes.

### ADR-047: Proactive Tavily usage display in run-report strip; end-of-run only; display-only
**Decision**: `get_usage()` in `tools/tavily_search.py` calls `GET https://api.tavily.com/usage` once per run, after the gather phase. The result (`NN% used (X/Y)`) is inserted into `run_report["tavily"]` and rendered as a third field in the run-report strip alongside Scoring and Summaries. The reactive "quota reached" note (previously in `run_report["sources"]`) is folded into the same field and takes priority when searches were actually skipped. No auto-skip and no ⚠ prefix on normal usage — display only.
**Why**: Tavily emailed an 80% credit warning that went unnoticed until digest quality degraded. A daily display of the burn-rate makes the trend visible before it becomes a problem. End-of-run timing was chosen over start-of-run (the check captures actual post-run usage, not a stale pre-run snapshot). Display-only was chosen over auto-skip to keep behaviour predictable — the operator can decide whether to pause Tavily manually rather than having the pipeline do it silently at a hardcoded threshold.

---

## 2026-05-22 — Session 36

### ADR-040: Recency enforcement extended to all categories; Builder exemption removed
**Decision**: The 14-day recency cutoff in `newsroom_lead.py` now applies to all four categories. Previously, Builder category was exempt (`if cat not in NEWS_CATS: fresh.append(s); continue`), allowing old "I built X" posts and months-old builder content to reach the digest without date checking. GitHub Trending entries are now stamped with the scrape time rather than `None`.
**Why**: The Builder exemption was the most likely path for the "several months old" story reported on 2026-05-22. Extending the cutoff uniformly is the right principle — all content should be dateable and within window. GitHub Trending "trending now" repos are by definition fresh regardless of repo creation date, so stamping with scrape time is semantically accurate.

### ADR-041: Parse-failure-keeps-story bug fixed; dateutil added as fallback
**Decision**: The recency cutoff `except (ValueError, TypeError): fresh.append(s)` branch replaced with a drop. A second parse attempt via `dateutil.parser.parse()` is made before giving up.
**Why**: The `except: fresh.append` behaviour was the inverse of the stated intent ("drop undated news articles" per commit ce20652). Tavily returns many date formats that `fromisoformat` rejects (`"March 15, 2024"`, `"15/03/2024"`) — these were silently passing through. `dateutil` handles all common formats with no new dependency (ships with `feedparser`).

### ADR-042: Four-tier date extraction in `tools/tavily_search.py`
**Decision**: `_extract_published()` helper tries: (1) strict ISO, (2) dateutil, (3) URL-embedded date regex `/YYYY/MM/DD/`, (4) content-text date regex against the first 500 chars of the article body.
**Why**: Tavily's `published_date` field is unreliable — often `None` or in a non-ISO format. URL and content patterns recover dates for the vast majority of real publisher URLs and article openings. This is a pure data-quality fix with no observable behaviour change for stories that have a clean ISO date.

### ADR-043: Tavily `days=` window enforced at all call sites
**Decision**: All 8 Tavily call sites in `agents/search_agents.py` now pass `days=3` (open queries) or `days=2` (no_feed_sites). `run_queries()` updated to accept and forward `days=`.
**Why**: Brief 14 (settled 2026-05-18) specified `days=2` for no_feed_sites but the code change was never made. Without a recency window, Tavily returns whatever it ranks best — including evergreen SEO content from 2023. This was the primary source of "years old" articles flooding the candidate pool.

### ADR-044: Persistent rejected-URL memory via Notion Candidates DB
**Decision**: `read_rejected_urls(days_back=30, reasons=["recency_cutoff"])` queries the Candidates DB for previously-dropped stale URLs and filters them out before the heuristic/LLM funnel.
**Why**: The same evergreen URL was re-entering the funnel every run (cross-day dedup only tracks published URLs). Remembering `recency_cutoff` drops for 30 days stops stale content from spending gather + LLM-rerank tokens repeatedly. `confidence_floor` and `not_selected` are intentionally excluded — those are quality judgements that should be re-made fresh each day.

### ADR-045: Trusted-voice Substack pin
**Decision**: `priority: must_include` added to all 9 named-individual Substack sources. Fresh posts (≤48h) are pinned into the digest before the normal funnel. Cap: 1 pin per byline. Protected through semantic dedup (pinned story always wins its cluster).
**Why**: Named individuals (Gary Marcus, Nathan Lambert, Swyx, Zvi Mowshowitz, Sebastian Raschka, Edward Zitron, Brian Merchant, L.M. Sacasas, Jack Clark) are curated for editorial quality. A fresh post from these voices should appear in the newsletter unconditionally — per-source caps, confidence floors, and category budgets should not gate them. The 1-per-byline cap prevents an unusually prolific day from flooding the digest.

## 2026-05-14 — Session 33

### ADR-038: Editor re-brief — pure factual summary, no implication tail
**Decision**: `editor_agent` in `config/agents.yaml` is re-briefed to produce a 2-sentence factual summary (≤60 words) per story. The implication/why-it-matters sentence is explicitly forbidden. Source attribution is removed from prose. The banned-phrases list is removed from the prompt; the regex layer (`tools/style_check.py`) is the enforcement mechanism per ADR-032.
**Why**: The 2026-05-14 digest had 26 stories and every one ended with an editorialising "why it matters" sentence — the root cause was `agents.yaml:188` ("End with a single sentence on why the owner might care"). The editor also learned to write the banned `For those…, this signals…` formula in inverted word order (`This is relevant for anyone…`) that evaded the existing regex. Re-briefing the editor away from implications entirely removes the failure mode at source rather than patching more regex. Per Gemini 3.1 Flash-Lite best practices: ##-delimited sections, measurable constraints (not subjective), one small example. Prompt shrinks from ~70 to ~40 lines.

### ADR-039: Inverted-formula regex added as 7th BANNED_PATTERNS entry
**Decision**: `tools/style_check.py` adds a safety-net regex for `This [word(s)] for/to [actor]` — the inverted form of the already-banned `For those tracking X, this signals Y` formula.
**Why**: 20 of 26 stories in the 2026-05-14 digest used the inverted word order to pass the existing `^For` regex. The new editor prompt no longer asks for implications, so this regex should rarely fire; it exists to catch slippage. Pattern is intentionally wide (`\w+(?:\s+\w+){0,5}?`) — if false positives appear on legitimate sentences, narrow to a verb shortlist (`is|signals|matters|highlights|suggests|…`). `_WORD_LIMIT` also dropped from 90 → 60 to match the new ≤60-word target.

---

## 2026-05-14 — Session 32

### ADR-036: Session-end protocol — explicit ship-to-main, no PR ceremony
**Decision**: `CLAUDE.md` "Ending a session" is rewritten as a six-step ritual: record work → commit → verify → sync+fast-forward-merge+push → cleanup worktree+branch → state plain-English result. Direct merge into `main` (not via PR). `git branch -d` (not `-D`) at cleanup as the trip-wire.
**Why**: Session 31 exposed the structural failure mode — work committed on a `claude/<name>` branch but never reaching `origin/main`, invisible to GitHub Actions. The previous protocol ended at "Commit with descriptive message" and assumed the rest. Direct merge (over PR) keeps the ritual short enough that a session reliably completes it for solo work; PR ceremony was rejected as friction without a review surface to justify it. `--ff-only` refuses to silently create a merge commit on divergence; `-d` (not `-D`) refuses to delete an unmerged branch, so a skipped step fails loudly instead of erasing work.

### ADR-037: Workflow enshrined in CLAUDE.md, not a standalone doc or script
**Decision**: The session-end ritual lives in the project's `CLAUDE.md`. No `docs/WORKFLOW.md`, no `scripts/end-session.sh`.
**Why**: `CLAUDE.md` is read by every Claude instance at session start; a separate doc is only consulted if explicitly referenced. A helper script was rejected as premature — adding tooling before the manual protocol is reliably followed risks hiding skipped steps behind broken automation. Revisit only if the manual protocol fails to hold.

---

## 2026-05-14 — Session 31

### Process note — Brief 17 delivery gap
Brief 17 was fully implemented (36/36 tests) and pushed to `origin/claude/suspicious-euclid-207164` on 2026-05-13 (Session 30), but the session closed without merging into `main`. Today's scheduled run executed `main` (Brief 16), so none of the run-log or email-link features were live. Fix: fast-forward merge to main in Session 31.
**Lesson**: Merge feature branches to `main` before closing a session. A branch that is pushed but not merged delivers nothing to the scheduled workflow.

---

## 2026-05-12 — Session 29

### ADR-032: Style enforcement is deterministic regex + surgical rewrite, not pure LLM
**Decision**: Banned phrases are caught by compiled regex patterns and fixed with a minimal targeted LLM prompt (flagged sentence + rule → rewritten sentence). If the rewrite still matches, the sentence is dropped entirely. The Editor's prose instructions are unchanged.
**Why**: The Editor already had an explicit blacklist in agents.yaml that the LLM was ignoring. Adding more emphatic prompt language wouldn't change this — LLMs apply soft constraints inconsistently across 30 stories. A regex pass after the fact is O(1) and guaranteed. The surgical rewrite approach (not the whole paragraph) minimises hallucination risk and preserves the Editor's voice for all non-violating content.

### ADR-033: Per-category hard caps replace shared soft budget
**Decision**: `category_max` in digest.yaml gives each category a hard cap (big_news: 5, others: 8) with `rollover_max: 2` extra slots drawn from a shared leftover pool.
**Why**: The previous model gave laws_ethics and critical_voices a shared soft budget with uncapped rollover — critical_voices hit 14 on 2026-05-12 because it had more high-scoring stories than laws_ethics. Hard caps per category guarantee balanced coverage. Rollover_max: 2 preserves flexibility on strong news days without allowing any category to dominate.

### ADR-034: ai_only_filter at ingest rather than removing whole-site feeds
**Decision**: Bellingcat, Less Wrong, and 404 Media get `ai_only_filter: true` in sources.yaml. Non-AI items are dropped at fetch time by keyword match. The feeds stay enabled.
**Why**: These feeds are editorially valuable when they cover AI — 404 Media's AI accountability reporting and LessWrong's alignment discussion are exactly what critical_voices needs. Removing them would lose that signal. A keyword filter at ingest is a cheaper and more tunable solution than LLM-based relevance scoring, and the `logs/ingest_filter.log` makes over-filtering observable.

### ADR-035: Paywall preference applies only inside semantic dedup clusters
**Decision**: No global confidence_score penalty for paywalled stories. Paywall preference fires only when two stories in the same semantic cluster compete — the non-paywalled sibling wins if it's within 1 point.
**Why**: The summary written by the Editor is the value delivered to subscribers — the paywall only matters when clicking through. Penalising paywalled stories globally would lose high-quality content (Wired, FT) on days when they're the only source for a story. The cluster-only rule catches the real problem (two sources covering the same event, one paywalled) without degrading coverage depth.

---

## 2026-05-07 — Session 24

### ADR-029: Editorial categories use recency/affinity-only heuristic scoring
**Decision**: `critical_voices` and `laws_ethics` stories are scored on recency + tag affinity + source bonus only. Authority (source_weight), engagement (HN/Reddit/GitHub), and cluster bonus are omitted.
**Why**: The full heuristic rewards signals that correlate with mainstream attention — social upvotes, outlet authority, multi-source coverage. Editorial sources (404 Media, EFF, Cory Doctorow, LessWrong) rarely generate these signals and were systematically underscored relative to their actual editorial value. Recency and tag affinity are more meaningful proxies for these categories.

### ADR-030: Per-category heuristic cutoff replaces single global pool
**Decision**: Each category gets its own top-N cutoff before the LLM rerank. Editorial categories get 20 slots; standard categories get 12. The LLM sees ~64 candidates total.
**Why**: A single ranked pool meant editorial categories competed against big_news stories on signals they couldn't win (engagement, authority). Even with better scoring, a low-engagement critical-voices story at score 1.5 would be cut in favour of a big_news story at score 3.0. Per-category cuts guarantee representation at the LLM stage regardless of cross-category score differences. The post-LLM budget allocation already handles final balance.

### ADR-031: Builder sources paused selectively, not category-wide
**Decision**: HN, Reddit, GitHub Trending, Product Hunt, arXiv, and most blog feeds disabled. Marktechpost, Simon Willison, and Andrej Karpathy remain active.
**Why**: Reddit and arXiv were producing high volumes of low-editorial-value content (Reddit reposts, paper abstracts) that absorbed builder category slots. Marktechpost and the named blogs produce curated, editorial content consistent with the digest's voice. The pause is reversible — sources are disabled with `enabled: false` rather than removed.

---

## 2026-05-07 — Session 23

### ADR-024: Diversity floor matches min_confidence_score (both = 6)
**Decision**: `diversity_min_quality` raised from 4 to 6, matching `min_confidence_score`. Both values read from `digest.yaml` so they stay in sync.
**Why**: The original floor of 4 allowed the diversity slot and source de-dupe mechanisms to select stories that would not survive ordinary selection (confidence < 6). This caused perverse outcomes where a confidence-5 story entered via diversity slot while a confidence-9 story was displaced by source-dupe logic. Aligning the floors means diversity mechanisms can only rescue or displace stories that would be considered acceptable by the main filter.

### ADR-025: Source de-dupe gap threshold = 2 confidence points
**Decision**: A duplicate-source story is only displaced if the best available replacement is within 1 confidence point (gap < 2). A dup that scores 2+ points higher than any replacement survives.
**Why**: The old rule displaced any story if any alternative existed at quality ≥ floor, regardless of quality difference. This discarded genuinely better stories in favour of source diversity. The gap threshold preserves diversity as the default while protecting clearly superior stories from being lost to an inferior replacement.

### ADR-026: big_news and builder hard-capped; laws_ethics/critical_voices share rollover
**Decision**: `big_news` and `builder` get exactly `base_budget` slots — no contribution to or from the leftover pool. `laws_ethics` and `critical_voices` share their combined remaining budget.
**Why**: `big_news` and `builder` tend to produce many high-confidence stories, so their excess was dominating the leftover pool and crowding out the editorial categories. Hard-capping them keeps the digest balanced. `laws_ethics` and `critical_voices` tend to have leaner days and benefit from sharing budget between themselves.

### ADR-027: Paywall detection via static domain list, not dynamic fetch
**Decision**: Paywalled stories are flagged by matching the story URL against a user-maintained list in `digest.yaml`. No HTTP fetch to detect paywalls dynamically.
**Why**: Dynamic detection would require fetching each story URL at scrape time — slow, quota-expensive, and unreliable (paywalls often show content to crawlers). A static list of known domains is accurate for the sites that matter and takes seconds to maintain. New paywalled sources are noticed quickly in use and can be added in one line.

### ADR-028: Candidates DB expanded to all raw stories; cleared daily
**Decision**: `write_candidates_log()` now logs every story from all four agents before any filtering, and clears the database at the start of each run rather than purging rows >30 days.
**Why daily clear vs retention**: The database is a monitoring tool for the current day's pipeline, not a historical record. Keeping 30 days of top-45 candidates was growing the DB to 1,350 rows while providing little diagnostic value for past runs. A daily snapshot of all raw stories (~150-200 rows) is more useful and stays manageable.
**Why all raw stories, not just top-45**: The top-45 were already past the heuristic filter — invisible in Notion were all the stories eliminated by URL dedup, source cap, clustering, and the cutoff itself. Logging everything with a `Stage Dropped` column makes the full selection cascade visible.

---

## 2026-05-06 — Session 22

### ADR-020: Abandon 👍/👎 voting; use title click-through as sole feedback signal
**Decision**: Remove the CSS `:checked` + background-image ping voting mechanism entirely. Title click-through (`v=top`) is the only feedback signal.
**Why**: Two independent failure modes made voting unreliable in practice: (1) Apple Mail Privacy Protection pre-fetches all URLs in an email at open time, which fired both vote URLs simultaneously and triggered the MPP guard to discard both; (2) Gmail strips `<style>` blocks entirely, making the hidden-checkbox `:checked` trigger invisible, so votes in Gmail required clicking a fallback link that opened a browser tab — noisy and rarely used. The title click-through works silently in every client because it's a plain `<a href>`, not CSS-triggered.
**Trade-off**: Less granular signal — no downvote, no "less of this". Offset by the behavioural strength of the signal: clicking a story title to read it is a stronger positive signal than a deliberate thumbs-up tap.

### ADR-021: Source affinity as a separate per-run decaying score (not calendar-based)
**Decision**: Source affinity accumulates as a YAML float [0.0–1.0] in `calibration.yaml`. Decay is per-run (×0.978), not per calendar day.
**Why per-run, not per-day**: Broadsheet run frequency can vary (manual triggers, skipped runs). Per-run decay is simpler and more predictable — a source that gets clicked consistently over ten consecutive runs earns its bonus regardless of whether those runs happened over one week or three. Per-calendar-day decay would require storing last-run timestamp and computing elapsed days.
**Why a separate score, not tag weights**: Tag weights aggregate topic-level preference across all sources. Source scores capture source-level quality independent of topic. A source that consistently produces clicked stories across different topic tags deserves a bonus even on days when it covers less-preferred topics.
**Decay rate rationale**: ×0.978/run ≈ ×0.85/week at daily cadence. A source with score 0.5 (5 clicks, never decayed) falls to ~0.25 after one week of no clicks and ~0.07 after three weeks. This means a source needs sustained engagement to maintain its bonus — prevents one lucky week from permanently boosting a mediocre source.

### ADR-022: Content filters in digest.yaml, not agents.yaml
**Decision**: `blocked_topics` and `deprioritized_sources` are structured config in `digest.yaml`, not prose instructions in `agents.yaml`.
**Why**: Agent prose in `agents.yaml` is interpreted by the LLM — it's approximate and can be overridden by strong story scores. Structured filters in `digest.yaml` are applied deterministically before the LLM sees anything. A blocked keyword is a hard exclude; a deprioritised source is a guaranteed penalty. This separation also keeps `agents.yaml` focused on editorial voice and lets the user tune content exclusions without risking accidental changes to agent behaviour.
**Why penalty not hard-block for sources**: Source names are not always clean (Tavily returns hostnames, RSS returns display names). A -2.0 penalty ensures deprioritised sources survive only if nothing better exists — they're suppressed, not silently lost.

### ADR-023: Candidate log in Notion, not local JSON
**Decision**: The heuristic top-45 candidates are written to a Notion database after each run, not to a local JSON file.
**Why not local JSON**: GitHub Actions runners are ephemeral — local files disappear after the run. The JSON would only be accessible by downloading it from the Actions artifacts UI, which requires navigating GitHub and waiting for an upload step to complete.
**Why Notion**: Accessible from anywhere (phone, browser, desktop) without logging into GitHub. Filterable, sortable, and queryable. The user already uses Notion daily for the digest delivery and feedback — no new tool required.
**Retention**: 30-day auto-purge at the start of each run keeps the database under ~1,350 rows (45 candidates × 30 days).

---

## 2026-04-11 — Session 1

### ADR-001: Notion database over Notion pages
**Decision**: Store each story as a row in a Notion database, not as content on a page.  
**Why**: Database rows have typed properties (including a Select field for feedback emoji). This enables the feedback loop — user taps 👍/👎/⭐ in Notion mobile, agent reads it next run.  
**Consequence**: User must create the database once (schema in README). After that, it's automatic.

---

### ADR-002: CrewAI as agent framework
**Decision**: Use CrewAI for the multi-agent pipeline.  
**Why**: Fastest to prototype with role-based agents; works with any OpenAI-compatible API endpoint (needed for Nemotron). LangGraph is more powerful but overkill for a deterministic sequential pipeline.  
**Future**: Migrate to LangGraph if we need branching logic, retry loops, or streaming.

---

### ADR-003: Nemotron via NVIDIA NIM + OpenRouter fallback
**Status**: ⚠ Superseded by ADR-014 (2026-05-01). NVIDIA NIM credits were a one-time grant; OpenRouter free pool was unreliable. Both replaced.  
**Decision**: Primary LLM is Nemotron via NVIDIA NIM. Fallback is Llama 3.3 70B via OpenRouter.  
**Why**: Owner specifically wanted to try Nemotron. NVIDIA NIM gives 1000 free credits. OpenRouter has a genuinely free tier with capable models.  
**Mechanism**: On 429 or credit-exhaustion errors, code auto-switches and logs the event. Digest header includes "⚠ Running on fallback model today" when this happens.

---

### ADR-004: Dual cron for London time
**Decision**: Two GitHub Actions cron triggers (`0 7 * * *` and `0 6 * * *`) with a concurrency guard.  
**Why**: GitHub Actions cron is UTC-only. London alternates between GMT (UTC+0) and BST (UTC+1). The concurrency guard (using GitHub Actions `concurrency` with `cancel-in-progress: false` and a skip-if-ran-today check) ensures only one run happens per calendar day.  
**Alternative considered**: Single cron at 6am UTC (always runs, but 6am in winter instead of 7am). Rejected — owner specified 7am London.

---

### ADR-005: Source failure visibility in digest
**Decision**: Source failures appear in the digest itself (Notion + email), not just in logs.  
**Why**: Owner needs to know when a source drops out. Silent log failures would go unnoticed for days.  
**Threshold**: After 3 consecutive failures, source is flagged as "likely dead". Owner can remove it from `config/sources.yaml`.

---

### ADR-006: No arbitrary story count limit
**Decision**: EditorAgent decides how many stories to include based on quality.  
**Why**: Owner explicitly rejected arbitrary limits (3/5/10/15). Quality over completeness.  
**Control**: `config/digest.yaml` has a `story_count_guidance` field where owner can express preferences in plain English (e.g., "prefer depth over breadth", "no more than 20 total").

---

### ADR-007: Mobile feedback — Notion database (not bot)
**Decision**: Feedback is via the Notion mobile app, not a Telegram/Discord bot.  
**Why**: No extra service to set up. Notion mobile is already the reading interface.  
**Trade-off**: Notion requires 2-3 taps vs. a single emoji tap on a bot message. Acceptable for now.  
**Future upgrades** (documented, not built):
- **Discord bot** (`discord.py`): Best alternative. Free, excellent emoji reactions, mobile-optimised. Bot sends digest summary, owner reacts, bot reads reactions and feeds back.
- **Telegram bot** (`python-telegram-bot`): Gold standard for this UX pattern. Owner avoiding social media — worth reconsidering since Telegram is a messaging app, not social media. Easiest to implement.  
To build either: create a new file `tools/discord_delivery.py` or `tools/telegram_delivery.py` and add it to the delivery step in `main.py`.

---

### ADR-009: In-email feedback via Vercel endpoint
**Date**: 2026-04-28 (Session 6)
**Decision**: Use a Vercel serverless function as the click target for in-email feedback — not mailto links, not a direct Notion link.  
**Why**: Mailto links require two actions (click + send email) and add inbox noise. Linking directly to Notion still requires leaving the email and navigating Notion. A Vercel function is one click, returns immediately, and the free tier handles one user trivially.  
**Mechanism**: Per-story links embedded in the email HTML. `notion_page_id` is attached to each story dict by `publish_digest()` before `send_digest()` is called. The endpoint PATCHes the Notion page's Feedback property.  
**Status**: Implemented — `feedback_server/api/feedback.py`. Awaiting Vercel deployment (user sets `feedback_endpoint` in `config/digest.yaml` once deployed).

**ADR-009a: ⭐ Top pick is implicit (click-through), not a button**
**Decision**: Clicking a story title in the email is the ⭐ Top pick signal. The title link routes through the Vercel function (`v=top`), which records ⭐ and then 302-redirects to the article. The email only shows explicit 👍 / 👎 buttons.  
**Why**: If you clicked through to read the article, that's a stronger signal than any button. It keeps the feedback UI minimal (two buttons, not three) and makes the most valuable signal the lowest friction.  
**Trade-off**: Click-through tracking only works when `feedback_endpoint` is set. If the endpoint is empty, title links go directly to the article and no signal is recorded.

**ADR-009b: Deploy Vercel CLI via `npx`, not Homebrew**
**Decision**: Use `npx vercel` to deploy the feedback server, not `brew install vercel-cli`.  
**Why**: On macOS 13 (Ventura), Homebrew has no pre-built bottles for vercel-cli's dependency chain. It falls back to compiling from source — including LLVM, cmake, python@3.14, and node — which takes over an hour and failed with a missing patch file during the LLVM build. `npx vercel` downloads a pre-built JavaScript bundle directly from npm's registry, bypasses all compilation, and completes in seconds.  
**Command**: `cd /Users/jrichter/CLAUDE/broadsheet/feedback_server && npx vercel --prod`

---

### ADR-010: Feedback attaches to topic tags, not categories or sources
**Date**: 2026-04-17 (Session 5)
**Decision**: 👍/👎/⭐ feedback modifies per-tag weights in `calibration.yaml`. Category composition is structural and never changes. Sources are managed manually by editing `sources.yaml`.
**Why**: The original model charged 👎 signals to the category weight, which could silently suppress a category the user had intentionally chosen. A thumbs-down on a single article is ambiguous — it might mean "wrong source," "wrong angle," "duplicate coverage," or "not in the mood" — but it never means "remove this category from my digest." Positive feedback is less ambiguous ("I liked this kind of story") but still doesn't belong at the category level; the user deliberately set the categories and wants all of them represented.
**Mechanism**:
- EditorAgent assigns 2–3 topic tags to each story when writing the summary (same LLM call). Tags describe the story TYPE, not source or category.
- Tags are stored in Notion as a `Tags` multi-select property.
- On the next run, `read_feedback()` reads Tags + Feedback from rated Notion pages and builds a `tag_feedback` dict.
- `update_calibration_from_feedback()` applies Wilson score lower bound (sparse data gets conservative weight) and `math.log1p` (early ratings matter most) when updating `tag_weights`.
- `select_stories()` passes tag weights to the LLM as a plain-English feedback summary; the LLM applies the preference when scoring that day's candidates.
**Trade-off**: Tagging adds a small amount of work to the editor LLM call; Wilson score + log accumulation is more math than the original additive approach. Worth it — the original model had a structural bug that no amount of tuning could fix.
**Related improvements shipped in the same session**:
- **Category budget composition**: each category gets `max_stories // 4` slots; unused slots roll to strongest remaining candidates from other categories. Total is capped at `max_stories` (default 8). Feedback never changes this allocation.
- **Diversity slot**: if any category has zero stories above `min_confidence_score`, its best story above `diversity_min_quality` is added anyway, so categories can't silently disappear.
**Tests**: `tests/test_feedback_system.py` covers all seven invariants (tagging, budget, rollover, Wilson, log, tag-only attribution, diversity).

---

### ADR-011: max_stories should be 40, not 8
**Date**: 2026-04-28 (Session 7)
**Decision**: `max_stories: 40` with a base budget of 10 per category. Unused slots roll to stronger categories.
**Why**: Session 5 introduced `max_stories: 8` by operationalizing the number "8" from a rhetorical phrase in `story_guidance` ("A digest with 8 outstanding stories is better than 20 mediocre ones"). That phrase was a quality-over-quantity principle, not an intended target. The user's actual intention was 40 stories with equal category weighting. The error was only caught two sessions later when today's run produced exactly 8 stories.
**Lesson**: Never extract a number from prose guidance and encode it as a hard programmatic limit without explicit user confirmation.
**Related**: The category-budget architecture introduced in Session 5 is correct and kept. Only the cap value changed.

---

### ADR-012: Silent feedback via CSS :checked interactive email technique
**Date**: 2026-04-30 (Session 9)  
**Decision**: Replace `<a href>` feedback links with a CSS `:checked` technique for Apple Mail / iOS Mail users. Hidden checkboxes + labels trigger conditional background-image URL fetches from inside the email client — no browser tab opens. Gmail and other clients retain the existing `<a href>` links as fallback.  
**Why**: The original link approach (and even the self-closing `window.close()` variant from Session 8) always opened a browser tab, pulling the user out of their email view. For a skim-reading workflow, any context switch is disruptive. The `:checked` technique fires the feedback request from inside the email client's own rendering engine — the experience is a label dimming and getting a strikethrough, nothing more.  
**Mechanism**:
- Two hidden `<input type="checkbox">` elements sit at the top of each story `<div>` (must precede siblings to work with the CSS `~` general-sibling combinator)
- `<label>` elements render 👍 / 👎. Clicking a label toggles the associated checkbox
- CSS rules: `#id:checked ~ .interactive .ping { background-image: url(endpoint) !important }` — activating the rule causes the email client to fetch the endpoint URL, which is the Vercel feedback function
- A second CSS rule marks the label grey with `text-decoration: line-through` as visual confirmation
- `<style>` block is emitted once at the top of the email body; per-story rules are generated in a pre-pass over all stories before HTML is assembled
- Gmail strips `<style>` blocks entirely, so the interactive controls (which have `display:none` inline) stay hidden; the fallback `<p class="bs-fallback">` links — with no inline hiding — show instead
**Client support**: Apple Mail (macOS) ✅, iOS Mail ✅ — silent. Gmail web / other — window.close() href fallback, same as Session 8.  
**MPP conflict guard**: Apple's Mail Privacy Protection pre-fetches `<img>` tags silently on email arrival. It does *not* pre-fetch conditional `:checked` CSS background-images (which require user interaction to activate). However, if Apple ever changed this, both 👍 and 👎 would fire for every story on arrival, silently corrupting calibration. Defence: before writing a 👍/👎 to Notion, the Vercel function reads the current Feedback property + `last_edited_time`. If the opposing vote was set within the last 30 seconds, it clears the property and logs `[MPP-CONFLICT]` to Vercel function logs rather than writing — "no rating" is far preferable to a falsely-corrupted rating.  
**Trade-off**: The technique depends on Apple's continued CSS support and on MPP not pre-fetching conditional CSS URLs. Both are implementation details that could change without notice. The anomaly guard and Vercel logs provide visibility if this happens.

---

### ADR-008: Builder content uses different sources than other categories
**Decision**: `BuilderAgent` uses Hacker News Show HN API, Reddit RSS, personal blogs, GitHub Trending, ProductHunt — not mainstream news RSS.  
**Why**: "Look what I built" content doesn't appear in newspapers or wire services. It lives on HN, personal blogs, subreddits.  
**Key source**: Hacker News `/showstories.json` endpoint is specifically designed for this — posts tagged "Show HN" by their authors.

---

### ADR-015: Quality floor replaces global confidence threshold for story selection
**Date**: 2026-05-01 (Session 14)
**Decision**: The category budget loop now uses `diversity_min_quality` (4) as the selection floor instead of `min_confidence_score` (6). Each category selects its best `base_budget` stories from any story scoring ≥ 4. Stories scoring 1-3 are dropped as junk. The higher `min_confidence_score` value still appears in config and drives the emergency retry in `main.py`, but no longer gates normal selection.
**Why**: Live run on 2026-05-01 produced only 22 of a targeted 40 stories. Root causes: (1) the scoring rubric in `agents.yaml` places scores 5-6 in a "niche" band, so a story at score 5 passes the rubric's "interesting" tier but failed the old code filter of ≥ 6; (2) a rhetorical phrase in the agent prompt ("8 outstanding stories is better than 20 mediocre ones") anchors the LLM to score conservatively; (3) the builder and critical voices categories produce inherently lower-scoring material than big news, so a global threshold systematically disadvantages them. Switching to a category-local top-K approach with a lower floor guarantees each category fills its budget while still dropping genuine junk (scores 1-3).
**Per-source cap tightened concurrently**: Cap changed from 6 to `ceil(base_budget / 3)` = 4, preventing any single feed from filling more than ~30% of a category's guaranteed slots.
**Deferred**: The `agents.yaml` prompt phrase "8 outstanding > 20 mediocre" and the `min_confidence_score: 6` config value are flagged for review but not changed — both are in user-editable config files requiring explicit sign-off.

---

### ADR-014: Replace Nemotron + OpenRouter with Gemini + Cerebras
**Date**: 2026-05-01 (Session 12 — planned; implementation in Briefs 06–08)
**Decision**: Replace primary LLM (Nemotron via NVIDIA NIM) with Gemini (`gemini-3.1-flash-lite-preview` via Google AI Studio) and replace fallback LLM (OpenRouter / Llama 3.3 70B) with Cerebras (`llama-3.3-70b`). Groq remains as tertiary. OpenRouter is removed from the chain entirely.
**New chain**: Gemini → Cerebras → Groq → equal scoring.
**Why replace Nemotron:** NVIDIA NIM's free tier is a one-time credit grant (~1,000 credits per account) confirmed to have no monthly refresh. At ~30 LLM calls per run the credits deplete in approximately 30 days, after which every run cascades to fallback and tertiary — the exact failure mode seen on 2026-04-30. Provider must be replaced before GitHub Actions automation goes live.
**Why Gemini as primary:** Always free (250 RPD, 250K TPM), no credit card required, 1M-token context handles the scoring payload natively without chunking. Quality is at or above Llama 3.3 70B class. Gemini was previously ruled out (ADR-013) due to unreliability on the Garden-Cat project — that issue was likely model-name confusion (naming has since been validated: `gemini/gemini-3.1-flash-lite-preview` via LiteLLM with `GEMINI_API_KEY`). Model name is config-only (`digest.yaml`), so switching to a stable model is a one-line change if the preview is deprecated.
**Why Cerebras as fallback:** Always free (~1M tokens/day, 14,400 RPD), no credit card, different infrastructure from Gemini so failures are independent. 8K context cap on free tier requires chunked scoring (implemented in Brief 07) — two ~60-story batches fit comfortably. Verify `llama-3.3-70b` availability in account dashboard before relying on it; if absent, `qwen-3-235b-a22b-instruct-2507` is the confirmed fallback within Cerebras.
**Why remove OpenRouter:** OpenRouter's free-tier shared pool (Venice and similar upstreams) regularly 429s under load — seen again on 2026-04-30. With Gemini + Cerebras + Groq providing three independent always-free providers, OpenRouter adds no marginal resilience.
**Additional changes in Briefs 06–09:** TypeError crash fix (published_iso sort); payload reduction (per-source cap of 6, description 300→100 chars, drop `published` field); chunked scoring on all tiers; `num_retries=1` on all LLM constructors; off-peak cron (3am UTC); cross-day Notion URL dedup.
**Supersedes:** ADR-003 (Nemotron + OpenRouter as primary/fallback).

---

### ADR-016: Heuristic-first scoring with LLM pairwise rerank
**Date**: 2026-05-02 (planned, Session 15) — **Implemented 2026-05-05 (Session 20)**
**Decision**: Replace the current "LLM scores every candidate 1–10" approach with a two-stage pipeline: (1) score all candidates with a deterministic heuristic formula (source authority + engagement + recency + tag-affinity), then (2) pass only the top 45 survivors to the LLM for pairwise/relative ranking.
**Why**: LLMs are unreliable at absolute point-scoring — ratings cluster around 5–7 because the model can't hold a consistent internal scale across 100 independent judgments. LLMs are substantially more reliable at relative comparison ("which of these 45 stories matters most?"), which is a well-documented finding in "LLM-as-judge" literature. Meanwhile, engagement signals (HN upvotes, Reddit score, GitHub stars) and recency are deterministic, free, and stronger signals than LLM opinion on headlines — but the prior system only used them as pre-filters, not as scoring inputs. Switching reduces daily LLM token usage by ~70–80% and should improve ranking quality simultaneously.
**Mechanism**: `_score_heuristic()` in `agents/newsroom_lead.py` — additive formula: source_weight + (engagement / normalisers) + recency_bonus + tag_affinity + cluster_trending_bonus. `_rerank_with_llm()` replaces `_score_in_chunks()` — single prompt, pairwise ranking response, rank mapped to confidence_score 5–10. Top 45 candidates configurable via `heuristic_weights.llm_rerank_top_n` in `digest.yaml`.
**Consequence**: `confidence_score` now reflects LLM rank among top 45 (5–10 range); stories outside the top 45 score 0. Calibration is unaffected (reads tag weights, not confidence scores). Cross-category re-categorization (C4) is embedded in the same rerank call.
**Supersedes**: Chunked LLM scoring established in Brief 07.

---

### ADR-017: Topic clustering before scoring
**Date**: 2026-05-02 (planned, Session 15) — **Implemented 2026-05-05 (Session 20)**
**Decision**: Cluster candidate stories by topic similarity *before* scoring. One representative per cluster proceeds to scoring; cluster size becomes a "trending" signal that boosts the heuristic score.
**Why**: Five outlets covering the same OpenAI launch all pass through URL-exact deduplication. Per-source cap doesn't catch them (different sources). They eat scoring tokens and crowd categories with redundant takes. When five outlets cover something independently, that convergence is a signal — the story is genuinely significant. The prior system treated multi-outlet coverage as a problem to suppress rather than information to use.
**Mechanism**: `_cluster_stories()` in `agents/newsroom_lead.py`. TF-IDF cosine similarity on titles, greedy clustering at threshold 0.35 (tuned from the planned 0.45 — short news headlines have lower average similarity than longer text). `scikit-learn TfidfVectorizer + cosine_similarity`. No external API, no embeddings cost, runs in <1s on 100 stories. Cluster representative = most recent story (by `published_iso`). Trending bonus: `+log(cluster_size) × 0.5` in heuristic score.
**Trade-off**: Requires `scikit-learn` dependency. Threshold 0.35 was confirmed empirically (two different headlines about the same model launch: similarity 0.383). Upgrade path to Gemini embeddings exists if TF-IDF proves insufficient for harder cases.

---

### ADR-018: Multi-subscriber feedback via a separate Votes database (planned — Brief 11)
**Date**: 2026-05-02 (Session 15 — planned, not yet implemented)
**Decision**: Introduce a second Notion database ("Broadsheet Votes") with one row per vote per subscriber, replacing the single `Feedback` select property on each Stories row.
**Why**: The current single `Feedback` property on Stories rows stores one vote per story — whichever subscriber votes last overwrites everyone else. There is no subscriber identity in the system anywhere. For 5–20 subscribers with equal vote weighting, a dedicated Votes database is the right model: it scales to any subscriber count without schema changes per person, keeps the Stories database clean, and the aggregation query is a straightforward Notion API call using the same pattern already in `notion_delivery.py`.
**Why not multiple columns**: Adding a `Feedback_sub001`, `Feedback_sub002` column per subscriber requires a Notion schema change each time someone joins and makes `read_feedback()` progressively messier. Doesn't scale past ~5 people without becoming unwieldy.
**Mechanism**: New Notion db schema: `{Subscriber (title), Story Page ID, Story Title, Vote, Story Tags, Voted At}`. Tags are denormalized at vote time (copied from story) to avoid cross-db joins during aggregation. Per-subscriber emails with `&sub=subscriber_id` in feedback URLs. Vercel function writes to Votes db instead of Stories.Feedback. New `read_multi_feedback()` aggregates Votes rows by tag → same `{tag_feedback, total_items}` return shape as current `read_feedback()` (drop-in replacement). Wilson + log calibration formula unchanged.
**Privacy**: Votes database is only accessible via the Broadsheet Notion integration. Subscribers do not have Notion access. Only the owner sees individual votes.
**Backwards compatibility**: Old `Stories.Feedback` values are not migrated. Calibration weights in `calibration.yaml` carry forward. If migration of existing votes is desired, a one-off script can be added.
**Subscriber identity**: Short URL-safe IDs assigned by the owner in `config/digest.yaml`. No PII in URLs or Notion.

---

### ADR-019: Use Gmail IMAP inbox polling for Substack sources
**Date**: 2026-05-04 (Session 19 — Brief 12)
**Status**: Superseded. The dedicated Gmail account was later suspended for bot-like IMAP login patterns from GitHub Actions. Substack is now fetched via Tavily (`tools/inbox_fetcher.py: fetch_substack_via_tavily`, the `tavily_substack` block in `sources.yaml`). See the CHANGELOG entry "Replace Gmail inbox with Tavily for Substack sources".
**Decision**: Substack-hosted newsletters are fetched by polling a dedicated Gmail inbox via IMAP, not via RSS.
**Why**: Substack actively blocks RSS requests from AWS/cloud IP ranges at the HTTP layer (returns `403 Forbidden`). This affects GitHub Actions, which runs on AWS. No User-Agent string or retry strategy can work around an IP-based block. The path Substack actually wants users to take is email subscription — email delivery has no anti-bot layer.
**Why a dedicated Gmail, not personal**: Expanding the IMAP session to include a personal inbox creates a broad credential risk (read access to all personal mail). A dedicated Broadsheet account contains only Substack newsletters, so a leaked app password has minimal blast radius.
**Why not a feed proxy or Tavily fallback**: Self-hosted feed proxies (e.g. RSSHub on Vercel) use cloud IPs too and may be Cloudflare-blocked regardless of IP. Tavily returns search results, not feed entries — unreliable recency and no guaranteed publication dates, which breaks the 48-hour recency filter and cross-day dedup.
**Mechanism**: `tools/inbox_fetcher.py` connects via IMAPS (port 993) using the existing `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` credentials. Searches for `(UNSEEN)` messages — since the inbox receives only Substack subscriptions, no sender filtering is needed. Post URLs are extracted from the email HTML by matching the `/p/` path pattern (works for both `*.substack.com/p/*` and custom Substack domains like `interconnects.ai/p/*`). Query params (UTM, `redirect=app-store`) are stripped. Emails are marked as read after processing. Category assignment: default is "Critical Voices"; exceptions for non-Critical-Voices sources are configured in `config/sources.yaml` under `inbox_substack.senders`, matched against the stable email address rather than the variable display name.
**Trade-off**: Requires a one-time manual subscription step per Substack source (subscribe from the Broadsheet Gmail). Adding a new source with a non-default category requires a one-line entry in `sources.yaml`. Sources that haven't posted recently produce no stories (same as RSS). Welcome/confirmation emails are silently skipped because they contain no `/p/` URL.

---

### ADR-013: Tertiary scoring LLM uses Groq, not Anthropic
**Date**: 2026-04-30 (Session 11)
**Decision**: Add a third scoring tier using Groq's free-tier Llama 3.3 70B (model: `llama-3.3-70b-versatile`, env var: `GROQ_API_KEY`) via LiteLLM. The new chain is primary (Nemotron) → fallback (OpenRouter/Llama) → tertiary (Groq/Llama) → equal scoring.
**Why**: Brief 03 originally suggested Claude Haiku 4.5 via the Anthropic API as the tertiary tier. Anthropic requires a paid deposit before API keys become usable, but this version of Broadsheet is constrained to free tier only. The user requested a research pass on alternatives. Top three viable free-tier options were Google Gemini Flash, Groq, and Cerebras. Gemini was ruled out due to prior reliability issues on the user's Garden-Cat project. Between Groq and Cerebras the difference is negligible for this workload; Groq selected for greater operational maturity and a slightly newer model (Llama 3.3 vs Cerebras's typical 3.1). Groq's free tier (14,400 requests/day, 30 RPM, no credit card) is far in excess of one daily digest's needs.
**Why a third tier at all**: On 2026-04-28 both primary (Nemotron 502) and fallback (OpenRouter rate-limited on the free tier) failed simultaneously, dropping the run to equal scoring. Equal scoring assigns confidence_score=6 to every story, which exposes a feed-order bias in the category budget loop and produces a poor digest. Adding a third independent provider — different infrastructure, different rate-limit pool, different time of day for free-tier resets — significantly reduces the probability of a triple-failure dropping us to equal scoring on any given day.
**Why JSON validation is acceptable**: Groq, unlike Gemini, does not enforce a strict JSON schema on output (only `json_object` mode). This is fine because `agents/newsroom_lead.py:_parse_llm_scores()` already extracts the JSON array via regex and tolerates surrounding prose. The same parser handles all three scoring tiers identically.
**Trade-off**: Groq's free tier is *very* generous but it's still a free tier — quotas can change without notice. The tertiary path is non-fatal: if Groq fails or `GROQ_API_KEY` is unset, the system falls through to equal scoring as before. No regression.
**Implementation**: `main.py:_build_tertiary_llm()`, `agents/newsroom_lead.py:select_stories(tertiary_llm=...)`. Configuration in `config/digest.yaml` under `models.tertiary`. Verified end-to-end with a smoke test against the live Groq API.
