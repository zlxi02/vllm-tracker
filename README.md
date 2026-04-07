# vLLM Issue Tracker

A Python pipeline + interactive dashboard for analyzing the vLLM GitHub issue pool and generating prioritized roadmap recommendations for the vLLM team.

## What it does

1. **Loads** issue data from Hex/Databricks CSV exports into SQLite
2. **Classifies** issues by type, SIG group, models, and hardware using LLM (Claude Sonnet)
3. **Summarizes** issue clusters per SIG with priority rankings
4. **Generates** a roadmap-style HTML report with executive summary
5. **Dashboard** with searchable issue table, daily newsletter, roadmap tab, and resource links

## Quick start

```bash
# Install dependencies
pip install -e .

# Set up API key
cp .env.example .env
# Edit .env with your Anthropic API key

# Drop fresh CSV from Hex into data/
cp ~/Downloads/github_issues.csv data/github_issues.csv

# Run the full pipeline (incremental by default)
python3 -m vllm_issue_tracker.cli refresh

# Rebuild dashboard issue table
python3 dashboard/build_data.py

# Serve the dashboard
python3 dashboard/serve.py
# Open http://localhost:8000
```

## Pipeline commands

```bash
# Full pipeline (load → classify → summarize → rank → build-roadmap)
python3 -m vllm_issue_tracker.cli refresh          # incremental (default)
python3 -m vllm_issue_tracker.cli refresh --full    # full rebuild (prompts before wiping)

# Individual steps
python3 -m vllm_issue_tracker.cli load              # CSV → SQLite (incremental by default)
python3 -m vllm_issue_tracker.cli load --full        # full reload
python3 -m vllm_issue_tracker.cli dashboard-classify # LLM classify all issues (~$9 Sonnet)
python3 -m vllm_issue_tracker.cli dashboard-summarize # per-SIG cluster summaries
python3 -m vllm_issue_tracker.cli dashboard-rank     # priority ranking + executive summary
python3 -m vllm_issue_tracker.cli build-roadmap      # render HTML report

# Utilities
python3 -m vllm_issue_tracker.cli quality-check      # data quality metrics
python3 dashboard/build_data.py                      # rebuild dashboard JSON from SQLite
```

## Data sources

- `data/github_issues.csv` — required input (full dump from Hex/Databricks)
- `data/users.csv` and `data/issue_comments.csv` — optional enrichments
- `data/preview/*.csv` — small sample files for tests

## Architecture

```
data/github_issues.csv
    ↓
[ingest.py] Parse + regex classify → SQLite
    ↓
[llm_classify.py] LLM classification (type, SIG, models, hardware)
    ↓
[llm_classify.py] Per-SIG summarization → dashboard_summary.json
    ↓
[llm_classify.py] Priority ranking + executive summary
    ↓
[report.py] Render roadmap HTML → outputs/roadmap.html + dashboard/report.html

dashboard/build_data.py → dashboard/data/dashboard_data.json (for issue table)
```

## Dashboard tabs

- **Dashboard** — Searchable issue table with filters (type, SIG, hardware, model, failure mode)
- **Newsfeed** — Daily newsletter-style summaries of GitHub issues
- **Roadmap** — LLM-generated prioritized roadmap report by SIG
- **Resources** — Release schedule, project links, community links

## Cost

- Full classify (14k issues): ~$9 on Sonnet
- Incremental classify (new issues only): ~$0.02 per batch of 50
- Summarize + rank: ~$3.50
- Full pipeline: ~$12.50
