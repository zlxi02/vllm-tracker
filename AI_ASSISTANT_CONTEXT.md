# AI Assistant Context

This document provides working context for AI assistants. It reflects the current state of the project as of April 2026.

## 1. PREFERENCES

### Response and collaboration style
- Collaborative, product-minded working style
- Step-by-step when setting up new features
- Clear planning before implementation
- Concrete recommendations over abstract brainstorming
- Values rationale alongside conclusions
- Decisions documented explicitly

### Formatting
- Clean Markdown artifacts in the working folder
- Structured specs with "what we are doing" and "what we are not doing"
- Decision rationale included

### Communication
- Iterates interactively, redirects quickly if plan gets too complex
- Practical next steps over broad discussion
- Implementation blockers called out clearly

## 2. PROJECT STATE

### What this project is
A **vLLM issue tracker** that ingests GitHub issue data, classifies it with LLM, and produces:
1. An **interactive dashboard** (filterable table with SIG, model, hardware, type columns)
2. A **roadmap report** (per-SIG prioritized cluster summaries, release-notes style)

### Working directory
`/Users/zlxi/Desktop/vllm-issue-tracker`

### Pipeline (fully implemented and operational)
```
load -> dashboard-classify -> dashboard-summarize -> build-roadmap
                                                  -> build_data.py (dashboard JSON)
```

- **load**: CSV -> SQLite. Regex tags + label-based issue_type parsing
- **dashboard-classify**: LLM classifies every issue with: `sig_group`, `model_tags` (JSON array), `hardware_tags` (JSON array), `issue_type` (fallback for unlabeled issues)
- **dashboard-summarize**: LLM clusters issues per SIG group, generates one-line summaries per issue, outputs `build/dashboard_summary.json`
- **build-roadmap**: Renders `outputs/roadmap.html` from summary JSON, copies to `dashboard/report.html`
- **build_data.py**: Reads from SQLite (prefers LLM-classified fields), outputs `dashboard/data/dashboard_data.json`

### Classification system
- **Issue types** (MECE): Bug, Feature Request, Usage/Question, Model Request, RFC/Discussion, Other. GitHub labels first, LLM fallback.
- **SIG groups**: 11 fixed workstream themes (Core Engine, Large Scale Serving, Performance, Torch Compile, Frontend/API, Multimodality, Quantization/Model Acceleration, Model Support, Installation/Build/CI, RL/Post-training, Docs/UX). LLM-only classification.
- **Model tags**: JSON arrays, LLM-extracted. 21 model families: Qwen, DeepSeek, Llama, Gemma, Mistral, Mixtral, Phi, Yi, GLM, MiniMax, Falcon, InternLM, Baichuan, StarCoder, Command-R, Jamba, Mamba, GPT, Nemotron, Granite, Cohere. `["General"]` for agnostic issues.
- **Hardware tags**: JSON arrays, LLM-extracted. `["General"]` for agnostic issues.
- **Failure modes**: Regex-based only (legacy), not LLM-classified for dashboard.

### Legacy pipeline (separate, still functional)
- `discover-taxonomy` / `llm-classify` / `llm-validate` — taxonomy discovery + bug cluster classification
- `build-report` / `refresh` — legacy HTML snapshot report using regex tags + taxonomy
- `ROADMAP_PATTERNS` in classify.py — NOT used for dashboard, only legacy report

### Dashboard features
- Filterable table: Created, PR#, Title, State, Type, HW, Model, SIG, Age, Cmt #, Author
- Column filter dropdowns (lazy-loaded in batches of 50), sort modes (New/Hot/Top + Date Range), search tabs
- Expandable issue cards with sidebar metadata ordered to match column layout
- Roadmap tab — "Release Roadmap" with recent/upcoming releases
- Resources tab — Quarterly Roadmaps (10 roadmaps from H2 2023 to Q1 2026, top 3 shown, rest behind "show more"), docs, community links
- Newsfeed tab — daily newsletters, sidebar defaults collapsed
- Served via `dashboard/serve.py` on port 8000

### Age classification (computed in `build_data.py:compute_longevity()`)
Only open issues get a label:
- **New**: open < 14 days
- **Long-running**: open 90+ days, last activity within 30 days
- **Stale**: open 90+ days, no activity in 30+ days
- _(blank)_: open 14–89 days, or closed issues

### Key data
- 14,522 issues from `data/github_issues.csv`
- SQLite at `build/vllm_issue_snapshot.sqlite3`
- Dashboard JSON at `dashboard/data/dashboard_data.json`
- Summary at `build/dashboard_summary.json`
- Roadmap at `outputs/roadmap.html`

### Environment
- Python >=3.9, dependencies: anthropic, openai, python-dotenv, tqdm
- LLM config via `.env`: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `LLM_MODEL`
- Default model: claude-sonnet-4-20250514
- CI: GitHub Actions (`ci.yml` for tests, `preview-report.yml` for manual builds)

## 3. KEY DECISIONS

- **SIG groups are the primary organization** for both dashboard filtering and roadmap report
- **LLM over regex** for dashboard classification — regex is only for the legacy report pipeline
- **Multi-value model/hardware** as JSON arrays — issues can mention multiple models/hardware
- **GitHub labels first, LLM fallback** for issue type classification
- **Roadmap styling matches dashboard** — system font, same color palette, loads in iframe
- **Model lists synced** across classify.py, prompts.py, and build_data.py (21 families)

## 4. KNOWN ISSUES / NEXT STEPS

- 2 SIG groups (Model Support + 1 other) had JSON parse errors during summarization — can re-run `dashboard-summarize`
- 32 issues (0.2%) have NULL sig_group — uncategorized
- The roadmap report currently shows 9/11 SIGs (the 2 with parse errors are missing)
- The legacy `build-report` command still generates the old-style report — could be deprecated or updated
- Dashboard `build_data.py` falls back to CSV+regex when SQLite doesn't have classifications — this dual path works but adds complexity
