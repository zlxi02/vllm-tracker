# Product Spec: vLLM Issue Tracker

## Summary
A pipeline that ingests vLLM GitHub issue data, classifies issues using LLM, and produces:
1. **Interactive dashboard** — filterable table of all issues with SIG group, model, hardware, type columns
2. **Roadmap report** — per-SIG prioritized cluster summaries, embedded in the dashboard
3. **Daily newsfeed** — newsletter-style issue summaries

Live at: https://zlxi02.github.io/vllm-tracker/

## Architecture

### Pipeline
```
data/github_issues.csv
  → [load] CSV → SQLite (regex tags + label-based issue_type)
  → [dashboard-classify] LLM classifies: sig_group, model_tags, hardware_tags, issue_type
  → [dashboard-summarize] LLM clusters issues per SIG, generates summaries
  → [dashboard-rank] LLM ranks SIGs, generates executive summary
  → [build-roadmap] Renders roadmap HTML
  → [build_data.py] Exports SQLite → dashboard JSON
```

### CLI commands
```bash
python3 -m vllm_issue_tracker.cli refresh            # full pipeline (incremental)
python3 -m vllm_issue_tracker.cli refresh --full      # full rebuild (prompts before wiping)
python3 -m vllm_issue_tracker.cli load                # CSV → SQLite (incremental)
python3 -m vllm_issue_tracker.cli load --full         # full reload
python3 -m vllm_issue_tracker.cli dashboard-classify  # LLM classify all issues
python3 -m vllm_issue_tracker.cli dashboard-summarize # per-SIG summaries
python3 -m vllm_issue_tracker.cli dashboard-rank      # priority ranking
python3 -m vllm_issue_tracker.cli build-roadmap       # render HTML
python3 -m vllm_issue_tracker.cli quality-check       # data quality metrics
python3 dashboard/build_data.py                       # rebuild dashboard JSON
```

### Key files
| File | Role |
|------|------|
| `src/vllm_issue_tracker/cli.py` | CLI entry point |
| `src/vllm_issue_tracker/config.py` | Settings, paths, LLM config |
| `src/vllm_issue_tracker/ingest.py` | CSV → SQLite, incremental upsert, label parsing |
| `src/vllm_issue_tracker/classify.py` | Regex model/hardware/failure-mode extraction |
| `src/vllm_issue_tracker/prompts.py` | LLM prompt templates + workstream themes |
| `src/vllm_issue_tracker/llm_classify.py` | LLM orchestration (classify, summarize, rank) |
| `src/vllm_issue_tracker/report.py` | Roadmap report HTML rendering |
| `dashboard/build_data.py` | SQLite → dashboard JSON |
| `dashboard/index.html` | Interactive dashboard UI |
| `dashboard/serve.py` | Dev server (respects PORT env var) |

## Classification System

### Issue Types (MECE)
Assigned via GitHub labels first, LLM fallback for unlabeled issues:

| GitHub Label | Issue Type |
|---|---|
| `bug` | Bug |
| `feature request` | Feature Request |
| `documentation`, `usage` | Usage/Question |
| `new-model` | Model Request |
| `rfc` | RFC/Discussion |
| _(none matched)_ | Other (LLM assigns) |

### SIG Groups (LLM-classified)
11 fixed workstream themes from `prompts.py:WORKSTREAM_THEMES`:
1. Core Engine (#sig-core)
2. Large Scale Serving (#sig-large-scale-serving)
3. Performance (#sig-model-performance)
4. Torch Compile (#sig-torch-compile)
5. Frontend / API (#sig-frontend)
6. Multimodality (#sig-multi-modality)
7. Quantization / Model Acceleration (#sig-quantization)
8. Model Support (model-support-program)
9. Installation / Build / CI (#sig-ci)
10. RL / Post-training (#sig-post-training)
11. Docs / UX (docs)

### Model Tags (LLM-extracted, multi-value)
Stored as JSON arrays. 21+ model families recognized. `["General"]` for model-agnostic issues.

### Hardware Tags (LLM-extracted, multi-value)
Stored as JSON arrays. `["General"]` for hardware-agnostic issues.

## Database Schema

SQLite at `build/vllm_issue_snapshot.sqlite3`. Key columns on `issues` table:

| Column | Source | Description |
|--------|--------|-------------|
| `issue_type` | Labels + LLM | MECE type (Bug, Feature Request, etc.) |
| `sig_group` | LLM | SIG workstream name |
| `model_tags` | LLM | JSON array of model families |
| `hardware_tags` | LLM | JSON array of hardware platforms |
| `model_tag` | Regex | Single model tag (used in dashboard filtering) |
| `hardware_tag` | Regex | Single hardware tag (used in dashboard filtering) |
| `failure_mode_key` | Regex | Failure mode category |

## Incremental Updates

The pipeline is designed for daily refreshes:

1. **Load** defaults to incremental — compares `issue_id` + `updated_at` against existing SQLite rows. New issues are inserted, changed issues are updated (LLM columns cleared for reclassification), unchanged issues are skipped.
2. **Classify** skips issues that already have a `sig_group` value.
3. **Full wipe protection** — `load --full` and `refresh --full` check for existing classified issues and prompt before dropping tables, showing estimated reclassification cost.

## Dashboard

### Tabs
- **Dashboard** — Filterable table: Created, PR#, Title, State, Type, HW, Model, SIG, Age, Cmt #, Author. Sort modes: New/Hot/Top/Date Range.
- **Newsfeed** — Daily newsletters with summary bullets, issue breakdowns, prev/next navigation, collapsible sidebar (defaults collapsed).
- **Roadmap** — "Release Roadmap" showing recent/upcoming vLLM releases.
- **Resources** — Quarterly Roadmaps (10 roadmaps, H2 2023–Q1 2026), docs, community links.

### Age Classification
Computed in `build_data.py:compute_longevity()`. Only open issues:
| Label | Condition |
|-------|-----------|
| **New** | Open < 14 days |
| **Long-running** | Open 90+ days, last activity within 30 days |
| **Stale** | Open 90+ days, no activity in 30+ days |
| _(blank)_ | Open 14–89 days, or closed issues |

### Summarization Pipeline (prelims → finals → merge)
Three-step pipeline per SIG, using Claude Opus with extended thinking (10K budget):

**Step 1 — Prelims** (`dashboard-prelims`):
- Select 100 issues per SIG via 6-tier sampling that progressively relaxes engagement and recency filters:
  - T1: Top 10% comments + active 30d + open 90+ days
  - T2: Top 33% comments + active 30d + open 45+ days
  - T3: Top 10% comments + active 30d
  - T4: Top 33% comments + active 30d
  - T5: Any activity within 30d
  - T6: Backfill by recency
- Filter to actionable types: Bug, Feature Request, Usage/Question, Other (excludes RFC/Discussion)
- Split into 10 batches of 10 issues each
- Each batch gets full issue body (10K chars) + comment thread excerpts (5K chars)
- LLM picks top 3 most pressing issues per batch → 30 issues per SIG

**Step 2 — Finals** (`dashboard-finals`):
- Re-reads the 30 top issues with full body + comments in a single prompt
- Clusters by root cause (bugs) or capability theme (FRs), ranks top 15
- Output: `main_fix`, `cluster_type`, `root_cause`, `severity`, `regression_from`, `priority`

**Step 3 — Merge** (`dashboard-merge`):
- Deduplicates clusters that describe the same root cause
- Final output: top 15 clusters per SIG

### Deployment
GitHub Pages via Actions workflow. Auto-deploys on push to `dashboard/**`.

## Cost
| Step | Model | Cost |
|------|-------|------|
| Full classify (~14.5k issues) | Sonnet | ~$9 |
| Prelims (10 batches × 11 SIGs) | Opus + thinking | ~$30 |
| Finals (1 call × 11 SIGs) | Opus + thinking | ~$20 |
| Merge (1 call × 11 SIGs) | Opus + thinking | ~$15 |
| Rank (1 call) | Opus + thinking | ~$1 |
| Enrich (batched) | Sonnet | ~$3 |
| **Full pipeline** | | **~$78** |
| **Incremental daily** | | **~$5-10** |

## What We Are Not Doing
- Not building full user segmentation or account scoring
- Not attempting exhaustive coverage — report is a triage brief
- Not optimizing for public/external consumption
- Not solving perfect taxonomy accuracy — prefer useful over overfitted
