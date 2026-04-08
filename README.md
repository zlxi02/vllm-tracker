# vLLM Issue Tracker

**[View the live dashboard](https://zlxi02.github.io/vllm-tracker/)**

Tracks and analyzes 14,500+ GitHub issues from the [vLLM project](https://github.com/vllm-project/vllm) to surface what the team should focus on next. Issues are classified by SIG group, model, hardware, and type using LLM, then clustered into prioritized recommendations.

## Dashboard

- **Issues** — Searchable table of all issues with filters for type, SIG, hardware, model, and failure mode
- **Newsfeed** — Daily newsletter-style summaries of notable issues and emerging trends
- **Roadmap** — Prioritized recommendations by SIG group, generated from issue clustering
- **Resources** — Release schedule, project links, community

## How it works

A Python pipeline ingests CSV exports of vLLM GitHub issues and runs them through a multi-stage LLM classification and triage process to surface the most pressing issues per SIG group.

```
CSV export → SQLite → classify → prelims → finals → enrich → rank → report
```

### Step 1: Load
Parses issue data into SQLite with regex-based enrichment (model tags, hardware tags, failure modes). Supports incremental updates — only new or changed issues are reprocessed.

### Step 2: Classify
LLM (Sonnet) assigns each issue a SIG group, model families, hardware platforms, and issue type. Batched at 50 issues per call. GitHub labels are used first; LLM fills in gaps.

### Step 3: Prelims
Selects 100 issues per SIG using 6-tier priority sampling that balances engagement, recency, and longevity:

| Tier | Engagement | Activity | Age |
|------|-----------|----------|-----|
| 1 | Top 10% comments | Within 30 days | Open 90+ days |
| 2 | Top 33% comments | Within 30 days | Open 45+ days |
| 3 | Top 10% comments | Within 30 days | Any |
| 4 | Top 33% comments | Within 30 days | Any |
| 5 | Any | Within 30 days | Any |
| 6 | Backfill by recency | | |

The 100 selected issues are split into 10 batches of 10. LLM (Opus with extended thinking) reads full issue bodies (10K chars) and comment threads (5K chars, most recent first) and picks the top 3 most pressing from each batch. Output: ~30 issues per SIG.

### Step 4: Finals
The ~30 top issues are re-read with full context in a single LLM call (Opus). Each issue is individually ranked — no clustering at this stage. The LLM produces a `main_fix` description, `why_pressing` editorial context, and metadata. Output: top 15 issues per SIG.

### Step 5: Enrich
Each of the ~15 issues per SIG gets its own LLM call (Sonnet) with the full issue body and all comments (token-budgeted, most recent first). Generates three fields:
- **Problem** — what's broken, specific error/trigger/model/hardware
- **Workaround** — what to tell users right now
- **Likely solve** — where to look in the code to fix it
- **Short title** — concise scannable title for the report

### Step 6: Rank
A single LLM call (Opus) ranks all 11 SIG groups against each other and produces a 4-8 bullet executive summary, informed by the current quarter's vLLM roadmap.

### Step 7: Build
Renders the roadmap report HTML (top 5 issues visible per SIG, 6-15 behind "show more") and exports dashboard data JSON. The report shows each issue with its short title, creation date, last activity date, comment count, and model/hardware tags. Clicking a row expands to show the problem, why it matters now, workaround, and likely solve.

### Running the pipeline

| Step | Command | Time | Cost | Model |
|------|---------|------|------|-------|
| Load | `python3 -m vllm_issue_tracker.cli load` | ~30s | Free | — |
| Classify | `python3 -m vllm_issue_tracker.cli dashboard-classify` | ~2-5 min | ~$1 | Sonnet |
| Build data | `cd dashboard && python3 build_data.py` | ~30s | Free | — |
| Prelims | `python3 -m vllm_issue_tracker.cli dashboard-prelims` | ~15 min | ~$30 | Opus |
| Finals | `python3 -m vllm_issue_tracker.cli dashboard-finals` | ~6 min | ~$20 | Opus |
| Enrich | `python3 -m vllm_issue_tracker.cli dashboard-enrich --force` | ~5 min | ~$6 | Sonnet |
| Rank | `python3 -m vllm_issue_tracker.cli dashboard-rank` | ~30s | ~$1 | Opus |
| Build report | `python3 -m vllm_issue_tracker.cli build-roadmap` | instant | Free | — |
| **Total** | | **~30 min** | **~$58** | |

```bash
# Full pipeline (all steps)
python3 -m vllm_issue_tracker.cli refresh

# Run a single SIG (prelims/finals support --sig filter)
python3 -m vllm_issue_tracker.cli dashboard-prelims --sig "Core Engine"
python3 -m vllm_issue_tracker.cli dashboard-finals --sig "Core Engine"
```

Each step saves intermediate results per SIG (`build/prelims/`, `build/finals/`), so individual SIGs can be re-run without affecting others. Load and classify are incremental by default — only new or changed issues are processed.
