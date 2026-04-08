# AI Assistant Context

This document provides working context for AI assistants. Reflects project state as of April 2026.

## 1. PREFERENCES

### Collaboration style
- Collaborative, product-minded
- Concrete recommendations over abstract brainstorming
- Practical next steps over broad discussion
- Iterates interactively, redirects quickly if plan gets too complex
- Decisions documented explicitly

## 2. PROJECT STATE

### What this project is
A **vLLM issue tracker** that ingests GitHub issue data, classifies it with LLM, and produces:
1. An **interactive dashboard** (filterable table with SIG, model, hardware, type columns)
2. A **roadmap report** (per-SIG prioritized cluster summaries)
3. A **daily newsfeed** (newsletter-style issue summaries)

Live at: https://zlxi02.github.io/vllm-tracker/

### Pipeline
```
load → dashboard-classify → dashboard-summarize → dashboard-rank → build-roadmap
                                                                 → build_data.py
```

- **load**: CSV → SQLite. Regex tags + label-based issue_type. Incremental by default (upserts new/changed issues, preserves LLM classifications). Full reload prompts before wiping classified data.
- **dashboard-classify**: LLM classifies every issue with `sig_group`, `model_tags`, `hardware_tags`, `issue_type` (fallback). Skips already-classified issues.
- **dashboard-summarize**: LLM clusters issues per SIG, generates summaries. Fetches current roadmap from GitHub and recent release notes for context.
- **dashboard-rank**: LLM ranks SIGs by priority, generates executive summary.
- **build-roadmap**: Renders HTML from summary JSON, copies to `dashboard/report.html`.
- **build_data.py**: Reads from SQLite (prefers LLM fields), outputs `dashboard/data/dashboard_data.json`.

### Refresh command
`python3 -m vllm_issue_tracker.cli refresh` chains the full pipeline. Incremental by default. `--full` forces full rebuild with confirmation prompt.

### Classification system
- **Issue types** (MECE): Bug, Feature Request, Usage/Question, Model Request, RFC/Discussion, Other. GitHub labels first, LLM fallback.
- **SIG groups**: 11 fixed workstream themes from `prompts.py:WORKSTREAM_THEMES` (Core Engine, Large Scale Serving, Performance, Torch Compile, Frontend/API, Multimodality, Quantization/Model Acceleration, Model Support, Installation/Build/CI, RL/Post-training, Docs/UX).
- **Model tags**: JSON arrays, LLM-extracted. 21+ model families. `["General"]` for agnostic issues.
- **Hardware tags**: JSON arrays, LLM-extracted. `["General"]` for agnostic issues.
- **Failure modes**: Regex-based only (legacy), used in dashboard filtering.

### Dashboard tabs
- **Dashboard** — Filterable issue table (Created, PR#, Title, State, Type, HW, Model, SIG, Age, Cmt #, Author). Sort modes: New/Hot/Top/Date Range.
- **Newsfeed** — Daily newsletter with prev/next navigation, collapsible sidebar (defaults collapsed).
- **Roadmap** — "Release Roadmap" showing recent/upcoming vLLM releases.
- **Resources** — Quarterly Roadmaps (10 roadmaps, H2 2023–Q1 2026), docs, community links.

### Summarization pipeline (prelims → finals → merge)
Three-step pipeline, each independently executable via CLI with `--sig` filter:
- **Prelims** (`dashboard-prelims`): 100 issues → 10 batches of 10, pick top 3 each → 30 issues. Full body (10K) + comments (5K).
- **Finals** (`dashboard-finals`): 30 issues in one prompt, cluster + rank → top 15 clusters.
- **Merge** (`dashboard-merge`): Deduplicate clusters with same root cause.
- **Model**: Opus with extended thinking (10K budget) for all three steps. Sonnet for enrich.
- **Issue selection**: 100 per SIG, filtered to Bug/FR/Usage/Other. 6-tier sampling: T1 (top 10% comments + active 30d + open 90d) → T2 (top 33% + active 30d + open 45d) → T3-T4 (engagement + active) → T5 (any activity 30d) → T6 (backfill).
- **Comment bodies**: Loaded from `data/issue_comments_body.csv` (1.4M rows).

### Key data
- ~14,540 issues from `data/github_issues.csv` (Hex/Databricks export)
- SQLite at `build/vllm_issue_snapshot.sqlite3`
- Dashboard JSON at `dashboard/data/dashboard_data.json`
- Summary at `build/dashboard_summary.json`
- Roadmap at `outputs/roadmap.html` + `dashboard/report.html`

### Deployment
- GitHub Pages from `dashboard/` directory via Actions workflow
- Auto-deploys on push to `dashboard/**` on main
- Data refresh: run pipeline locally, commit dashboard data, push

### Environment
- Python >=3.9, dependencies: anthropic, openai, python-dotenv, tqdm
- LLM config via `.env`: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `LLM_THINKING_BUDGET`
- Default model: claude-opus-4-20250514 (enrich uses Sonnet automatically)
- CI: GitHub Actions (`ci.yml` for tests, `deploy-pages.yml` for GitHub Pages)

## 3. KEY DECISIONS

- **SIG groups are the primary organization** for both dashboard filtering and roadmap report
- **LLM over regex** for dashboard classification
- **Multi-value model/hardware** as JSON arrays
- **GitHub labels first, LLM fallback** for issue type
- **Incremental by default** — load and classify only process new/changed issues
- **Full wipe requires confirmation** — prevents accidental loss of ~$9 in LLM classifications
- **Legacy Path 1 removed** — taxonomy discovery/classify/validate pipeline was cleaned up. Only the dashboard pipeline remains.
- **Dashboard data committed to repo** for static hosting on GitHub Pages

## 4. COST

- Full classify (~14.5k issues): ~$9 (Sonnet)
- Summarize (11 SIGs): ~$48 (Opus + thinking)
- Prioritize + rank: ~$19 (Opus + thinking)
- Enrich: ~$3 (Sonnet)
- Full pipeline: ~$79
- Incremental daily refresh: ~$1-2
