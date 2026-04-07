# Product Spec: vLLM Issue Tracker

## Summary
A pipeline that ingests vLLM GitHub issue data, classifies issues using LLM, and produces two outputs:
1. **Interactive dashboard** — filterable table of all issues with SIG group, model, hardware, type, and failure mode columns
2. **Roadmap report** — per-SIG prioritized cluster summaries in release-notes style, embedded in the dashboard

## Architecture

### Pipeline steps
```
data/github_issues.csv
  -> [load] CSV -> SQLite (regex tags + label-based issue_type)
  -> [dashboard-classify] LLM classifies: sig_group, model_tags, hardware_tags, issue_type (fallback)
  -> [dashboard-summarize] LLM clusters issues per SIG, generates one-line summaries
  -> [build-roadmap] Renders roadmap HTML from summary JSON
  -> [build_data.py] Exports SQLite -> dashboard JSON
```

### CLI commands
```bash
# Core pipeline
python3 -m vllm_issue_tracker.cli load                # CSV -> SQLite
python3 -m vllm_issue_tracker.cli dashboard-classify   # LLM classify all issues
python3 -m vllm_issue_tracker.cli dashboard-summarize  # LLM per-SIG summaries
python3 -m vllm_issue_tracker.cli build-roadmap        # Render roadmap HTML
python3 -m vllm_issue_tracker.cli build-report         # Legacy snapshot report

# Legacy LLM taxonomy pipeline (separate from dashboard)
python3 -m vllm_issue_tracker.cli discover-taxonomy
python3 -m vllm_issue_tracker.cli llm-classify
python3 -m vllm_issue_tracker.cli llm-validate

# Utilities
python3 -m vllm_issue_tracker.cli refresh              # load + build-report
python3 -m vllm_issue_tracker.cli quality-check
```

### Key files
| File | Role |
|------|------|
| `src/vllm_issue_tracker/cli.py` | CLI entry point |
| `src/vllm_issue_tracker/config.py` | Settings, paths, LLM config |
| `src/vllm_issue_tracker/ingest.py` | CSV -> SQLite, label parsing |
| `src/vllm_issue_tracker/classify.py` | Regex model/hardware/failure-mode (legacy report) |
| `src/vllm_issue_tracker/prompts.py` | All LLM prompt templates + workstream themes |
| `src/vllm_issue_tracker/llm_classify.py` | LLM orchestration (classify, summarize, taxonomy) |
| `src/vllm_issue_tracker/report.py` | Legacy report HTML + roadmap report HTML |
| `dashboard/build_data.py` | SQLite -> dashboard JSON |
| `dashboard/index.html` | Interactive dashboard UI |
| `dashboard/serve.py` | Dev server (port 8000) |

## Classification System

### MECE Issue Types
Assigned via GitHub labels first, LLM fallback for unlabeled issues:
- **Bug** — something is broken
- **Feature Request** — new capability or enhancement
- **Usage/Question** — how-do-I / config help
- **Model Request** — new model support
- **RFC/Discussion** — design proposals
- **Other**

Label mapping (in `ingest.py`):
| GitHub Label | Issue Type |
|---|---|
| `bug` | Bug |
| `feature request` | Feature Request |
| `documentation`, `usage` | Usage/Question |
| `new-model` | Model Request |
| `rfc` | RFC/Discussion |

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
Stored as JSON arrays. 21 model families recognized:
Qwen, DeepSeek, Llama, Gemma, Mistral, Mixtral, Phi, Yi, GLM, MiniMax, Falcon, InternLM, Baichuan, StarCoder, Command-R, Jamba, Mamba, GPT, Nemotron, Granite, Cohere

`["General"]` when issue is model-agnostic.

### Hardware Tags (LLM-extracted, multi-value)
Stored as JSON arrays. Recognized platforms include:
H100, H200, A100, L40S, B200, B300, GB200, GB300, MI300, MI355, ROCm, AMD, CUDA, NVIDIA, Jetson, CPU, TPU, Intel, XPU, Neuron

`["General"]` when issue is hardware-agnostic.

### Legacy regex classification (still used by `build-report`)
`classify.py` has regex-based `model_tag`, `hardware_tag`, `failure_mode_key`, `roadmap_tag`. These are computed during `load` and used by the legacy report. The `ROADMAP_PATTERNS` are NOT used for dashboard classification.

## Database Schema

SQLite at `build/vllm_issue_snapshot.sqlite3`. Key columns on `issues` table:

| Column | Source | Description |
|--------|--------|-------------|
| `issue_type` | Labels + LLM | MECE type (Bug, Feature Request, etc.) |
| `sig_group` | LLM | SIG workstream name |
| `model_tags` | LLM | JSON array of model families |
| `hardware_tags` | LLM | JSON array of hardware platforms |
| `model_tag` | Regex | Single model (legacy) |
| `hardware_tag` | Regex | Single hardware (legacy) |
| `failure_mode_key` | Regex | Failure mode (legacy) |
| `roadmap_tag` | Regex | Roadmap area (legacy) |
| `llm_workstream` | LLM taxonomy | Bug cluster workstream (legacy taxonomy pipeline) |
| `llm_bug_cluster` | LLM taxonomy | Bug cluster name (legacy taxonomy pipeline) |

## Dashboard

### Features
- Filterable table with columns: Created, PR#, Title, State, Type, HW, Model, SIG, Age, Cmt #, Author
- Column filter dropdowns with counts (lazy-loaded in batches of 50 for large lists like Author)
- Sort modes: New, Hot, Top (with period selector), Date Range
- Expandable issue cards with full body and sidebar metadata (order matches column layout)
- Search tabs for saved filter states
- Roadmap tab — "Release Roadmap" showing recent/upcoming vLLM releases
- Resources tab with Quarterly Roadmaps (10 roadmaps, H2 2023–Q1 2026, show 3 + "show more"), docs, and community links
- Newsfeed tab — daily issue newsletters with collapsible sidebar (defaults collapsed)

### Age Classification
Computed in `build_data.py:compute_longevity()`. Only open issues get an age label:
| Label | Condition |
|-------|-----------|
| **New** | Open, created within the last 14 days |
| **Long-running** | Open 90+ days, last activity within 30 days (still active) |
| **Stale** | Open 90+ days, no activity in 30+ days (neglected) |
| _(blank)_ | Open 14–89 days, or closed issues |

"Last activity" is determined by the most recent comment date (SQLite path) or `updated_at` as a proxy (CSV fallback path).

### Data flow
`build_data.py` reads from SQLite when dashboard classifications exist, falls back to CSV + regex otherwise. Outputs `dashboard/data/dashboard_data.json` + chunked body files.

## Roadmap Report

### Structure
Per-SIG collapsible sections, each with a table:
| # | Main Issue / Fix | Issues & Summaries | Categories |
|---|-----------------|-------------------|------------|
| 1 | Description | #123 One-line summary | Bug, H100, Qwen |

Generated from `build/dashboard_summary.json` by the `dashboard-summarize` -> `build-roadmap` pipeline.

### Styling
Matches the dashboard's design system (system font, #f8f9fa background, #0d6efd primary, clean modern look) so it loads seamlessly in the dashboard's roadmap iframe.

## Cost Envelope
- Dashboard classification (~14.5k issues / 50 per batch): ~$9 per full run (Sonnet)
- Dashboard summarization (11 SIG groups): ~$3-5 per run (Sonnet)
- Incremental runs (unclassified only): proportionally less

## What We Are Not Doing
- Not building full user segmentation or account scoring
- Not attempting exhaustive coverage — report is a triage brief
- Not optimizing for public/external consumption
- Not solving perfect taxonomy accuracy — prefer useful over overfitted
