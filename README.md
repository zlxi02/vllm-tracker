# vLLM Issue Tracker

**[View the live tracker](https://zlxi02.github.io/vllm-tracker/)**

Tracks and analyzes 14,500+ GitHub issues from the [vLLM project](https://github.com/vllm-project/vllm) to surface what the team should focus on next. Issues are classified by SIG group, model, hardware, and type using LLM, then triaged into prioritized recommendations with a daily newsfeed.

## Tracker

- **Dashboard** — Searchable table of all issues with filters for type, SIG, hardware, model, and age
- **Newsfeed** — Daily LLM-generated digests of notable issues and emerging trends
- **Triage** — Prioritized recommendations by SIG group with per-issue problem/workaround/fix analysis
- **Resources** — Past releases, quarterly roadmaps, documentation, community links

## How it works

### Dashboard

A Python pipeline ingests CSV exports of vLLM GitHub issues into SQLite. LLM (Sonnet) classifies each issue by SIG group, model families, hardware platforms, and issue type — batched at 50 issues per call. GitHub labels are used first; LLM fills in gaps. The dashboard reads from SQLite and renders a filterable, sortable table.

```
CSV export → SQLite → LLM classify → dashboard JSON
```

### Triage

A multi-stage LLM pipeline surfaces the most pressing open issues per SIG group.

```
classify → prelims → finals → enrich → rank → report
```

**Prelims** — Selects 100 issues per SIG using 6-tier priority sampling that balances engagement, recency, and longevity:

| Tier | Engagement | Activity | Age |
|------|-----------|----------|-----|
| 1 | Top 10% comments | Within 30 days | Open 90+ days |
| 2 | Top 33% comments | Within 30 days | Open 45+ days |
| 3 | Top 10% comments | Within 30 days | Any |
| 4 | Top 33% comments | Within 30 days | Any |
| 5 | Any | Within 30 days | Any |
| 6 | Backfill by recency | | |

The 100 selected issues are split into 10 batches of 10. LLM (Opus with extended thinking) reads full issue bodies (10K chars) and comment threads (5K chars, most recent first) and picks the top 3 most pressing from each batch. Output: ~30 issues per SIG.

**Finals** — The ~30 top issues are re-read with full context in a single LLM call (Opus). Each issue is individually ranked — no clustering. The LLM produces a `main_fix` description, `why_pressing` editorial context, and metadata. Output: top 15 issues per SIG.

**Enrich** — Each of the ~15 issues per SIG gets its own LLM call (Sonnet) with the full issue body and all comments. Generates:
- **Problem** — what's broken, specific error/trigger/model/hardware
- **Workaround** — what to tell users right now
- **Likely solve** — where to look in the code to fix it
- **Short title** — concise scannable title for the report

**Rank** — A single LLM call (Opus) ranks all 11 SIG groups against each other and produces a 4-8 bullet executive summary, informed by the current quarter's vLLM roadmap.

**Build** — Renders the triage report HTML (top 5 issues visible per SIG, 6-15 behind "show more"). Each issue shows its short title, creation date, last activity, comment count, and model/hardware tags. Expanding a row reveals the problem, why it matters, workaround, and likely solve.

### Newsfeed

A daily LLM-generated digest of issue activity.

**Input** — The LLM receives:
- All issues created or updated on the target date (up to 60), each with title, body excerpt (300 chars), type, state, comment count, model tags, and hardware tags — sorted by engagement
- The 3 most recent vLLM release notes (full text) for context on what shipped recently
- The target date

**Output** — A structured JSON digest with:
- A punchy headline and "Today in vLLM:" opening
- 3-5 themed sections grouping related issues (e.g. "Gemma 4 Bugs Continue", "MoE Infrastructure Growing Pains")
- Per-issue entries with emoji, linked title, and 1-2 sentence description
- An editorial "Bottom Line" takeaway
- Stats (issue count, comments, closed)

Each day is saved as a separate JSON file in `build/newsfeed/`. The HTML is rebuilt from all available day files on each run, so the archive grows over time.

```bash
# Generate today's digest
python3 -m vllm_issue_tracker.cli generate-newsfeed

# Backfill a specific date range
python3 -m vllm_issue_tracker.cli generate-newsfeed --date 2026-04-05 --days 4
```

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
| Newsfeed | `python3 -m vllm_issue_tracker.cli generate-newsfeed` | ~30s | ~$0.50 | Sonnet |
| **Total** | | **~30 min** | **~$59** | |

```bash
# Full triage pipeline (all steps except newsfeed)
python3 -m vllm_issue_tracker.cli refresh

# Run a single SIG (prelims/finals support --sig filter)
python3 -m vllm_issue_tracker.cli dashboard-prelims --sig "Core Engine"
python3 -m vllm_issue_tracker.cli dashboard-finals --sig "Core Engine"
```

Each step saves intermediate results per SIG (`build/prelims/`, `build/finals/`), so individual SIGs can be re-run without affecting others. Load and classify are incremental by default — only new or changed issues are processed.
