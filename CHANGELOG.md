# CHANGELOG — Broadsheet

## [v0.16.0] — 2026-06-04 (Session 43) — Brief 21: Selection pipeline fixes

Five fixes to the selection pipeline. 99 tests pass.

**Fix 1 — Dedup before budget (`agents/newsroom_lead.py`).** `_semantic_dedup` now runs per-category on the `passing` pool before the budget loop. Previously, near-duplicate events (e.g. four outlets covering the same EO) consumed the full per-category cap; post-selection dedup collapsed them but freed slots were never reclaimed. A second dedup pass is kept after merging pinned stories as a safety net.

**Fix 2 — Editorial scoring for all categories (`agents/newsroom_lead.py`).** Added `big_news` and `builder` to `_EDITORIAL_CATS`. All four categories now use `recency + affinity + source_bonus` with no engagement dependency. Major news articles were scoring ~1–2 on the old engagement formula and being cut at the heuristic top-N before the model saw them (Anthropic IPO filing scored 1; two military-AI bills scored 1–1.5). As a side effect, `big_news` and `builder` now pass up to 20 candidates to the LLM reranker (up from 12).

**Fix 3 — Label normalization at ingestion (`tools/inbox_fetcher.py`, `main.py`).** Substack `sources.yaml` entries use display labels ("Commentary", "Big News", "Built & Released") as their `category` field. The budget loop only recognises internal keys, so display-labelled stories formed phantom mini-categories that escaped per-category caps. Added `_DISPLAY_TO_INTERNAL` map and `_normalize_category()` applied at ingestion.

**Fix 4 — Guardian via Tavily; zero-yield alerting (`agents/search_agents.py`, `tools/source_monitor.py`, `config/sources.yaml`).** The Guardian API was returning HTTP 200 with zero results (over-constrained query) and logging success silently. Dropped the bespoke API path; added `theguardian.com` to `laws_ethics.no_feed_sites` for Tavily-based fetching. Added `record_zero_yield()` to `source_monitor.py` and per-site zero-yield tracking to all four gather functions; sources that fetch OK but return 0 stories for 2+ consecutive runs now surface in alerts. Deleted `tools/guardian_search.py`.

**Fix 5 — `must_include` window configurable (`agents/newsroom_lead.py`, `config/digest.yaml`).** Hardcoded `MUST_INCLUDE_HOURS = 48` promoted to `digest_cfg.get("must_include_max_hours", 48)`. Set to 72 in `digest.yaml` so curated-author posts arriving late via Tavily or with slightly-off timestamps are still pinned.

Files changed: `agents/newsroom_lead.py`, `agents/search_agents.py`, `tools/inbox_fetcher.py`, `tools/source_monitor.py`, `main.py`, `config/sources.yaml`, `config/digest.yaml`, `tests/test_selection.py` (+2 tests). Deleted: `tools/guardian_search.py`. Docs: `docs/DECISIONS.md` ADR-051.

## [v0.15.0] — 2026-06-03 (Session 42) — Brief 20: Tavily query rewrite + category reshape

Config-only change to `config/sources.yaml` and `config/digest.yaml` (both user-editable), executed as a coaching session per `briefings/brief-20-query-coaching-tavily-translation.md`.

**Tavily queries rewritten (all four categories).** Every `tavily_queries` and `no_feed_sites` query was converted from operator-laden keyword strings (`OR`, `"quotes"`, `site:`) into plain natural-language phrases. Tavily does semantic retrieval and does not reliably parse those operators; source-anchoring now lives in `include_domains` (the `no_feed_sites` mechanism), recency in the global `topic="news"` + `days=`. Topic queries follow one-intent-per-query (Law & Ethics 4→8, Commentary 1→5).

**Category reshape:**
- **Builder** refocused from the "Show HN" firehose onto practitioner guidance / tooling / model comparisons. Hacker News **paused**; added Nvidia, Cursor, Groq, Artificial Analysis, Epoch AI as `no_feed_sites`; Anthropic/OpenAI added here with developer-guidance queries (distinct from their Big News announcement queries).
- **Critical Voices → "Commentary"** (display label only; internal key `critical_voices` unchanged). Broadened to organisational-AI-risk, the psychology of AI use, and AI alignment/behaviour research. The Batch (Andrew Ng) moved here from Builder.
- **Mozilla.ai** moved from Big News to Law & Ethics; the dropped slots (Semafor/The Information, techpolicy.press et al., the name-OR critics) are all already fed via RSS/Substack.

Category caps held at 8 (+2 rollover) — to be revisited from real-run evidence, not raised pre-emptively.

Files: `config/sources.yaml`, `config/digest.yaml`, `docs/DECISIONS.md` (ADR-050). 97 tests pass unchanged; rewritten queries validated live (fresh, on-intent results in news mode).

## [v0.14.4] — 2026-06-03 (Session 41) — Tavily news-mode fix

`tools/tavily_search.py`: added `topic="news"` to the `kwargs` dict in `search()`. All 8 Tavily call sites route through this function, as does the Substack path via `search_site()` in `tools/inbox_fetcher.py`, so a single-parameter change covers the entire codebase. The `days=` recency window and a reliable `published_date` field are now actually honored by the API (both require `topic="news"`; silently ignored in the default `topic="general"` mode). Smoke test confirmed: 3/3 results dated within the 2-day window.

`docs/DECISIONS.md`: ADR-049 added, noting that ADR-043 (Session 36) was a no-op in general mode and only takes effect with this fix.

97 tests pass unchanged.

## [unreleased] — 2026-06-02 (Session 40) — Tavily root-cause diagnosis + two briefs (docs only)

Analysis/documentation session — **no code change**.

**Root cause found:** `tools/tavily_search.py` calls Tavily in the default
`topic="general"` mode, where the `days=` recency window and the `published_date` field
are silently ignored (both require `topic="news"`). Session 36's ADR-043 therefore had
no effect; all recency filtering fell to the post-hoc 14-day cutoff. Same bug silently
breaks the trusted-voice `must_include` pin (undated Substack posts dropped before the
pin runs). Verified against the installed Tavily client signature.

**`briefings/brief-19-tavily-news-mode-fix.md` (new):** executable code-only fix —
set `topic="news"` (one change covers the Substack path via `tools/inbox_fetcher.py`),
plus honest Session-36 cleanup guidance and a real-run verification plan. Broad-query
strategy deliberately out of scope.

**`briefings/brief-20-query-coaching-tavily-translation.md` (new):** session playbook for
coaching query-writing and translating intents into Tavily calls (params over operators,
one intent per query). Supersedes Brief 14's operator-in-string approach.

## [v0.14.3] — 2026-05-27 (Session 39) — suppress Tavily quota errors from source alerts

Commit `2ead2b0`.

**`tools/tavily_search.py`:** Removed `record_failure()` call from the quota-error branch. When Tavily returns a credit-limit error, the `quota_exceeded` flag is still set (drives the top-bar banner) and a warning is still logged, but no entry is written to `logs/source_state.json`. Real Tavily failures (network, auth, malformed queries) continue to call `record_failure()` as before.

**`tools/source_monitor.py`:** `get_alerts()` now skips any alert whose `last_error` starts with `[TAVILY-QUOTA]`. Backstop for legacy state and any future edge case.

**`logs/source_state.json` (gitignored):** One-time cleanup — `consecutive_failures` reset to 0 on the 12 entries polluted by today's quota errors.

## [v0.14.2] — 2026-05-26 (Session 38) — replace deprecated Gemini primary model

Commit `a77907c`.

**`config/digest.yaml`:** `models.primary.model` updated from `gemini/gemini-3.1-flash-lite-preview` (404 — Google pulled it) to `gemini/gemini-2.5-flash-lite` (current stable GA release). `gemini-2.0-flash-lite` was ruled out — it shuts down 2026-06-01. Sanity check confirmed `fallback=False` (Gemini initialises as primary).

## [v0.14.1] — 2026-05-26 (Session 37) — remove Nemotron banner, add Tavily usage to run report

Commit `3130a47`.

**Removed stale fallback banner (`tools/email_sender.py`):**
- Yellow "fallback model (Nemotron credits exhausted)" banner removed from HTML and plaintext email. The banner cited a provider removed months ago; the run-report strip already shows which model ran.
- `fallback_model_used` parameter removed from `send_digest`, `_build_html`, `_build_plaintext`. Parameter remains in `agents/editor.py` for the writing-prompt context.

**Proactive Tavily usage tracking (`tools/tavily_search.py`, `main.py`, `tools/email_sender.py`):**
- New `get_usage()` in `tavily_search.py`: `GET /usage` with bearer auth; returns `{used, limit, pct}` or `None` on failure. Non-blocking — digest continues if the call fails.
- `run_report` dict: `sources` key replaced with `tavily`. Populated end-of-run with either "Tavily: NN% used (X/Y)" (proactive) or "Tavily searches skipped — monthly credit limit reached" (reactive, when searches were actually skipped).
- HTML and plaintext run-report renderers updated to show the new `tavily` field as a third entry alongside Scoring and Summaries.
- `_build_tertiary_llm` docstring updated: Nemotron/OpenRouter → Gemini/Cerebras.

## [v0.14.0] — 2026-05-22 (Session 36) — recency enforcement, date extraction, trusted-voice pin

Root-cause fix for old stories reaching the newsletter. Commit `a5e11b1`.

**Recency cutoff — all categories:**
- Builder category exemption removed; 14-day cutoff now applies to all four categories.
- Parse-failure bug fixed: `except: fresh.append(s)` → drop. Stories with unparseable dates in any category are now dropped, not silently kept.
- `dateutil` added as a fallback date parser so Tavily date strings like "March 15, 2024" are accepted.

**Date extraction (`tools/tavily_search.py`):**
- New `_extract_published()` helper: four-tier cascade — strict ISO → dateutil → URL date pattern → content-text date regex. Replaces the raw passthrough that left most Tavily results undated.

**Tavily recency windows (`agents/search_agents.py`):**
- All 8 Tavily call sites now pass `days=3` (open queries) or `days=2` (no_feed_sites). Implements the intent of Brief 14 which shipped with no code change.
- `run_queries()` updated to accept and forward `days=`.

**Evergreen-URL memory (`tools/notion_delivery.py`, `agents/newsroom_lead.py`, `main.py`):**
- New `read_rejected_urls()` queries the Notion Candidates DB for URLs previously dropped for recency. Applied pre-funnel each run so the same stale URL doesn't spend LLM-rerank tokens twice.

**GitHub Trending (`tools/github_trending.py`):**
- `published_iso` now stamped with the scrape time instead of `None`.

**Trusted-voice pin (`config/sources.yaml`, `tools/inbox_fetcher.py`, `agents/newsroom_lead.py`):**
- `priority: must_include` added to all 9 named-individual Substack sources.
- Fresh posts (≤48h) from these sources are pinned into the digest before the normal funnel. One pin per byline. Pinned stories win semantic dedup clusters.
- `published_iso` now included in the LLM rerank candidate JSON.

**Tests:** 18 new tests (date extraction × 6, recency cutoff × 7, trusted-voice pin × 5). All 97 tests pass.

## [unreleased] — 2026-05-19 (Session 35) — observations inbox for agent pipeline

Documentation-only. No code or config touched.

- **`briefings/observations-inbox.md`** — new file. A lightweight capture
  doc for ongoing observations about the multi-agent pipeline (search,
  selection, writing). Append-only during the week; drained on weekly
  synthesis into a new numbered brief. Workflow documented in the file
  header: capture → synthesise → cleanup.
- **`.claude/session.md`** — Session 35 block added.

The inbox starts empty. Synthesis will produce `brief-19-*.md` (next
available number) when the first batch of observations is ready.

## [unreleased] — 2026-05-18 (Session 34) — source pool expansion + Brief 14 settled

Config-only change. No code touched.

### Source pool expansion
Daily digest was being squeezed by cross-day dedup + 14-day recency cutoff
against a candidate pool that had thinned out as RSS feeds rotted. This session
adds 22 new RSS feeds across the four categories, plus 10 `no_feed_sites`
entries (Tavily site-search) that revive sources whose RSS has died but whose
underlying publication is still active.

**`config/sources.yaml` — full revision**
- Header rewritten: source-selection rubric (alive in 30 days, AI-relevant or
  AI-filterable, editorial signal not aggregation, adds something the list
  doesn't already cover) + routing rule (RSS → tavily_substack → no_feed_sites).
- Big News: added The Decoder, Semafor (`ai_only_filter`), Axios (`ai_only_filter`),
  Rest of World (`ai_only_filter`), Platformer (Casey Newton, byline-tagged).
  `no_feed_sites` revives Anthropic and OpenAI; adds AI2 and Mozilla.ai.
- Laws & Ethics: added The Markup, CDT, Montreal AI Ethics Institute,
  Programmable Mutter (Henry Farrell, byline-tagged). `no_feed_sites` revives
  Brookings, Stanford HAI, Lawfare; adds Mozilla Foundation.
- Builder: added Hamel Husain, Eugene Yan, Lilian Weng, Sebastian Raschka,
  Jay Alammar, Chip Huyen (all byline-tagged). `no_feed_sites` revives Hugging
  Face Blog and The Batch.
- Critical Voices: added Zvi Mowshowitz, Edward Zitron, Brian Merchant,
  Molly White, L.M. Sacasas, Baldur Bjarnason (all byline-tagged).
  `no_feed_sites` revives DAIR Institute and The Register AI.
- `tavily_substack.sources`: added Zvi, Sebastian Raschka, Edward Zitron,
  Brian Merchant, L.M. Sacasas as Substack-fallback paths.
- `tavily_queries` per category rewritten per Brief 14 — removed
  adjective-led SEO magnets; new queries restrict by event type, named
  domain, or named person.
- `no_feed_default_queries` per category tightened from `["news", "new post"]`
  to category-appropriate AI terms.
- All `Template / Url / enabled: false` placeholder rows removed.

**`briefings/brief-14-tavily-queries.md` — rewritten as a settled decision doc**
- Was an open question doc (carried forward 8+ sessions).
- Now records the principle (queries restrict by object type, domain, or
  named person; pure adjective queries banned), the final per-category query
  set, the removed queries and why each was a noise magnet, and the routing
  rule for any future candidate source.

### Verification
- All 22 new RSS URLs HEAD-checked: 21 returned 200 + valid XML;
  Semafor returned 405 on HEAD but 200 on GET (feed parser uses GET).
- Sample fetch of 7 new feeds through `tools/rss_fetcher.fetch_multiple_feeds()`
  — parsed cleanly, no XML errors, no auth failures.
- YAML parse and per-category enabled-source counts verified.

### Files affected
- `config/sources.yaml`
- `briefings/brief-14-tavily-queries.md`
- `CHANGELOG.md`
- `.claude/session.md`

---

## [0.16.0] — 2026-05-14 (Session 33)

### Editor re-brief: 2-sentence factual summaries (Brief 18)

Every story in the 2026-05-14 digest ended with an editorialising "why it
matters" tail. Root cause: `agents.yaml:188` ("End with a single sentence on
why the owner might care") — that line invited the implication-essay style
every run. The editor also learnt to write the banned formula in inverted word
order (`This is relevant for anyone…`) that evaded the existing regex.

**`config/agents.yaml` — `editor_agent` block replaced (lines 172–243)**
- Goal reframed: "two-sentence factual summary, enough to decide whether to click"
- Length: "Aim for 60-85 words" → "Exactly 2 sentences. ≤60 words total. No exceptions."
- Implication sentence: removed entirely; replaced with explicit "Do NOT add" rule
- Source attribution: "weave naturally" → "Do NOT include source attribution in prose"
- Banned-phrases list (12 lines) removed — regex layer is the enforcement mechanism
- Structure: flat bullet block → `##`-delimited sections (Gemini Flash-Lite best practice)
- Model paragraph: 4-sentence → 2-sentence user-supplied version

**`tools/style_check.py` — three enforcement changes**
- `_WORD_LIMIT`: 90 → 60 (calibrated to new ≤60-word cap)
- `_tighten()` prompt: "trim by 15+ words" → "rewrite as 2 sentences, ≤60 words,
  no implication, no source attribution"
- `BANNED_PATTERNS`: 7th entry added for the inverted formula: `This [word(s)] for/to
  [actor]` — catches the dominant escape hatch from the 2026-05-14 audit (20/26 stories)

**`tests/test_style_check.py` — 17 new tests (27 → 44)**
- `TestInvertedFormula`: 13 tests — 10 violation fixtures from the audit,
  2 clean-sentence non-matches, 1 end-to-end rewrite assertion
- `TestWordCountTightening`: 4 tests — verifies tightening fires at >60 words,
  is silent at ≤60 words

**`docs/DECISIONS.md`** — ADR-038 (editor re-brief) and ADR-039 (inverted-formula regex)

**Files affected**
- `config/agents.yaml`
- `tools/style_check.py`
- `tests/test_style_check.py`
- `docs/DECISIONS.md`
- `CHANGELOG.md`, `.claude/session.md`

---

## [unreleased] — 2026-05-14 (Session 32) — session-end protocol

Process-only change; no version bump.

### Closed the "commit but never merge" gap
- **`CLAUDE.md`** — "Ending a session" rewritten as a six-step ritual that explicitly includes `git fetch`/rebase, fast-forward merge into `main`, push to `origin/main`, worktree removal, and branch deletion. New stop condition: "the session is not over until `git branch -vv` shows the working branch is gone and `main` is up to date with `origin/main`". Cleanup uses `git branch -d` (not `-D`) so a skipped merge fails loudly rather than silently erasing the work.
- Triggered by Session 31's "merge fix" symptom (Brief 17 implemented, pushed, but never merged — GitHub Actions ran the stale `main` and produced no run log).

### Orphan worktree sweep
- 11 stale `claude/*` worktrees + branches retired (7 pure-behind cleanly deleted; 4 force-retired after content verification: `festive-hellman-115bd1` superseded by main, `affectionate-heisenberg-5550b2` test-fix already on main, `frosty-galileo-b4de9e` + `quizzical-spence-cc2d26` only dirty with briefing files already committed).
- Final state: `/Users/jrichter/CLAUDE/broadsheet` (main) and one active session worktree.

---

## [0.15.0] — 2026-05-13 (Session 30)

### Run log & earlier cron (Brief 17)

Diagnosing the 2026-05-13 run surfaced two operational gaps: 17 stories
were tagged "selected" in the Candidates database but only 10 reached the
email (the editor QA pass dropped 7, with no visible audit trail), and the
3am UTC cron consistently arrives after 7am London because GitHub Actions'
free-tier scheduler routinely delays scheduled workflows by 1-4 hours.
This release adds an always-visible per-run diagnostic page in Notion,
links to it (and the Candidates DB) at the top of each email, and moves
the cron 2h earlier to absorb the scheduler delay.

**Per-run diagnostic page in Notion (`tools/notion_delivery.py`)**
- New `write_run_log_page()` writes one row per run to a new *Run Logs*
  Notion database (database ID configured via `NOTION_RUN_LOG_DATABASE_ID`)
- Properties capture: stories selected, editor drops, published count,
  style rewrites/drops, source failures, LLM tier, run status
- Page body has three Notion toggle blocks: Editor drops, Style edits,
  Source events — populated by filtering today's lines from the existing
  `logs/editorial_drops.log`, `logs/style_check.log`, `logs/source_errors.log`
- Skipped silently if the env var is missing; failures are non-fatal
  (cannot break email delivery)

**Diagnostic links in the email (`tools/email_sender.py`)**
- New optional `candidates_url` and `run_log_url` parameters on
  `send_digest()`, `_build_html()`, `_build_plaintext()`
- Renders a small links strip below the existing "Run report" line in the
  email body with "Today's candidates →" and "Today's run log →" links
- HTML strip uses the same muted styling as the run report line;
  plaintext version gets matching plain URLs
- Each strip omits individual links if their URL is missing; the whole
  strip disappears if both are missing

**Pipeline stats threaded out for the run log**
- `agents/editor.py`: `write_digest()` now returns `(stories, label, drop_count)`
- `tools/style_check.py`: `run()` now returns `(stories, counts)` where
  `counts` has `flagged`, `rewritten`, `dropped` totals
- `main.py`: collects all stats into a `run_stats` dict, calls
  `write_run_log_page()` after Notion publish (before email) so the
  resulting page URL can be passed into `send_digest()`

**Cron moved 3am UTC → 1am UTC (`.github/workflows/daily_digest.yml`)**
- GitHub Actions free-tier scheduler routinely delays workflows by 1-4h
- 1am UTC start gives a wider buffer below the 6-7am UTC LLM congestion
  window that caused today's Gemini 503 cascade
- Workflow env block now also forwards `NOTION_RUN_LOG_DATABASE_ID`

**Test updates (`tests/test_style_check.py`, `tests/test_feedback_system.py`)**
- All `run(...)` and `write_digest(...)` callers updated for the new
  tuple return signatures. 36/36 tests pass.

**Files affected**
- `tools/notion_delivery.py` — new `write_run_log_page()` (~190 lines) and
  helpers for toggle/paragraph blocks and date-filtered log reading
- `tools/email_sender.py` — diagnostic links strip in HTML and plaintext
- `agents/editor.py` — return signature includes drop count
- `tools/style_check.py` — return signature includes counts dict
- `main.py` — wire stats collection, run log page write, URL passthrough
- `.env.example` — new `NOTION_RUN_LOG_DATABASE_ID` placeholder
- `.github/workflows/daily_digest.yml` — cron + new env var
- `tests/test_style_check.py`, `tests/test_feedback_system.py` — caller updates
- `briefings/brief-17-run-log-and-scheduling.md` — design brief
- `CHANGELOG.md`, `.claude/session.md` — this entry

**Manual setup required**
Create the *Run Logs* database in Notion (schema in brief 17), share it
with the existing integration, copy the database ID, then set
`NOTION_RUN_LOG_DATABASE_ID` in local `.env` and GitHub repo secrets.
Until that's done, the pipeline logs a skip and continues normally.

---

## [0.14.0] — 2026-05-12 (Session 29)

### Selection & style enforcement (Brief 16)

Today's digest (32 stories) violated Broadsheet's own rules in ~20 of 32 paragraphs —
the banned "For those tracking X, this signals Y" formula appeared throughout.
This release makes those rules deterministic.

**Style check (`tools/style_check.py` — new)**
- Regex blacklist of 6 banned patterns mirroring `agents.yaml:215-226`
- Two-stage pass: scan → LLM surgical rewrite → if still banned, drop sentence
- Word-count check: paragraphs >90 words get LLM tightening
- All rewrites and drops logged to `logs/style_check.log`
- Wired into `main.py` after Editor, before delivery

**Per-category hard caps (`config/digest.yaml`, `agents/newsroom_lead.py`)**
- `category_max`: big_news 5, critical_voices/laws_ethics/builder 8
- `rollover_max: 2` — each category may absorb up to 2 extra slots from leftover pool
- Replaces the previous shared soft-budget rollover that allowed critical_voices to reach 14

**AI-relevance filter at ingest (`tools/rss_fetcher.py`, `config/sources.yaml`)**
- `ai_only_filter: true` on Bellingcat, Less Wrong, 404 Media (whole-site feeds)
- Word-boundary keyword match against title+description at fetch time
- Drops non-AI items (Fibonacci posts, oil hedging, Mafia studies) before pipeline
- Logs drops to `logs/ingest_filter.log`

**Per-byline diversity cap (`agents/newsroom_lead.py`, `config/sources.yaml`)**
- 2 stories per named author per digest (alongside existing 4/source cap)
- `byline` field added to Cory Doctorow and Simon Willison feeds in sources.yaml
- Stories without `byline` fall back to source name

**Selection-time semantic dedup (`agents/newsroom_lead.py`)**
- LLM pass over final selection groups stories covering the same event
- Within each cluster: prefers non-paywalled if score within 1pt; else highest score
- No global paywall score penalty — paywalled singleton stories kept at full score
- Logs cluster decisions to `logs/dedup.log`

**`ai_subject` field + Editor QA pass (`config/agents.yaml`, `agents/editor.py`)**
- NewsroomLead must write `ai_subject`: one sentence naming the AI event/paper/ruling that is the subject (not an implication) of each story
- Editor checks `ai_subject` before writing; drops off-topic stories silently
- Drops logged to `logs/editorial_drops.log`; no footer in email
- Word target: 60–100 → 60–85 words

**Tests**
- `tests/test_style_check.py`: 27 tests (pattern detection, fixture violations, rewrite isolation, drop-on-failure)
- `tests/test_selection.py`: 25 tests (caps, rollover, byline cap, ai_only_filter, semantic dedup)
- `tests/fixtures/digest-2026-05-12.json`: 32-story fixture from today's run; 20+ stories contain banned patterns on first pass
- All 52 tests passing

---

## [0.13.1] — 2026-05-12 (Session 27)

### Switch outbound email from Gmail SMTP to Brevo SMTP

Gmail account used for outbound was suspended for bot-like login patterns
(server-triggered SMTP from GitHub Actions). Brevo is a transactional email
service — server-triggered sending is the product, not a tolerated edge case.
Free tier covers 300 emails/day; Broadsheet sends 5.

- `tools/email_sender.py`: transport switched from `smtplib.SMTP_SSL` on
  `smtp.gmail.com:465` to `smtplib.SMTP` + `starttls()` on
  `smtp-relay.brevo.com:587`. The single `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`
  pair becomes three env vars: `SENDER_EMAIL` (verified From address),
  `BREVO_SMTP_LOGIN` (auto-generated synthetic SMTP login from Brevo), and
  `BREVO_SMTP_KEY`. Module docstring updated.
- `.env.example`: Gmail block replaced with Brevo block — signup link,
  sender-verification step, and a note that the SMTP key is generated under
  *SMTP & API → SMTP* (not the API Keys tab).
- `.github/workflows/daily_digest.yml`: two Gmail secrets replaced with the
  three Brevo secrets in the `env:` block. Runner already pins Python 3.11.
- `README.md`: GitHub Secrets table and email-troubleshooting line updated to
  the new var names.

Validated locally on branch `claude/condescending-bassi-26f1e5` with a
single-recipient test send. Brevo accepted the message and Gmail delivered it
to the inbox.

---

## [0.13.0] — 2026-05-12 (Session 26)

### Replace Gmail inbox with Tavily for Substack sources

Gmail account suspended for bot-like IMAP login patterns from GitHub Actions.
Substack blocks cloud IPs on RSS (403), so direct feed fetching remains impossible.
Tavily (already in the stack) proxies the requests from its own infrastructure.

Probe script (`scripts/probe_substack_tavily.py`) confirmed: Tavily Extract
returns unparseable HTML fragments for RSS URLs; Tavily Search returns correct
recent articles for all 4 sources.

- `tools/inbox_fetcher.py`: `fetch_substack_inbox()` replaced with
  `fetch_substack_via_tavily()`. Reads domain/category from `sources.yaml`,
  calls `search_site()` with `days=2`, returns same story dict shape.
  All imaplib, IMAP credential, and seen-ID logic removed (~430 lines → ~60).
- `tools/tavily_search.py`: `search()` and `search_site()` gain optional `days`
  parameter, passed through to Tavily API.
- `config/sources.yaml`: `inbox_substack` block replaced with `tavily_substack`
  (domain-keyed). Four disabled RSS stubs removed.
- `main.py`: import and call site updated.
- `.env.example`: SMTP section note updated (IMAP no longer needed).
- `briefings/brief-15-brevo-migration.md`: brief for outbound SMTP migration
  to Brevo (Gmail outbound still used; migration pending user setup).

---

## [0.12.0] — 2026-05-11 (Session 25)

### Editor style guide rewrite

- `config/agents.yaml` `editor_agent`: backstory updated to "smart friend who happens to follow AI closely". Three new instructions added: lead with most important fact, include source name naturally, end with one implication sentence.
- Full writing rules added to instructions: British English, proper noun in sentence one, concrete specifics, short sentences, inline technical definitions, no source attribution formula.
- Model paragraph added as few-shot example (Anthropic/Claude blackmail story from TechCrunch).
- Banned phrases: "For those following AI...", "This matters because...", "According to the source...", "In a world where...", "As AI continues to...", generic "...for AI development" endings.

### Newsroom relevance gate + URL rules

- `config/agents.yaml` `newsroom_lead_agent`: AI relevance check added — must state in one sentence what specific AI event occurred; if not possible, score below 5. AI must be subject, not conclusion.
- All five agents (4 search + newsroom lead): reject homepages, category pages, and domain root URLs.

### Big News cap enforced

- `config/digest.yaml`: `big_news_max: 5` key added. Previously missing — code fell back to `max_stories // 4 = 8`.
- Budget logic already in `agents/newsroom_lead.py` from session 24; now wired to config key.
- `story_guidance` updated to reflect 5/10 category budgets.

### Per-agent thinking level (Gemini)

- `main.py`: `_build_llm_with_thinking()` helper added. Builds Gemini LLM with `extra_body={"thinkingLevel": level}`.
- `config/digest.yaml`: `agent_thinking` section added — `newsroom_lead: "high"`, `editor: "low"`.
- `main.py` `run()`: `newsroom_llm` built before story selection; both `select_stories()` calls use it. Falls back to standard `llm` if on fallback model.

### Source cleanup

- `config/sources.yaml`: `Tech Africa News` disabled (general African tech outlet, not AI-specific). `Al Jazeera` all-site feed disabled (pulls world news, not AI).
- `hacker_news` re-enabled in builder section.

### Domain blocklist + recency filter

- `config/digest.yaml` `filters`: `blocked_domains` list added — `linkedin.com`, `reddit.com`, `youtube.com`.
- `agents/newsroom_lead.py` `_score_heuristic()`: domain blocklist check added before topic blocklist check; matching URLs assigned score -999 and filtered.
- `agents/newsroom_lead.py` `select_stories()`: recency cutoff added after cross-day dedup — articles >14 days old dropped from `big_news`, `laws_ethics`, `critical_voices`. Builder exempt (practitioner posts take time to gain traction).

### Temperature defaults fixed

- `main.py`: hardcoded temperature defaults corrected `0.3 → 1.0` in all three `_build_llm*()` functions (Gemini, Cerebras, Groq). Config values already at 1.0 since session 24; defaults now match.

### Tavily query brief

- `briefings/brief-14-tavily-queries.md`: documents current queries, identifies quality problems per category, and poses four questions for user collaboration before queries are refined.

---

## [0.11.0] — 2026-05-07 (Session 24)

### Category-aware heuristic scoring

- `agents/newsroom_lead.py` `_score_heuristic()`: editorial categories (`critical_voices`, `laws_ethics`) now score on recency + tag affinity + source bonus only. Authority, engagement (HN/Reddit/GitHub points), and cluster bonus removed for these categories — they unfairly disadvantaged small publications with no social footprint.
- Standard categories (`big_news`, `builder`) use the unchanged full formula.
- `_EDITORIAL_CATS = {"critical_voices", "laws_ethics"}` constant defined at module level.

### Per-category heuristic cutoff

- `agents/newsroom_lead.py`: single global top-45 pool replaced with per-category cuts. Editorial categories get 20 candidates each; standard categories get 12. Total LLM candidate pool is now ~64 (was 45).
- `config/digest.yaml`: `llm_rerank_top_n: 12` (was 45 — now per-category for standard); `llm_rerank_top_n_editorial: 20` added.

### Builder sources paused

- `config/sources.yaml`: `hacker_news`, `reddit_rss`, `github_trending`, `product_hunt` disabled. `tavily_queries` cleared. Individual feeds disabled: arXiv cs.AI, arXiv cs.CL, Hugging Face Blog, Towards Data Science.
- Active builder feeds: Marktechpost, Simon Willison's Weblog, Andrej Karpathy, plus one user-added source.

### Bug fix — `candidates` NameError in candidate log

- `tools/notion_delivery.py` `write_candidates_log()` line 462: `len(candidates)` raised `NameError` — `candidates` is not defined in that scope. Corrected to `len(all_stories)`.

### New utility script

- `tools/reset_stories_db.py`: one-shot script to archive all pages in the Notion Stories database (`NOTION_DATABASE_ID`), resetting the cross-day dedup lookback window.

---

## [0.10.0] — 2026-05-07 (Session 23)

### Selection quality improvements

**Diversity floor raised 4→6** (`agents/newsroom_lead.py`, `config/digest.yaml`):
`diversity_min_quality` now reads from `digest.yaml` and defaults to 6, matching `min_confidence_score`. Fixes perverse selection where confidence-5 stories won over confidence-9 via diversity slots and source-dupe displacement.

**Source de-dupe softened** (`agents/newsroom_lead.py`):
Duplicate-source story now survives if it is ≥2 confidence points better than the best available replacement. Previously any alternative at quality ≥4 was enough to displace it.

**Hard cap on big_news and builder** (`agents/newsroom_lead.py`):
Both categories hard-capped at `base_budget` (10) stories — no rollover in or out. `laws_ethics` and `critical_voices` continue to share their combined remaining budget.

**Job ad hard block** (`config/digest.yaml`, `agents/newsroom_lead.py`):
Ten job ad keywords added to `blocked_topics` (score -999, excluded before LLM). LLM rerank prompt also explicitly instructed to reject job postings not caught by keyword filter.

### No-feed site Tavily search

- `tools/tavily_search.py`: `search()` gains `include_domains` parameter; new `search_site(domain, query, category)` helper.
- `agents/search_agents.py`: all four gather functions read `no_feed_sites` from `sources.yaml`, dispatch per-site Tavily queries with correct category tags. Bug found in review: three agents had `category="big_news"` hardcoded — fixed.
- `config/sources.yaml`: `no_feed_sites` skeleton (all entries `enabled: false`) and `no_feed_default_queries` list added to all four categories.

### Paywall flag

- `config/digest.yaml`: `known_paywalls` domain list (user-maintainable).
- `agents/newsroom_lead.py`: `paywalled: bool` attached to every story dict before any filtering.
- `tools/email_sender.py`: `[💸 Paywall]` badge rendered after story title for paywalled stories.
- `tools/notion_delivery.py`: `Paywalled` checkbox added to Candidates DB row.

### Pre-selection Notion monitor (Candidates DB expanded)

- `tools/notion_delivery.py` `write_candidates_log()`: now accepts all raw stories (not just heuristic top-45); clears the entire database at run start (replaces 30-day purge); writes `Stage Dropped` (text) and `Paywalled` (checkbox) properties per row.
- `agents/newsroom_lead.py` `select_stories()`: `stage_dropped` dict tracks elimination reason at each stage — `url_dedup`, `cross_day_dedup`, `heuristic_cutoff`, `confidence_floor`, `not_selected`; all raw stories passed to Notion writer.

**Note**: Add `Stage Dropped` (Text) and `Paywalled` (Checkbox) properties to the Notion Candidates database schema before next run.

---

## [0.9.1] — 2026-05-06 (Session 22, hotfixes)

### Hotfix 1 — `source_scores: None` crash (`agents/newsroom_lead.py`)
YAML parses a key with only comments beneath it as `None`, not `{}`. `cal.get("source_scores", {})` returned `None` because the key existed — the default only fires when the key is absent entirely. Fixed all three callsites to `or {}`.

### Hotfix 2 — `NOTION_CANDIDATES_DATABASE_ID` not injected (`.github/workflows/daily_digest.yml`)
Secret was present in GitHub repo secrets but not listed in the workflow `env:` block. GitHub Actions requires explicit listing. Added.

---

## [0.9.0] — 2026-05-06 (Session 22)

### Feedback model rebuild — voting removed, source affinity + filters + candidate log added

**WS1 — 👍/👎 voting removed:**
- `tools/email_sender.py`: removed CSS `:checked` trick, hidden checkbox inputs, interactive vote-link paragraphs, and 👍/👎 plaintext line. Title click-through (`v=top`) is the only remaining feedback mechanism.
- `feedback_server/api/feedback.py`: handler simplified to validate `v=top` → fetch story from Notion (title + source + tags) → write vote row → 302 redirect. Deleted `_query_votes()`, `_delete_row()`, MPP guard, idempotent guard, top-pick guard. Added `Story Source` rich_text property to vote row.
- `tools/notion_delivery.py` `read_multi_feedback()`: added `Vote = top` filter to Notion query; extracts `Story Source` from each row; returns `source_feedback: dict[str, int]` alongside `tag_feedback`.

**WS2 — Source affinity (click-through → slow-accumulating per-source score):**
- `agents/newsroom_lead.py` `update_calibration_from_feedback()`: tag weight simplified to `top`-only signal. New source affinity block: decay ×0.978/run (≈×0.85/week), +0.1 per click, cap 1.0, pruned below 0.005.
- `agents/newsroom_lead.py` `_score_heuristic()`: source bonus = `min(0.5, source_scores.get(source, 0))`.
- `agents/newsroom_lead.py` `_format_feedback_summary()`: sources with score >0.2 surfaced in LLM rerank prompt.
- `config/calibration.yaml`: added `source_scores:` section.

**WS3 — User-editable content filters:**
- `config/digest.yaml`: added `filters:` block — `blocked_topics` (keyword match, excluded pre-LLM, score set to -999) and `deprioritized_sources` (exact match, -2.0 heuristic penalty).
- `agents/newsroom_lead.py` `_rerank_with_llm()`: filter context injected into LLM prompt.

**WS4 — Candidate audit log (Notion):**
- `tools/notion_delivery.py`: new `write_candidates_log()` — posts heuristic top-45 candidates to Broadsheet Candidates database; purges rows >30 days; skips silently without `NOTION_CANDIDATES_DATABASE_ID`.
- `agents/newsroom_lead.py` `select_stories()`: calls `write_candidates_log()` after diversity slot.
- `.env.example`: added `NOTION_CANDIDATES_DATABASE_ID=` with comment block.
- `.github/workflows/daily_digest.yml`: added `NOTION_CANDIDATES_DATABASE_ID` to env block.

**Tests:** `tests/test_feedback_system.py` — all 9 tests updated and passing. Fixed non-deterministic hash-based source names in fixtures (Python 3.3+ hash randomisation caused per-source-cap collisions between runs).

---

## [0.8.2] — 2026-05-05 (Session 20, revert)

### Reverted v0.8.1 silent-feedback removal

v0.8.1 incorrectly diagnosed the cause of "👍/👎 votes not registering" and removed the Session 9 CSS `:checked` silent-feedback design. That design was a deliberate decision — it makes voting silent on Apple Mail and iOS Mail (no browser tab opens). Removing it broke an explicit product behaviour without confirming the actual cause.

**Reverted to pre-v0.8.1 design:**
- `tools/email_sender.py` — restored CSS pre-pass, hidden checkboxes, labels, and 1px ping spans for Apple Mail / iOS Mail. Plain `<a href>` fallback links (hidden by CSS in clients that support `<style>`, visible in Gmail) are unchanged from the original.
- `feedback_server/api/feedback.py` — restored 30-second MPP guard. Original Session 9 logic preserved.

**Kept from v0.8.1 (legitimate fixes):**
- `tools/notion_delivery.py` `_feedback_signal()` — Votes DB stores plain text ("good"/"less"/"top"), function previously only matched emoji strings. The plain-text match is correct and unrelated to the silent-feedback design.
- `tools/tavily_search.py` `quota_exceeded` flag + `[TAVILY-QUOTA]` log marker.
- `main.py` — Tavily quota wired into `run_report["sources"]`.

**Open question:** The user's reported symptom (👍/👎 not appearing in the Votes DB) remains undiagnosed. Possible causes to investigate next: (a) Apple Mail no longer fires CSS state-triggered background-image URLs (privacy update); (b) MPP pre-fetches both opposing CSS URLs at email open and the 30-second guard cancels them both out; (c) a regression introduced by Brief 11's per-subscriber refactor. Need Vercel function logs from a real test click to distinguish.

---

## [0.8.1] — 2026-05-05 (Session 20, patch — superseded by 0.8.2)

### Voting fix + Tavily quota alerting

**Issue 1 — 👍/👎 votes not registering:**

Root cause was two separate bugs:

1. **Read side** (`tools/notion_delivery.py`): `_feedback_signal()` matched emoji strings ("👍 More like this") but the Votes database stores plain text ("good", "less", "top"). Fixed by adding plain-text values to each condition.

2. **Write side** (`tools/email_sender.py`, `feedback_server/api/feedback.py`): ~~The CSS `:checked` + `background-image` ping approach from Session 9 is unreliable~~ — **this diagnosis was wrong; reverted in 0.8.2.** The CSS approach was a deliberate design decision for silent feedback on Apple Mail. The actual cause of the symptom is not yet identified.

**Issue 2 — Tavily budget visibility** (`tools/tavily_search.py`, `main.py`, `tools/email_sender.py`):

Added a module-level `quota_exceeded` flag to `tavily_search.py`, set to `True` when any search call receives a quota/limit/credit/402/429 response. `main.py` reads this flag after the gather phase and includes it in the run report's `sources` field. The run report rendered in each digest email now shows the Tavily note when the quota is hit. Log marker: `[TAVILY-QUOTA]`.

Note: Tavily's 1000 credits/month translates to ~10,000 searches/month (10 searches = 1 credit). At ~240 searches/month actual usage, the limit is not an urgent concern.

---

## [0.8.0] — 2026-05-05 (Session 20)

### Brief 10 — Scoring architecture overhaul (C1, C2, C4, C5, C6)

Replaced the LLM-as-absolute-scorer pattern with a two-stage pipeline: deterministic heuristic funnel → single LLM pairwise rerank. Token usage drops ~70–80%.

**C2 — Topic clustering before scoring:**
- `agents/newsroom_lead.py`: new `_cluster_stories()` — TF-IDF + cosine similarity on titles (threshold 0.35). One representative per cluster (most recent story). Stories covering the same topic collapse to a single candidate, reducing noise and scoring cost.
- Cluster size feeds a trending bonus in the heuristic stage: when 5 outlets cover a story, that weight compounds.
- `requirements.txt`: added `scikit-learn>=1.3.0`.

**C1 — Heuristic-first scoring + LLM pairwise rerank:**
- `agents/newsroom_lead.py`: new `_score_heuristic()` — scores all candidates on source authority, engagement (HN points/Reddit upvotes/GitHub stars), recency (1.0 bonus <24h, 0.5 bonus 24–48h), tag affinity from feedback calibration, cluster trending bonus. No LLM, no cost.
- `agents/newsroom_lead.py`: new `_rerank_with_llm()` — single LLM call on top 45 heuristic candidates. Pairwise-style relative ranking (not independent 1–10 scores), which is more reliable for pre-filtered candidates. Rank mapped to confidence_score 5–10.
- Replaced `_score_in_chunks()` and chunked scoring loop. Old multi-chunk approach also removed.
- `config/digest.yaml`: added `heuristic_weights` block (engagement_weight, recency_bonus_24h, recency_bonus_48h, tag_affinity_weight, llm_rerank_top_n=45).
- `config/agents.yaml`: removed conservative scoring anchor ("It's better to have 8 outstanding stories than 20 mediocre ones") — no longer load-bearing with relative ranking.

**C4 — Cross-category re-categorization:**
- LLM rerank prompt now includes a `category` field in output. Stories can be re-assigned from their fetch-time category to the most accurate category based on content. Falls back to original category if LLM omits or misnames.

**C5 — Multiple diversity slots:**
- Diversity slot loop now iterates over all missing categories (not just rescuing one). If 2–3 categories are absent after the budget pass, all get their best above-floor story added.

**C6a — Token budget tracking:**
- `tools/token_tracker.py` (new): estimates tokens (chars/4), logs daily totals per provider to `logs/token_usage.json`. Warns at startup if any provider was within 20% of its free-tier daily cap yesterday.
- `agents/newsroom_lead.py`: `_rerank_with_llm()` logs each call via `token_tracker.log_call()`.
- `main.py`: calls `check_yesterday_usage()` at startup.
- `.gitignore`: added `logs/token_usage.json`.

**C6b — Cerebras chunk-size test script:**
- `scripts/test_cerebras_chunk_size.py` (new): one-off script that sends progressively larger batches to Cerebras until it errors, then reports the safe chunk size.

**Files changed:**
- `agents/newsroom_lead.py`
- `tools/token_tracker.py` (new)
- `scripts/test_cerebras_chunk_size.py` (new)
- `requirements.txt`
- `config/digest.yaml`
- `config/agents.yaml`
- `main.py`
- `.gitignore`

---

## [0.7.0] — 2026-05-04 (Session 19)

### Dedicated Gmail + Substack inbox polling (Brief 12)

Replaced the Substack RSS feeds (blocked on GitHub Actions cloud IPs with 403) with IMAP inbox polling from a dedicated Broadsheet Gmail account.

**New:**
- `tools/inbox_fetcher.py` — connects to Gmail via IMAP, fetches unread emails, parses each into a story dict (title, url, source, published_iso, description, category). Canonical post URL extracted by matching `/p/` pattern in anchor tags, with query params stripped. Marks emails as read after processing. Routes failures through `source_monitor`.
- `config/sources.yaml`: new `inbox_substack` block with sender→source→category mapping. To add a new Substack source: subscribe from the Broadsheet Gmail and add one entry to this block — no code change needed.

**Changed:**
- `config/sources.yaml`: disabled 4 Substack RSS entries (Gary Marcus, Import AI, Interconnects, Latent Space) — set `enabled: false` with explanatory comment.
- `main.py`: loads `sources.yaml`, calls `fetch_substack_inbox()` after agent gather phase, merges inbox stories into candidate pool before scoring.

**Also this session:**
- `.github/workflows/daily_digest.yml`: upgraded actions to Node.js 24 compatible versions (checkout@v4→v6, setup-python@v5→v6, upload-artifact@v4→v6) ahead of GitHub's June 2 2026 deadline.
- `.github/workflows/daily_digest.yml`: removed unused `DIGEST_RECIPIENT_EMAILS` env var (recipients come from `config/digest.yaml`).

**Files changed:**
- `tools/inbox_fetcher.py` (new)
- `config/sources.yaml`
- `main.py`
- `.github/workflows/daily_digest.yml`
- `.env.example`
- `docs/RUNBOOK.md`
- `docs/DECISIONS.md`

---

## [0.6.2] — 2026-05-04 (Session 18 follow-up)

### Substack RSS workaround + stale reference cleanup

**`tools/rss_fetcher.py`:**
- User-Agent changed from `"Broadsheet/1.0 (personal news digest; contact via GitHub)"` to `"Mozilla/5.0 (compatible; Broadsheet/1.0; +https://github.com/pagancrew/broadsheet)"` — Mozilla-prefix passes basic anti-bot checks; self-identifying string preserves honesty.
- Added 3-attempt retry with exponential backoff (1s, 3s, 7s) around the `requests.get()` call. All 3 must fail before the feed is counted as an error.

**`config/digest.yaml`:** `schedule.time` corrected from `"07:00"` to `"04:00"` (informational field only; actual cron is `0 3 * * *` in the workflow file).

**`briefings/github-deployment.md`:** Added `DEPLOYED 2026-05-04` callout at the top.

**Files changed:**
- `tools/rss_fetcher.py`
- `config/digest.yaml`
- `briefings/github-deployment.md`
- `.claude/session.md`

---

## [0.6.1] — 2026-05-04 (Session 18)

### GitHub Actions deployment + repo cleanup

**Deployment:**
- First successful GitHub Actions run confirmed. Secrets added by user; scheduled run fires at 3am UTC (4am London BST).
- `DIGEST_RECIPIENT_EMAILS` confirmed unused — recipients read from `config/digest.yaml` directly; secret not needed.

**Repo cleanup:**
- `.claude/worktrees/` added to `.gitignore`; `affectionate-heisenberg-5550b2` worktree directory removed from git tracking. Was causing `fatal: No url found for submodule path` warning on every Actions post-job cleanup.

**Documentation:**
- `docs/RUNBOOK.md` (new) — operator instructions for adding subscribers, adding sources, pushing changes, updating docs. Includes privacy warning about subscriber emails in `config/digest.yaml`.
- `README.md` — corrected schedule time, provider chain, secrets list, Notion schema (Tags), feedback section, recipients format, troubleshooting
- `CLAUDE.md` — corrected model chain and delivery description; added privacy constraint reminder

**Files changed:**
- `.gitignore`
- `docs/RUNBOOK.md` (new)
- `README.md`
- `CLAUDE.md`

---

## [0.6.0] — 2026-05-03 (Session 17)

### Multi-subscriber feedback — Brief 11

Votes from different subscribers now accumulate independently in a separate Notion "Votes" database, one row per vote per subscriber. The calibration pipeline aggregates across all rows by tag. The existing Stories.Feedback property is no longer written to.

**New:**
- `tools/notion_delivery.py`: `read_multi_feedback(days_back)` — queries Votes db, aggregates by tag across all subscribers, returns same shape as `read_feedback()`. Graceful no-op if `NOTION_VOTES_DATABASE_ID` unset.

**Changed:**
- `feedback_server/api/feedback.py`: Full rewrite. Reads `&sub=` param (default "owner"). Writes one row per vote to Votes db. Per-subscriber MPP guard and top-pick guard (both query Votes db, not Stories db). Stops writing to Stories.Feedback.
- `tools/email_sender.py`: Sends one individual email per subscriber (not a group email). `_build_html()` and `_build_plaintext()` gain `subscriber_id` param; `&sub=` injected into all feedback URLs. `_coerce_recipient()` handles legacy plain-string format.
- `main.py`: Uses `read_multi_feedback()` instead of `read_feedback()`. Passes subscriber dicts to `send_digest()`.
- `config/digest.yaml`: Fixed broken YAML structure (email block was outside delivery:, mixed tabs/spaces). Recipients updated to dict format with `email` and `subscriber_id` (no `name`).

**Infrastructure:**
- Connected GitHub repo (`pagancrew/broadsheet`) to Vercel project
- Set Vercel root directory to `feedback_server/`
- `NOTION_VOTES_DATABASE_ID` added to Vercel environment variables and local `.env`

**Files changed:**
- `tools/notion_delivery.py`
- `feedback_server/api/feedback.py`
- `tools/email_sender.py`
- `main.py`
- `config/digest.yaml`

---

## [0.5.0] — 2026-05-02 (Session 16)

### Source list expansion — Brief 10 Fix 3 (C3)

Config-only change. Adds 13 RSS feeds across three categories, addressing the structural weakness identified in Session 15's architecture critique: Big News had zero primary AI lab blogs; Builder had no academic or newsletter sources; Laws & Ethics coverage was geographically narrow.

**Big News** — 4 new enabled feeds (1 disabled pending verification):
- DeepMind Blog, Import AI (Jack Clark), OpenAI Blog, Stratechery — enabled
- Anthropic News — added but `enabled: false` (user preference)

**Laws & Ethics** — 2 new enabled feeds (2 disabled pending verification):
- Ada Lovelace Institute, Tech Policy Press — enabled
- Lawfare AI, Stanford HAI — added but `enabled: false` (user preference)

**Builder** — 4 new feeds, all enabled:
- arXiv cs.AI, arXiv cs.CL (academic papers feeds)
- Interconnects (Nathan Lambert), Latent Space (Swyx) (ML practitioner newsletters)

**Critical Voices** — no changes (no links available in the briefing for candidate feeds)

**Files changed**
- `config/sources.yaml`

---

## [0.4.9] — 2026-05-02 (Session 15)

### Remove my_sources.yaml

`config/my_sources.yaml` and the `_merge_sources()` function that loaded it have been removed. The file existed to give users a "safe" place to add custom sources without touching the curated list — unnecessary once the user is comfortable editing `sources.yaml` directly. Edit `config/sources.yaml` for all source changes going forward.

**Files changed**
- `agents/search_agents.py`: removed 6-line merge block from `_load_config()` and deleted `_merge_sources()` function entirely
- `config/sources.yaml`: removed comment directing users to `my_sources.yaml`
- `tools/source_monitor.py`: removed `my_sources.yaml` reference from dead-source alert message
- `README.md`: removed `my_sources.yaml` section and example block; updated file tree and source-alert fix instructions
- `CLAUDE.md`: removed `my_sources.yaml` row from key files table
- `docs/DECISIONS.md`: removed `my_sources.yaml` reference from ADR-005

---

## [Planning] — 2026-05-02 (Session 15)

Architecture critique and planning session. No code changes.

**Critique:** Identified four structural weaknesses — LLM absolute scoring is the wrong tool for ranking candidates; deduplication is URL-only and misses same-story multi-outlet coverage; source list is too narrow (5 Critical Voices feeds, zero AI lab blogs, no newsletters or academic feeds); category assignment is source-based rather than content-based.

**Brief 10 written** (`briefings/General-fixes-10 quality and token efficiency.md`): Seven fixes across scoring architecture, topic clustering, source expansion, cross-category reassignment, multi-slot diversity, token budget tracking, and optional equal-scoring retry. User and Claude tasks split for parallel execution.

**Brief 11 written** (`briefings/General-fixes-11 multi-subscriber feedback.md`): Multi-subscriber feedback via a separate Notion Votes database. Per-subscriber emails with subscriber IDs in feedback URLs. Equal vote weighting, private feedback. Calibration formula unchanged — aggregation layer added below it. Sequenced after Brief 10.

**No files changed (code).**  
**New files:** `briefings/General-fixes-10 quality and token efficiency.md`, `briefings/General-fixes-11 multi-subscriber feedback.md`

---

## [0.4.8] — 2026-05-01 (Session 14)

### Story selection improvements + GEMINI_API_KEY false alarm fix

Three changes from live-run analysis that diagnosed why only 22 of a targeted 40 stories were selected and why source diversity was low.

**False GEMINI_API_KEY warning fix (`main.py`):** `_build_llm()` logs "GEMINI_API_KEY not set" when the primary-branch condition fails. The condition `if gemini_key and not force_fallback` is false in two cases: key missing, or intentional `force_fallback=True`. The startup pre-build of the Cerebras client hits case 2 and was printing a misleading alarm even on runs where Gemini worked correctly. Fixed by changing `else:` to `elif not gemini_key:`.

**Per-source cap tightened (`agents/newsroom_lead.py`):** Changed from hardcoded 6 to `ceil(base_budget / 3)` = 4. Prevents a single high-volume feed from dominating a category. Cap value logged on each run.

**Diagnostic logging added (`agents/newsroom_lead.py`):** Three new log lines after scoring: scores returned vs. sent (detects LLM truncation), score distribution by band (9-10 / 7-8 / 5-6 / 1-4), and per-category count above the quality floor.

**Quality floor replaces global threshold for selection (`agents/newsroom_lead.py`):** The category budget pool now uses `diversity_min_quality` (4) as the filter floor instead of `min_confidence_score` (6). Each category selects its best `base_budget` stories from this wider pool. Stories scoring 1-3 are still dropped as junk. `max_stories`, `categories`, `base_budget`, `quality_floor`, and `per_source_cap` are now computed once at function entry and reused throughout.

**Files changed**
- `main.py`: `else:` → `elif not gemini_key:` in `_build_llm()`
- `agents/newsroom_lead.py`: per-source cap formula, diagnostic logging, quality floor selection

---

## [0.4.7] — 2026-05-01 (Session 13)

### Workflow secrets cleanup

Removed stale `NVIDIA_API_KEY` and `OPENROUTER_API_KEY` from the GitHub Actions workflow `env:` block (left over from before Brief 08 replaced those providers). Added `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, and `GROQ_API_KEY` so the runner has the correct keys at runtime.

**Files changed**
- `.github/workflows/daily_digest.yml`

---

## [0.4.6] — 2026-05-01 (Session 13 — Brief 09)

### Off-peak scheduling + cross-day Notion deduplication

Two operational fixes that reduce noise and provider load.

**Fix 1 — Off-peak scheduling:** Shifted the GitHub Actions cron from two triggers (6am + 7am UTC to target 7am London across GMT/BST) to a single `0 3 * * *` trigger. 3am UTC is 4am London BST / 3am London GMT — well before the working day and well outside the 6–7am UTC peak-load window for Gemini, Cerebras, and Groq free-tier pools.

**Fix 2 — Cross-day deduplication:** RSS feeds have 48–72h lookback windows, so the same article could appear in consecutive digests. Before LLM scoring, the system now fetches the set of story URLs already published to Notion in the last 7 days and drops any candidate that matches. Verified live: found 8 prior URLs; candidate pool correctly filtered.

**Changed**
- `.github/workflows/daily_digest.yml`: Two cron triggers replaced with one (`0 3 * * *`). Comment block updated.
- `tools/notion_delivery.py`: Added `read_published_urls(days_back=7) -> set`. Queries all Notion pages in the last N days (no Feedback filter), paginates via `has_more`/`start_cursor`, returns set of URL strings. Fails safe (returns empty set on any error).
- `agents/newsroom_lead.py`: Added `published_urls=None` param to `select_stories()`. Filter applied after within-run URL dedup and before per-source cap. Docstring updated (provider names corrected to Gemini/Cerebras/Groq).
- `main.py`: Added `read_published_urls` to import; calls it alongside `read_feedback()` inside the `no_notion` guard; passes `published_urls` to both `select_stories()` calls (initial + low-confidence retry).

**Verified**
- Import check: clean ✓
- Dry run: `Cross-day dedup: 8 previously-published URLs (last 7 days)` log line appeared; pipeline completed normally ✓

---

## [0.4.5] — 2026-05-01 (Session 13 — Brief 08-2)

### Editor agent fallback chain

Gives the editor agent the same three-tier provider resilience already in place for scoring. Previously `write_digest()` retried up to 3× on a single LLM before falling back to raw RSS descriptions. Now it steps through primary → fallback → tertiary (Gemini → Cerebras → Groq) before giving up, matching the scoring chain exactly.

**Changed**
- `agents/editor.py`: `write_digest()` gains `fallback_llm=None` and `tertiary_llm=None` parameters. The 3-attempts-on-one-model loop is replaced by a tier chain; `editor_label` records which tier succeeded or `"raw descriptions (all tiers failed)"`. `attempts_needed` variable removed.
- `main.py`: `write_digest()` call now passes `fallback_llm` and `tertiary_llm` (already constructed at startup for scoring — no new LLM clients built).

**Pre-check note:** The hardcoded "Nemotron credits exhausted" string mentioned in the brief was already removed by Brief 08. No change needed.

**Files changed**
- `agents/editor.py`
- `main.py`

---

## [0.4.4] — 2026-05-01 (Session 13 — Brief 08)

### Provider replacement: Gemini primary + Cerebras fallback

Replaces NVIDIA NIM (Nemotron) and OpenRouter with two always-free providers. NVIDIA NIM's free tier is a one-time credit grant with no monthly refresh; OpenRouter's shared free pool was unreliable under load. With Gemini + Cerebras + Groq, all three scoring tiers are genuinely always-free.

**New provider chain:**
1. Primary: Gemini (`gemini/gemini-3.1-flash-lite-preview`) — 250 RPD, 1M context, no credit card
2. Fallback: Cerebras (`qwen-3-235b-a22b-instruct-2507`) — ~1M tokens/day, no credit card (llama-3.3-70b absent from this account; qwen-3-235b-a22b-instruct-2507 used instead)
3. Tertiary: Groq — unchanged
4. Last resort: equal scoring — unchanged

**Changed**
- `main.py`: `_build_llm()` rewritten (Gemini primary, Cerebras fallback; NVIDIA/OpenRouter removed). Run-report resolver fixed: when startup falls back, "primary" label now correctly maps to the fallback model name rather than the config primary name.
- `agents/editor.py`: Removed hardcoded "Nemotron credits exhausted" from fallback note — now provider-agnostic.
- `config/digest.yaml`: `models.primary` → Gemini; `models.fallback` → Cerebras qwen model.
- `.env.example`: Replaced NVIDIA and OpenRouter sections with Gemini and Cerebras.
- `requirements.txt`: Added `crewai[google-genai]>=0.80.0` (required for Gemini native provider).
- `docs/DECISIONS.md`: ADR-003 marked superseded by ADR-014.

**Verified**
- Primary path (real keys): all labels show `gemini/gemini-3.1-flash-lite-preview` ✓
- Fallback path (bogus Gemini key): all labels show `cerebras/qwen-3-235b-a22b-instruct-2507` ✓

---

## [0.4.3] — 2026-05-01 (Session 13 — Brief 07)

### Scoring robustness — payload reduction and chunked scoring

Fixes the 29K-token scoring payload that caused NVIDIA NIM to 504 timeout and Groq to hard-reject the request. Also a prerequisite for Brief 08 (Cerebras has an 8K-per-request cap).

**Part 1 — Smaller payload:**
- Added per-source cap: at most 6 stories kept per source after deduplication. Reddit feeds (r/LocalLLaMA: 23 entries, r/singularity: 22) were dominating the prompt. Expected reduction: 238 → ~100–120 candidates.
- Compressed per-story descriptions from 300 → 100 chars in `stories_for_llm`.
- Dropped `published` field from `stories_for_llm` (scoring LLM doesn't use it).
- Combined effect: ~29K → ~7–9K tokens per scoring call.
- Added two log lines: `Stories before/after per-source cap` and `Scoring payload size: N chars`.

**Part 2 — Chunked scoring:**
- Added `_score_in_chunks(llm, messages, stories, chunk_size, inter_chunk_delay=15)` helper that slices the story list, substitutes the chunk into a `__STORIES_PLACEHOLDER__` in the user message, calls the LLM once per chunk, sleeps 15s between chunks for TPM pacing, and concatenates results.
- Refactored `user_prompt` to use `__STORIES_PLACEHOLDER__` instead of inline `json.dumps()`.
- All three scoring branches now call `_score_in_chunks()` with tier-appropriate chunk sizes: primary=200 (Gemini, effectively one chunk), fallback=60 (Cerebras 8K cap), tertiary=80 (Groq 12K TPM).

**Changed**
- `agents/newsroom_lead.py`: per-source cap block, `stories_for_llm` builder, `user_prompt`, `_score_in_chunks` helper, three scoring branches

---

## [0.4.2] — 2026-05-01 (Session 13 — Brief 06)

### TypeError crash fix + fail-forward speed

Two fixes that unblock bad LLM days.

**Fix 1 — TypeError in sort (`agents/newsroom_lead.py`)**

Sort keys comparing `published_iso` used `x.get("published_iso", "")`, which returns `None` (not `""`) when the key is present with value `None`. Several scrapers store `None` explicitly. On equal-scoring days every story gets the same confidence score, forcing date comparison on every pair — hitting `None < "2026-04-30"` → `TypeError` → abort. Changed all three sort key lambdas (lines 255, 265, 279) to `(x.get("published_iso") or "")`, which coerces both `None` and absent to `""`.

**Fix 2 — Fail-forward speed (`main.py`)**

LiteLLM's default 6-retry exponential back-off held each tier hostage for ~20 minutes before failing. Added `num_retries=1` to all three `LLM()` constructors (primary, fallback, tertiary) so a failed tier bails in seconds and the next tier is tried promptly.

**Changed**
- `agents/newsroom_lead.py`: three sort key lambdas — `x.get("published_iso", "")` → `(x.get("published_iso") or "")`
- `main.py`: `num_retries=1` added to primary, fallback, and tertiary `LLM()` constructors

---

## [Planning] — 2026-05-01 (Session 12)

Diagnosis and planning session. No code changes. Briefing files written for the next round of fixes.

**Diagnosed from 2026-04-30 test run:**
- Latent `TypeError` in `agents/newsroom_lead.py` sort (published_iso=None vs string comparison); surfaces only when equal scoring is used
- Groq tertiary structurally unfit at current payload size (29K tokens vs 12K TPM cap)
- NVIDIA NIM free credits confirmed as one-time grant, not monthly refresh — provider replacement required before automation

**Decisions recorded:** ADR-014 (new provider chain: Gemini → Cerebras → Groq)

**Briefings written:** General-fixes-06 through 09, 08-2 (supersedes 05)

---

## [0.4.0] — 2026-04-30

### Tertiary scoring LLM (Session 11 — Brief 03)

The Broadsheet scoring chain previously had two tiers: primary (Nemotron via NVIDIA NIM) and fallback (OpenRouter / Llama 3.3 70B). When both failed simultaneously (as on 2026-04-28), the system fell straight to equal scoring, which exposed a feed-order selection bias. This release adds a third tier — Groq's free-tier Llama 3.3 70B — so the system has one more real-scoring attempt before giving up.

**Why Groq:** Originally planned with Claude Haiku 4.5 (Anthropic). Switched to Groq because Anthropic requires a paid deposit and this version of Broadsheet is free-tier only. Groq offers 14,400 requests/day with no credit card. Reasoning recorded in `docs/DECISIONS.md` (ADR-013).

**New scoring chain:**
1. Primary: Nemotron via NVIDIA NIM
2. Fallback: OpenRouter / Llama 3.3 70B
3. Tertiary: Groq / Llama 3.3 70B *(new)*
4. Last resort: equal scoring (all stories score 6)

**Changed**
- `main.py`: New `_build_tertiary_llm()` function. `run()` builds `tertiary_llm` after `fallback_llm` and passes it to `select_stories()`. `run_report` resolver extended with `"tertiary"` → `"groq/<model>"` mapping so the email run-report line shows the correct model when tertiary fires.
- `agents/newsroom_lead.py`: `select_stories()` accepts `tertiary_llm=None`. Exception chain restructured from a nested two-branch try/except to four sequential `if scored is None` checks (primary → fallback → tertiary → equal). Cleaner control flow and easier to extend further if needed.
- `config/digest.yaml`: New `models.tertiary` block (provider: groq, model: llama-3.3-70b-versatile, temperature: 0.3).
- `.env.example`: New `GROQ_API_KEY` entry with sign-up link and rate-limit note.

**User action required**
- Add `GROQ_API_KEY` to local `.env` (and to GitHub repo secrets when GitHub Actions deployment is enabled). Free key from https://console.groq.com — no credit card.

**Verification**
- Smoke test against Groq API succeeded — `_build_tertiary_llm()` constructs the LLM correctly, returns 'ok' on a test call, ~170 ms latency.
- Both modified files import cleanly.
- Tertiary tier is optional — if `GROQ_API_KEY` is unset, the system silently skips it and falls through to equal scoring as before.

**Briefings closed**
- `briefings/General-fixes-03 tertiary scoring.md` — marked Complete.
- `briefings/General-fixes-04 Diagnosing output.md` — marked Complete (work was actually done in Session 7 / CHANGELOG 0.3.1; brief file was never updated).

**Files changed**
- `main.py`
- `agents/newsroom_lead.py`
- `config/digest.yaml`
- `.env.example`
- `briefings/General-fixes-03 tertiary scoring.md`
- `briefings/General-fixes-04 Diagnosing output.md`
- `CHANGELOG.md`
- `docs/DECISIONS.md`
- `.claude/session.md`

---

## [0.3.4] — 2026-04-30

### Top pick cannot be superseded by later 👍/👎 (Session 10)

Clicking a story title records ⭐ Top pick — the highest-value feedback signal. Previously, an accidental 👍 or 👎 click after opening the link would overwrite it. Now, if a Notion page already has ⭐ Top pick, any incoming 👍/👎 vote is silently discarded (returns 200 to the email client, writes nothing).

**Changed**
- `feedback_server/api/feedback.py`: Added top-pick guard after the existing current-state read in the 👍/👎 path. Guard fires before the MPP conflict check, so both protections remain active. Updated module docstring.

**Deployed**
- Vercel production updated: `https://broadsheet-feedback.vercel.app`

---

## [0.3.3] — 2026-04-30

### Genuinely silent feedback — :checked interactive email technique (Session 9)

The window.close() approach from 0.3.2 still opened a browser tab momentarily. This replaces it entirely for Apple Mail and iOS Mail users.

**How it works**
Hidden `<input type="checkbox">` elements and `<label>` tags replace the `<a href>` feedback links. Clicking 👍 / 👎 toggles the checkbox; a CSS `:checked` rule activates a `background-image` URL on a 1px invisible element — that URL is the Vercel feedback endpoint, fetched silently by the email client. A second CSS rule marks the clicked label grey with strikethrough. No browser tab opens.

**Changed**
- `tools/email_sender.py` (`_build_html`): Pre-pass over stories builds per-story CSS rules (4 rules each: two background-image pings, two label visual-state rules). A single `<style>` block is emitted at the top of the email body. Each story div now opens with hidden `<input>` checkboxes, followed by an `<p class="bs-interactive">` block (shown in Apple Mail via CSS, hidden via inline `display:none` in Gmail) and a `<p class="bs-fallback">` block (hidden in Apple Mail via CSS, visible in Gmail which strips `<style>`).
- `feedback_server/api/feedback.py`: Added `from datetime import datetime` and `OPPOSITE` dict. Handler restructured: `v=top` path unchanged (immediate write + redirect); `v=good`/`v=less` path now reads current Notion page state before writing to detect MPP conflicts (opposing vote set < 30 s ago → clear property + log `[MPP-CONFLICT]`, don't write). Shared `notion_headers` dict eliminates header repetition.

**Fallback**
Gmail and other clients that strip `<style>` see the existing `<a href>` links and the window.close() self-closing browser response — same as 0.3.2.

**Client support**
- ✅ Apple Mail (macOS) — fully silent; label marks as clicked
- ✅ iOS Mail — fully silent; label marks as clicked
- ↩️ Gmail web / other — window.close() fallback as before

**Deployed**
- Vercel production updated: `https://broadsheet-feedback.vercel.app`

**Files changed**
- `tools/email_sender.py`
- `feedback_server/api/feedback.py`

---

## [0.3.2] — 2026-04-28

### Silent feedback acknowledgment (Session 8)

Clicking 👍/👎 in the email previously opened a browser tab with a "Got it!" page, interrupting skim-reading.

**Changed**
- `feedback_server/api/feedback.py`: Thank-you page replaced with `<script>window.close()</script>`. Tab closes itself automatically in most desktop browsers; blank tab in browsers that block `window.close()`. Removed `MSGS` constant (unused). `v=top` redirect path unchanged.
- `tools/email_sender.py`: 👍/👎 link style changed from `color:#888; text-decoration:none` to `color:#999`. Visited-link rendering now applies in Apple Mail, Thunderbird, and desktop Outlook (clients that use system browser history).

**Deployed**
- Vercel production updated: `https://broadsheet-feedback.vercel.app`

**Known limitation**
- Gmail and Outlook Web sandbox link rendering; visited-link state is not visible in those clients.

---

## [0.3.1] — 2026-04-28

### Output quality regression fix (Session 7)

**Root cause:** Session 5 introduced `max_stories: 8` (user intended 40). On April 28, both scoring LLMs failed simultaneously (Nemotron 502, OpenRouter free-tier rate-limited), causing equal scoring (all stories score=6) which exposed a feed-order bias in the category budget selection — 3 of 4 categories showed only a single source.

**Fixed**
- `config/digest.yaml`: `max_stories: 8` → `max_stories: 40`; updated `story_guidance` to reference 10-per-category budget with unused-slot rollover.
- `agents/newsroom_lead.py`: Added `published_iso` as secondary sort key in category budget loop — on equal-scoring days, most recent stories are selected rather than arbitrary feed-order stories.
- `agents/newsroom_lead.py`: Added source-diversity post-processing after budget selection. Within each category, if two or more selected stories share the same source, duplicate-source entries are replaced with the best alternative from a different source (above `diversity_min_quality`), preventing a single feed from dominating a category.
- `agents/newsroom_lead.py`: Default `max_stories` code fallback updated from 8 → 40.

**Not implemented (brief 03 — separate session)**
- Tertiary LLM to prevent equal scoring. Suggested: Claude Haiku 4.5 via Anthropic API.

**Files changed**
- `config/digest.yaml`
- `agents/newsroom_lead.py`
- `.claude/session.md`

---

## [0.3.0] — 2026-04-28

### In-email feedback links, scoring fallback, run report (Session 6)

**Added**
- `feedback_server/api/feedback.py` (new) — Vercel serverless function handling three flows: `v=good` (👍 thank-you page), `v=less` (👎 thank-you page), `v=top` (⭐ 302 redirect to article). Clicking a story title in the email is the ⭐ signal; 👍 / 👎 are explicit buttons below each summary.
- `feedback_server/vercel.json` (new) — Vercel build and routing config.
- `feedback_server/requirements.txt` (new) — `requests` only.
- `config/digest.yaml`: `feedback_endpoint` key under `delivery:`. Set to `https://broadsheet-feedback.vercel.app`.
- Per-agent run report in email: small grey line at top showing which model handled scoring and summaries, with ⚠ if scoring degraded to equal scoring.
- Scoring fallback: if Nemotron times out or errors during `select_stories()`, retries with OpenRouter/Llama before falling back to equal scoring. `main.py` builds the fallback LLM at startup and passes it through.

**Changed**
- `tools/notion_delivery.py`: `create_story_row()` now returns the Notion page ID (was `True`). Handles both the normal create path and the tags-retry fallback path. `publish_digest()` attaches `notion_page_id` to each story dict so the email can build per-story feedback links.
- `tools/email_sender.py`: new `feedback_endpoint` and `run_report` params. Story title links route through Vercel when endpoint is set (recording ⭐ and redirecting). 👍 / 👎 links appear below each summary. Notion paragraph no longer mentions feedback (links handle it). Run report rendered at top.
- `agents/newsroom_lead.py`: `select_stories()` accepts optional `fallback_llm`; returns `(stories, scoring_label)` tuple.
- `agents/editor.py`: `write_digest()` tracks retry attempts; returns `(stories, editor_label)` tuple.
- `main.py`: builds fallback LLM at startup; captures `(stories, label)` tuples; builds `run_report` dict; passes `feedback_endpoint` and `run_report` to `send_digest()`.

**Deployed**
- Vercel project `broadsheet-feedback` live at `https://broadsheet-feedback.vercel.app`
- `NOTION_API_KEY` set in Vercel environment variables
- First real run with feedback links sent 2026-04-28 12:38

**Design decisions recorded**
- ADR-009 (in `docs/DECISIONS.md`): updated to "implemented"; added ADR-009a (implicit ⭐ via click-through) and ADR-009b (`npx vercel` over `brew install vercel-cli` — Homebrew fails on macOS 13 due to LLVM source compilation in dependency chain).

**Files changed**
- `tools/notion_delivery.py`
- `tools/email_sender.py`
- `agents/newsroom_lead.py`
- `agents/editor.py`
- `main.py`
- `config/digest.yaml`
- `feedback_server/api/feedback.py` (new)
- `feedback_server/vercel.json` (new)
- `feedback_server/requirements.txt` (new)
- `briefings/email-feedback-links.md`
- `docs/DECISIONS.md`
- `.claude/session.md`

---

## [0.2.0] — 2026-04-17

### Feedback system rewrite (Session 5)

The feedback loop previously modified category and source weights directly, which could silently suppress an intentionally-chosen category from a single 👎. Rebuilt around three principles: category composition is structural (not learned from feedback); feedback attaches to topic tags (not categories or sources); sources are managed manually in YAML.

**Added**
- Topic-tag feedback model: editor agent assigns 2–3 tags per story (describing story TYPE, e.g. "model release", "AI regulation"); `tag_weights` dict in `calibration.yaml` replaces legacy `category_weights` and `source_weights`.
- Wilson score lower bound for sparse-data confidence adjustment (`_wilson_lower_bound()` in `agents/newsroom_lead.py`).
- Logarithmic weight accumulation (`math.log1p`) so the 20th rating doesn't count as much as the 1st.
- Category budget composition in `select_stories()`: each category gets `max_stories // 4` base slots; unused slots roll to stronger candidates from other categories.
- Diversity slot: if a category has zero stories above `min_confidence_score`, its best candidate above `diversity_min_quality` is added anyway.
- New Notion property: `Tags` (multi-select) — stores topic tags per story; read back in `read_feedback()` to build `tag_feedback`.
- `tests/test_feedback_system.py` — 8 unit tests covering tagging, budget allocation, rollover, Wilson score, log accumulation, tag-only feedback, diversity slot. All passing.
- New config keys in `config/digest.yaml`: `max_stories`, `diversity_slot`, `diversity_min_quality`.

**Changed**
- `agents/editor.py`: LLM now returns tags alongside summaries in the same JSON response.
- `agents/newsroom_lead.py`: `update_calibration_from_feedback()` and `select_stories()` rewritten. `_format_feedback_summary()` now renders tag weights, not category/source weights.
- `tools/notion_delivery.py`: `create_story_row()` writes `Tags` (gracefully falls back if property missing); `read_feedback()` returns `{"tag_feedback": {...}, "total_items": N}`.
- `config/calibration.yaml`: reset to new schema (`tag_weights: {}`); legacy keys removed on next run.

**Files changed**
- `agents/editor.py`
- `agents/newsroom_lead.py`
- `tools/notion_delivery.py`
- `config/calibration.yaml`
- `config/digest.yaml`
- `tests/__init__.py` (new)
- `tests/test_feedback_system.py` (new)
- `.claude/session.md`
- `docs/DECISIONS.md` — ADR-010 added

**Requires (user action)**
- Added a `Tags` multi-select property to the Notion database (done by user).

**Known limitations**
- Whether tag-affinity feedback produces measurably better digests is not yet verified in production; calibration will emerge organically after a few days of ratings.

---

## [0.1.3] — 2026-04-17

### Bug fix (Session 4)

**Fixed**
- `tools/rss_fetcher.py`: disabled feeds (`enabled: false` in `sources.yaml`) could leave stale failure counts in `source_state.json`, causing a persistent source alert even after the source was properly disabled. Fixed by calling `record_success()` when skipping a disabled feed — disabling a source now auto-clears its alert on the next run.

**Operational**
- Removed stale `The Batch (Andrew Ng)` entry from `logs/source_state.json` — failure counts were from a pre-fix test run and would never increment again, but were triggering a false alert.

**Planning / documentation**
- `briefings/email-feedback-links.md` — design brief for one-click in-email feedback links (Vercel endpoint → Notion). Not yet implemented.
- `briefings/github-deployment.md` — step-by-step deployment guide for pushing to GitHub Actions.

**Files changed**
- `tools/rss_fetcher.py`
- `logs/source_state.json`
- `briefings/email-feedback-links.md` (new)
- `briefings/github-deployment.md` (new)
- `.claude/session.md`

---

## [0.1.2] — 2026-04-16

### Bug fixes (Session 3 — first live run)

**Fixed**
- `tools/source_monitor.py`: race condition — four concurrent agents reading/writing `logs/source_state.json` simultaneously truncated the file to 0 bytes, crashing the builder agent with `JSONDecodeError`. Fixed with `threading.Lock()`, atomic writes (`.tmp` → rename), and resilient JSON loading.
- `tools/notion_delivery.py`: `databases.query` removed in notion-client 2.7.0 — replaced with direct `requests.post()` to Notion REST API. Also corrected filter property name from `"Date"` to match database schema (user renamed column accordingly).
- `agents/editor.py`: LLM occasionally returned malformed JSON, silently falling back to raw RSS descriptions. Added retry logic (up to 3 attempts before fallback).
- `agents/search_agents.py`: `gather_builder()` hardcoded `"enabled": True` in feed list comprehensions, ignoring `enabled: false` set in `sources.yaml`. Fixed to `f.get("enabled", True)` for both Reddit RSS and personal blog feed lists.
- `main.py`: added `exc_info=True` to gather-agent error logging for full tracebacks on future failures.

**Dead sources disabled in `config/sources.yaml`**
- Reuters Technology — DNS resolution failure (feed discontinued)
- DAIR.AI / Timnit Gebru — 404
- The Register AI — 404
- Brookings AI — persistent XML parse error
- The Batch (Andrew Ng) — 404

**Files changed**
- `tools/source_monitor.py`
- `tools/notion_delivery.py`
- `agents/editor.py`
- `agents/search_agents.py`
- `main.py`
- `config/sources.yaml`
- `logs/source_state.json` (deleted — rebuilt on next run)

**Known issues / pending**
- Newsroom story count varies significantly run-to-run (10–63) — scoring rubric in `config/agents.yaml` is minimal; adding a proper 1-10 scale and story count target will stabilise output
- GitHub Actions deployment not yet configured


## [0.1.1] — 2026-04-16

### Bug fixes (Session 2 — first run)

**Fixed**
- `requirements.txt`: added missing `litellm>=1.0.0` dependency (required by `crewai`'s `LLM` class at runtime)
- `requirements.txt`: pinned `notion-client>=2.2.0,<3.0.0` — v3.0.0 removed `DatabasesEndpoint.query` used in `tools/notion_delivery.py`
- `agents/newsroom_lead.py`: fixed `KeyError: 'weight'` in LLM fallback scoring path — `s["weight"]` corrected to `s.get("_weight", 1.0)`

**Files changed**
- `requirements.txt`
- `agents/newsroom_lead.py`


## [0.1.0] — 2026-04-11

### Initial build (Session 1)

**Added**
- Full project scaffold: `agents/`, `config/`, `tools/`, `logs/`, `.github/workflows/`
- 6 CrewAI agents: BigNews, LawsEthics, Builder, CriticalVoices, NewsroomLead, Editor
- 5 tool modules: `rss_fetcher.py`, `tavily_search.py`, `guardian_search.py`, `notion_delivery.py`, `email_sender.py`
- Source monitor: `tools/source_monitor.py` — tracks fetch failures, writes `logs/source_errors.log`, flags dead sources after 3 failures
- 5 config files (all user-editable except `calibration.yaml`)
- GitHub Actions workflow: 7am London time (dual cron for GMT/BST with concurrency guard)
- `.env.example` with all required keys and sign-up links
- `requirements.txt`
- `README.md` with full setup guide and Notion database schema
- `.claude/briefing.md`, `.claude/session.md`, `docs/DECISIONS.md`, `CLAUDE.md`

**Files created**
- `main.py`
- `agents/search_agents.py`
- `agents/newsroom_lead.py`
- `agents/editor.py`
- `tools/rss_fetcher.py`
- `tools/tavily_search.py`
- `tools/guardian_search.py`
- `tools/notion_delivery.py`
- `tools/email_sender.py`
- `tools/source_monitor.py`
- `config/agents.yaml`
- `config/sources.yaml`
- `config/my_sources.yaml`
- `config/digest.yaml`
- `config/calibration.yaml`
- `.env.example`
- `.gitignore`
- `requirements.txt`
- `.github/workflows/daily_digest.yml`
- `README.md`
- `CLAUDE.md`
- `.claude/briefing.md`
- `.claude/session.md`
- `docs/DECISIONS.md`

**Status**: Awaiting API keys from owner before first run
