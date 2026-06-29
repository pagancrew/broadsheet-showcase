# Broadsheet

A daily AI news digest, fully automated and running on free-tier infrastructure end to end.
A six-agent CrewAI pipeline gathers, deduplicates, ranks, and writes up AI news across four
categories every night, then delivers it via email and tracks content on a Notion database. It
sharpens its selection over time from what readers actually click.

**Problem it solves:** AI news moves fast and most newsletters don't cover exactly the content I 
want to read about, or duplicate the same headline news. Broadsheet enforces a strict
quality bar instead. Stories are recency-checked, deduplicated across sources, and ranked by
an LLM rather than by a fixed point score, and the pipeline remembers what it rejected so the
same stale link does not cost tokens and attention twice.

> **Built to run on free tiers.** Every service in the stack (Gemini, Cerebras, Groq, Tavily,
> Notion, Brevo, Vercel) has an always-free plan, and the project needs no paid infrastructure.
> Known issue: The current query volume overruns Tavily's free monthly allocation as it is also
> used to keep track of named Substack authors (no RSS feeds). See [Known limitations](#known-limitations).

---

## Architecture

**Six agents, one pipeline (CrewAI):**

| Agent | Role |
|---|---|
| `BigNewsAgent` | Headline AI news, via RSS feeds and Tavily web search |
| `LawsEthicsAgent` | Regulation, policy, and ethics. RSS plus Tavily news search |
| `BuilderAgent` | Creative AI build ideas from curated technical blogs, GitHub Trending, and Tavily |
| `CriticalVoicesAgent` | Critical and anti-hype commentary, from curated RSS and Substack newsletters fetched via Tavily |
| `NewsroomLeadAgent` | Selects, deduplicates, and scores stories, and reads click-through history |
| `EditorAgent` | Writes a two-sentence factual summary per story against a strict tone of voice brief and runs a final quality pass |

**Retrieval.** Where a source publishes a usable RSS feed, the pipeline reads it directly.
Everything else goes through Tavily: the per-category topic queries, the no-RSS sites (fetched
by domain), and the Substack newsletters. Substack returns HTTP 403 to cloud IPs such as
GitHub Actions runners, so its posts cannot be read by RSS from the deployment environment and
are pulled through Tavily instead. 

**Model fallback chain:** Gemini, then Cerebras, then Groq, then equal scoring. All three
providers are always-free tiers. The pipeline auto-switches on failure or rate limit and logs
every switch, so a single provider outage never blocks delivery. In practice, model fallback
is rare (the scheduling was selected to minimise chances of that.)

**Selection pipeline** (`agents/newsroom_lead.py: select_stories()`) is an ordered funnel
designed to keep quality high without ever repeating a story:

1. **Ingest filters.** Hard-block configured domains and topics (AI-related job postings, social
   platforms, organisational announcements...) at fetch time.
2. **Evergreen and stale removal.** Drop anything older than 14 days, and anything matching a
   URL the pipeline has already rejected in the last 30 days (persisted across runs via a
   Notion "Candidates" log), so the same recycled SEO piece cannot re-enter the funnel every night.
3. **Heuristic first pass.** Score by recency, tag affinity (from calibration), and source
   bonus, with a per-source cap so one prolific feed cannot fill a category alone. A small set
   of trusted named authors are prioritised if their post is fresh, bypassing the
   budget and confidence gates (capped at one post per byline). This ensures individually selected
   Substack posters get priority.
4. **LLM rerank.** The top candidates per category are pairwise-ranked by the LLM. Heuristic
   scoring builds the shortlist; the LLM decides relative quality within it, because LLMs are
   far more reliable at relative ranking than at absolute 1 to 10 scoring.
5. **Quality floor.** Anything below the confidence threshold is dropped.
6. **Semantic dedup before budgeting.** Near-duplicate coverage of the same event is collapsed
   before category slots are allocated, so duplicates cannot silently consume a category's
   budget and starve a better story.
7. **Budget, rollover, and diversity.** Each category fills to its cap from its best-scoring
   stories, unused slots roll over to stronger categories, and one slot is reserved for any
   category that would otherwise be unrepresented that day.
8. **Final dedup and editorial QA.** One more dedup safety pass, then the Editor agent drops
   anything that still does not meet the bar before writing summaries.

**Feedback loop.** Click-throughs are logged to a Notion "Votes" database and aggregated each run
into per-tag preference weights, which steer the next day's heuristic scoring.

**Delivery.** Per-subscriber email via Brevo SMTP, each with individually tracked feedback links. 
A serverless function on Vercel handles the click-through redirect and a Notion database write to 
keep track of 30 days' worth of stories to aid deduplication.

---

## Known limitations

**Tavily cost is not yet sustainable.** Tavily charges one credit per search, and the current
configuration fires roughly 50 searches per run (topic queries, no-RSS sites, and Substack
sources combined). The free tier is 1,000 credits per month, so a sustainable steady state is
about 33 searches per run. In practice the pipeline has been running hot: on a recent billing
cycle it had used about 73 percent of the monthly allocation by day 17, on track to exceed 100
percent before month end. A fix is designed (a self-pacing budget guard that caps discretionary
searches at the day's fair share of remaining credits, plus staggering low-priority sources to
alternate days) but it is not implemented yet. Until then, Tavily is the one part of the stack
that can run past its free allocation. Note that `max_results` and search depth do not affect
cost; only the number of searches does, so the fix targets search count directly.

---

## Setup

### Prerequisites
- Python 3.11+
- A GitHub account (for scheduling)
- A Notion account (free)
- A Brevo account (free, for transactional email)
- A Tavily account (free, for search)

### Step 1 - Get API keys

All providers below are free with no credit card required.

| Key | Sign up | Free tier |
|-----|---------|-----------|
| Gemini (primary LLM) | [aistudio.google.com](https://aistudio.google.com), Get API Key | 250 requests/day |
| Cerebras (fallback LLM) | [cloud.cerebras.ai](https://cloud.cerebras.ai), API Keys | ~1M tokens/day |
| Groq (tertiary LLM) | [console.groq.com](https://console.groq.com), API Keys | 14,400 requests/day |
| Tavily (search) | [app.tavily.com](https://app.tavily.com) | 1,000 credits/month (see Known limitations) |
| ProductHunt (optional) | [producthunt.com/v2/oauth/applications](https://www.producthunt.com/v2/oauth/applications) | Free read-only |
| Notion integration | [notion.so/my-integrations](https://www.notion.so/my-integrations) | Free |
| Brevo (email delivery) | [brevo.com](https://www.brevo.com) | 300 emails/day, no trial limit |

There is no Guardian API key. Law and ethics coverage of Guardian content is fetched through
Tavily news search instead (see Architecture above for why).

### Step 2 - Create the Notion database

1. Open Notion and create a new full-page database (Table view).
2. Name it "Broadsheet".
3. Add these properties (exact names and types required):

| Property name | Type | Notes |
|---------------|------|-------|
| **Title** | Title | Default, already exists |
| **URL** | URL | |
| **Category** | Select | Options: Big News, Built & Released, Law & Ethics, Commentary, Admin |
| **Source** | Text | |
| **Summary** | Text | |
| **Date** | Date | |
| **Confidence** | Number | |
| **Tags** | Multi-select | Topic tags assigned by the Editor agent |

(There is no `Feedback` property. Feedback is click-through only and lives entirely in the
separate Votes database below.)

4. Share the database with your integration: click **Share** (top right), then **Invite**,
   search for "Broadsheet", and confirm.
5. Copy the database ID from the URL. The URL looks like
   `https://www.notion.so/yourworkspace/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX?v=...`, and the
   database ID is the 32-character string before the `?`.
6. Create a second database, "Broadsheet Votes", with properties: `Subscriber` (title),
   `Story Page ID` (text), `Story Title` (text), `Vote` (select), `Story Tags` (multi-select),
   `Voted At` (date). One row is written per click, per subscriber.

### Step 3 - Configure locally

```bash
git clone https://github.com/YOUR_USERNAME/broadsheet-showcase.git
cd broadsheet-showcase
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and replace every `PLACEHOLDER_...` value with your actual keys.

### Step 4 - Test locally

```bash
# Dry run: prints digest to terminal, no delivery
python main.py --dry-run

# Full run with delivery
python main.py

# Notion only (skip email)
python main.py --no-email

# Email only (skip Notion)
python main.py --no-notion
```

### Step 5 - Deploy to GitHub Actions

1. Push this project to your own GitHub repository.
   > If you add real subscriber email addresses to `config/digest.yaml`, keep the repo private.
   > That file is committed to git, so anything in it is visible to anyone who can read the
   > repo. To go public, move the recipients out into a gitignored file first.
2. Add secrets in GitHub: **Settings, Secrets and variables, Actions, New repository secret**.

   | Secret name | Notes |
   |-------------|-------|
   | `GEMINI_API_KEY` | Primary LLM |
   | `CEREBRAS_API_KEY` | Fallback LLM |
   | `GROQ_API_KEY` | Tertiary LLM |
   | `TAVILY_API_KEY` | Web search across all four categories |
   | `PRODUCTHUNT_API_TOKEN` | Optional, skip if not set |
   | `NOTION_API_KEY` | Notion delivery |
   | `NOTION_DATABASE_ID` | Stories database |
   | `NOTION_VOTES_DATABASE_ID` | Per-subscriber votes database |
   | `NOTION_CANDIDATES_DATABASE_ID` | Optional. Powers the rejected-URL memory that stops stale stories re-entering the funnel; logging is silently skipped if unset |
   | `NOTION_RUN_LOG_DATABASE_ID` | Optional. Per-run diagnostics; silently skipped if unset |
   | `SENDER_EMAIL` | Verified Brevo "From" address |
   | `BREVO_SMTP_LOGIN` | Brevo SMTP login |
   | `BREVO_SMTP_KEY` | Brevo SMTP key |

3. Enable Actions: open the **Actions** tab and enable workflows.
4. Test manually: **Actions, Daily Broadsheet Digest, Run workflow, dry_run: true**.

The digest then runs automatically at **1am UTC** every day. It is scheduled early to absorb
GitHub Actions' best-effort cron delay and to stay below the morning congestion window on the
free-tier LLM pools. See the workflow file for the full reasoning.

---

## Customising the digest

All customisation is done by editing text files. No coding needed.

### Change agent behaviour
Edit `config/agents.yaml`. Each agent has a `goal`, `backstory`, and `instructions` in plain English.

### Add or remove sources
Edit `config/sources.yaml`. Add, remove, or set `enabled: false` on any entry.

### Change story count or quality threshold
Edit `config/digest.yaml`:
- `story_guidance`: plain-English instruction to the agents
- `min_confidence_score`: number from 1 to 10 (lower means more stories, higher means stricter)
- `category_max` and `rollover_max`: per-category hard caps and shared rollover slots

### Add or change recipients
Edit `config/digest.yaml`, under `delivery.email.recipients`:
```yaml
recipients:
  - email: you@example.com
    subscriber_id: "You"
```
`subscriber_id` must be unique and is used in feedback links and the Votes database. If you add
real email addresses here, keep the repository private (see the deploy step above).

### Change delivery settings
Edit `config/digest.yaml`:
- `delivery.email.enabled` and `delivery.notion.enabled`: true or false
- `delivery.email.subject`: subject line template

### Change models
Edit `config/digest.yaml`, under `models.primary`, `models.fallback`, and `models.tertiary`.
Check the provider's console for current model IDs, since free-tier model availability changes
over time.

---

## Giving feedback to calibrate the digest

Click a story's title in the email to read it. That is the entire feedback mechanism. The
click routes through a small Vercel function, logs a click-through against your `subscriber_id`
in the Votes database, then redirects you to the article. There is no explicit rating step.

The Newsroom Lead agent reads recent click-through history at the start of each run and turns
it into per-tag preference weights. Topics you click get more weight, decaying over time so a
single old burst of interest does not permanently bias selection. Weights are stored in
`config/calibration.yaml`, generated automatically each run. Do not edit it by hand.

---

## Source alerts

If a source fails to fetch, a "Source Alerts" section appears at the bottom of the digest, in
both Notion and email. After 3 consecutive failures it is flagged "Likely dead". A source that
returns successfully but yields zero stories twice in a row is flagged too, so silent
zero-result failures do not go unnoticed.

To fix: open `config/sources.yaml` and update the URL, or disable or remove the entry.

Full failure log: `logs/source_errors.log`.

---

## Troubleshooting

**"No stories gathered"**: check that at least one API key is set and working. Run
`python main.py --dry-run` to see error output.

**"GEMINI_API_KEY not set, using fallback model"**: your Gemini key is missing. The digest
still runs, falling through to Cerebras and then Groq.

**Email not arriving**: check spam. Verify `SENDER_EMAIL`, `BREVO_SMTP_LOGIN`, and
`BREVO_SMTP_KEY` are set correctly in `.env` and GitHub Secrets.

**Notion rows not appearing**: verify the database is shared with the integration. Check that
`NOTION_DATABASE_ID` has no spaces or extra characters.

**GitHub Actions not running**: workflows must be enabled in the Actions tab. Check the
workflow logs for errors.

---

## Project structure

```
broadsheet-showcase/
├── agents/
│   ├── search_agents.py    # 4 retrieval agents
│   ├── newsroom_lead.py    # selection pipeline + calibration
│   └── editor.py           # summary writer + final QA pass
├── config/
│   ├── agents.yaml         # EDIT: agent behaviour
│   ├── sources.yaml        # EDIT: curated sources
│   ├── digest.yaml         # EDIT: delivery + story settings
│   └── calibration.yaml    # AUTO: feedback weights (do not edit)
├── tools/
│   ├── rss_fetcher.py
│   ├── tavily_search.py
│   ├── inbox_fetcher.py    # Substack newsletters fetched via Tavily
│   ├── github_trending.py
│   ├── notion_delivery.py
│   ├── email_sender.py
│   ├── style_check.py      # deterministic regex pass on Editor output
│   ├── token_tracker.py
│   └── source_monitor.py
├── feedback_server/         # Vercel serverless function: click-through redirect + vote logging
├── tests/
├── scripts/
├── logs/                    # AUTO: source failures, dedup, style-check, ingest filters
├── .github/workflows/
│   └── daily_digest.yml    # GitHub Actions schedule
├── main.py                  # entry point
├── requirements.txt
├── .env.example             # copy to .env and fill in
└── docs/
    └── DECISIONS.md         # full design-decision log (51 entries): the record of why the system works this way
```

---

## Design history

`docs/DECISIONS.md` is a complete, dated log of every architectural decision in the project,
including dead ends and superseded approaches. Examples: the original thumbs up/down voting
mechanism, replaced by click-through-only feedback; the original LLM provider chain, replaced
after a free-tier credit grant ran out; and Gmail inbox polling for Substack, replaced by
Tavily after the Gmail account was suspended for bot-like login patterns. If you want to see
why the pipeline looks the way it does rather than just what it does, start there.
