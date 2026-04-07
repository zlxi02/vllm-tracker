# vLLM Issue Tracker

**[View the live dashboard](https://zlxi02.github.io/vllm-tracker/)**

Tracks and analyzes 14,500+ GitHub issues from the [vLLM project](https://github.com/vllm-project/vllm) to surface what the team should focus on next. Issues are classified by SIG group, model, hardware, and type using LLM, then clustered into prioritized recommendations.

## Dashboard

- **Issues** — Searchable table of all issues with filters for type, SIG, hardware, model, and failure mode
- **Newsfeed** — Daily newsletter-style summaries of notable issues and emerging trends
- **Roadmap** — Prioritized recommendations by SIG group, generated from issue clustering
- **Resources** — Release schedule, project links, community

## How it works

A Python pipeline ingests CSV exports of vLLM GitHub issues and runs them through a multi-stage LLM classification and summarization process:

```
CSV export → SQLite → LLM classification → per-SIG clustering → priority ranking → HTML report
```

1. **Load** — Parses issue data into SQLite with regex-based enrichment (model tags, hardware tags, failure modes)
2. **Classify** — LLM assigns each issue a SIG group, model families, hardware platforms, and issue type
3. **Summarize** — LLM clusters related issues within each SIG and generates one-line summaries, informed by the current vLLM roadmap and recent release notes
4. **Rank** — LLM produces a priority ranking across SIGs with an executive summary
5. **Render** — Generates the roadmap report and dashboard data

The pipeline supports incremental updates — only new or changed issues are reclassified on each run.
