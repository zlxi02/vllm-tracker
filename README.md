# vLLM Issue Tracker

**[View the live dashboard](https://zlxi02.github.io/vllm-tracker/)**

Analyzes the vLLM GitHub issue pool and generates prioritized roadmap recommendations. Classifies ~14,500 issues by SIG group, model, hardware, and type, then surfaces what the vLLM team should focus on next.

## Dashboard

The dashboard has four tabs:

- **Dashboard** — Searchable issue table with filters (type, SIG, hardware, model, failure mode)
- **Newsfeed** — Daily newsletter-style summaries of notable GitHub issues
- **Roadmap** — Prioritized recommendations by SIG, generated from issue clustering
- **Resources** — Release schedule, project links, community

## Refreshing data

Requires Python 3.9+ and an Anthropic API key.

```bash
# One-time setup
pip install -e .
cp .env.example .env   # add your ANTHROPIC_API_KEY

# Drop fresh CSV export from Hex into data/
cp ~/Downloads/github_issues.csv data/github_issues.csv

# Run the pipeline (incremental by default — only processes new/changed issues)
python3 -m vllm_issue_tracker.cli refresh

# Rebuild dashboard data
python3 dashboard/build_data.py

# Deploy
git add dashboard/data/ dashboard/report.html
git commit -m "Refresh data"
git push   # auto-deploys to GitHub Pages
```

A full pipeline run costs ~$12 on Claude Sonnet. Incremental runs (daily refresh) cost ~$1-2.

## Pipeline

```
data/github_issues.csv
    → [load] CSV → SQLite with regex enrichment
    → [dashboard-classify] LLM classifies type, SIG, models, hardware
    → [dashboard-summarize] LLM clusters issues per SIG with priorities
    → [dashboard-rank] Executive summary + SIG priority ranking
    → [build-roadmap] Render roadmap HTML
    → [build_data.py] Export SQLite → dashboard JSON
```

### Commands

```bash
python3 -m vllm_issue_tracker.cli refresh            # full pipeline (incremental)
python3 -m vllm_issue_tracker.cli refresh --full      # full rebuild (prompts before wiping)
python3 -m vllm_issue_tracker.cli load                # CSV → SQLite only
python3 -m vllm_issue_tracker.cli dashboard-classify  # LLM classify (~$9)
python3 -m vllm_issue_tracker.cli dashboard-summarize # per-SIG summaries (~$3)
python3 -m vllm_issue_tracker.cli dashboard-rank      # priority ranking (~$0.50)
python3 -m vllm_issue_tracker.cli build-roadmap       # render HTML
python3 -m vllm_issue_tracker.cli quality-check       # data quality metrics
python3 dashboard/build_data.py                       # rebuild dashboard JSON
```
