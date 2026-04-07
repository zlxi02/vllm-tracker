"""
Prompt templates for LLM-based issue classification and roadmap generation.

Dashboard pipeline:
  - DASHBOARD_CLASSIFY_BATCH: classify issues by type, SIG, models, hardware
  - DASHBOARD_SUMMARIZE_SIG: generate per-SIG summaries from classified issues
  - RANK_AND_SUMMARIZE_ROADMAP: rank SIGs by priority and generate executive summary
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Fixed workstream themes — derived from vLLM roadmap SIGs
# These are hard-coded and never invented by the LLM.
# ---------------------------------------------------------------------------

WORKSTREAM_THEMES = [
    {
        "name": "Core Engine",
        "sig": "#sig-core",
        "description": "Scheduler, memory management, block allocation, attention backends, model runner, weight loading, data structure bugs.",
    },
    {
        "name": "Large Scale Serving",
        "sig": "#sig-large-scale-serving",
        "description": "Distributed inference, tensor parallelism, pipeline parallelism, expert parallelism, disaggregated prefill/decode, multi-node coordination, NCCL, DeepEP.",
    },
    {
        "name": "Performance",
        "sig": "#sig-model-performance",
        "description": "Performance regressions between versions, throughput degradation, latency issues, profiling, benchmarking, startup time.",
    },
    {
        "name": "Torch Compile",
        "sig": "#sig-torch-compile",
        "description": "Torch compilation hangs, CUDA graph issues, kernel optimization, compilation timeouts, custom compiler loading.",
    },
    {
        "name": "Frontend / API",
        "sig": "#sig-frontend",
        "description": "OpenAI-compatible API server, tool calling, structured output, streaming, HTTP errors, request validation, chat completions, Responses API.",
    },
    {
        "name": "Multimodality",
        "sig": "#sig-multi-modality",
        "description": "Vision-language models, image/video/audio processing, multimodal encoders, token mismatch in batch inference, VLM-specific crashes.",
    },
    {
        "name": "Quantization / Model Acceleration",
        "sig": "#sig-quantization",
        "description": "FP8, AWQ, GPTQ, GGUF, online quantization, precision handling, quantization method conflicts, kernel dispatch for quantized models.",
    },
    {
        "name": "Model Support",
        "sig": "model-support-program",
        "description": "Model architecture not recognized, config/attribute mismatches for specific models, new model requests, model-family-specific bugs (Qwen, DeepSeek, Llama, etc).",
    },
    {
        "name": "Installation / Build / CI",
        "sig": "#sig-ci",
        "description": "Installation failures, CUDA dependency errors, platform compatibility (ROCm, CPU, XPU, Docker), build from source, CI test failures, release infrastructure.",
    },
    {
        "name": "RL / Post-training",
        "sig": "#sig-post-training",
        "description": "RLHF, determinism, weight sync, multi-turn scheduling, batch invariance, post-training workflows.",
    },
    {
        "name": "Docs / UX",
        "sig": "docs",
        "description": "Documentation issues, missing examples, usability problems, usage questions, configuration guidance, recipes.",
    },
]


def _format_workstream_list() -> str:
    """Format the fixed workstream themes for inclusion in prompts."""
    parts = []
    for idx, ws in enumerate(WORKSTREAM_THEMES, start=1):
        parts.append(f"{idx}. {ws['name']} ({ws['sig']}): {ws['description']}")
    return "\n".join(parts)


WORKSTREAM_LIST_BLOCK = _format_workstream_list()


# ---------------------------------------------------------------------------
# Dashboard classification — combined issue_type + SIG + models + hardware
# ---------------------------------------------------------------------------

DASHBOARD_CLASSIFY_BATCH = """\
You are classifying GitHub issues from the vLLM project (a high-performance \
LLM inference engine) for a dashboard.

For each issue, provide these four classifications:

1. ISSUE TYPE (exactly one of: Bug, Feature Request, Usage/Question, \
Model Request, RFC/Discussion, Other)
   - If the issue already has a "known_type" provided (not NONE), use it exactly.
   - Otherwise, classify based on the title and body content.
   - Bug = something is broken or producing wrong results.
   - Feature Request = new capability, enhancement, or improvement.
   - Usage/Question = how-do-I, configuration help, documentation question.
   - Model Request = request to support a new model architecture.
   - RFC/Discussion = design proposal or architectural discussion.
   - Other = does not fit any of the above.

2. SIG GROUP (exactly one of the following — use the exact name):
{workstream_list}

3. MODEL TAGS (JSON array of model families mentioned in the issue):
   - Extract ALL model families mentioned: Qwen, DeepSeek, Llama, Gemma, \
Mistral, Mixtral, Phi, Yi, GLM, MiniMax, Falcon, InternLM, Baichuan, \
StarCoder, Command-R, Jamba, Mamba, GPT, Nemotron, Granite, Cohere, \
or any other identifiable model family.
   - Use ["General"] if no specific model is mentioned or the issue is \
model-agnostic (e.g. a general API bug or build issue).
   - Do NOT include "General" alongside specific models — either list \
specific models or use ["General"] alone.

4. HARDWARE TAGS (JSON array of hardware platforms mentioned):
   - Extract ALL hardware mentioned: H100, H200, A100, A10, L40S, B200, \
B300, GB200, GB300, MI300, MI355, ROCm, AMD, CUDA, NVIDIA, Jetson, CPU, \
TPU, Intel, XPU, Neuron, or any other identifiable hardware platform.
   - Use ["General"] if no specific hardware is mentioned or the issue is \
hardware-agnostic.
   - Do NOT include "General" alongside specific hardware.

Below are {batch_size} issues. Each line has the format:
  ISSUE <number> [known_type: <type or NONE>] | <title> | <first 200 chars of body>

Return JSON only — no markdown fences, no commentary:
[
  {{
    "issue_number": 1234,
    "issue_type": "Bug",
    "sig_group": "Core Engine",
    "model_tags": ["Qwen"],
    "hardware_tags": ["H100", "A100"]
  }}
]

Issues:
{issues_block}
"""


# ---------------------------------------------------------------------------
# Dashboard summarization — per-SIG roadmap-style summary
# ---------------------------------------------------------------------------

DASHBOARD_SUMMARIZE_SIG = """\
You are producing a roadmap-style summary of OPEN issues for one SIG \
(Special Interest Group) from the vLLM project. These are unresolved \
problems and requests that need attention.

SIG Group: {sig_group}
SIG Description: {sig_description}

CONTEXT — CURRENT QUARTER ROADMAP OBJECTIVES FOR THIS SIG:
{roadmap_context}

Use the roadmap objectives above to understand what the team is actively \
working on. When clustering issues:
- Flag issues that are BLOCKING a roadmap objective.
- Note when a cluster of issues aligns with (or undermines) a planned \
milestone — e.g. "blocks model runner V2 default rollout" or "regression \
in feature shipped for Elastic EP beta".
- Issues unrelated to the roadmap are still important — don't ignore them.

CONTEXT — RECENT vLLM RELEASES (what has already shipped):
{release_notes}

Use these releases to understand what was recently shipped. If an open \
issue is about something that was supposedly fixed in a release, it may \
be a regression or incomplete fix — flag that in your summary.

SPECIFICITY GUIDE — match this level of detail in your cluster names:

GOOD (specific failure + component + trigger condition):
  - "CUDA error 803: system has unsupported display driver/CUDA driver \
combination on hosts with older drivers"
  - "Blackwell RTX 50-series (SM120/SM121) support missing in prebuilt \
wheels and Docker images"
  - "FP8 MLA attention + KV cache produces garbled output and CUDA \
graph failures"
  - "MTP speculative decoding crashes with encoder cache misses under \
high concurrency for multimodal models"

BAD (vague category labels that don't tell an engineer what to investigate):
  - "Critical GPU memory access violations and OOMs under high load \
across all architectures"
  - "Weight loading performance regression and memory issues"
  - "CPU offloading failures and memory management bugs"
  - "Tool calling parser failures and crashes across multiple models \
(Gemma4, Qwen3.5, GLM, DeepSeek, Kimi)"
  - "Reasoning content parsing and streaming issues across reasoning \
models (Qwen3.5, Gemma4, GPT-OSS, MiniMax)"
  - "Structured output and guided decoding failures with JSON schema, \
grammar, and reasoning modes"

The BAD examples bundle too many distinct root causes under one label. \
Split them: "Gemma 4 tool calling parser returns empty content on \
streaming" is one cluster; "DeepSeek v3.2 tool calling drops arguments \
in multi-tool responses" is a different cluster. An engineer reading \
the cluster name alone should know exactly what to investigate.

YOUR TASK:
1. Group related OPEN issues into clusters — issues about the same \
underlying problem or feature request belong together.
2. For each cluster, write:
   - main_fix: A specific, actionable description of the problem or \
request. Include the affected model, hardware, or component.
   - priority: Integer rank (1 = most important). Rank by severity, \
user count, and recency.
   - issues: Related issue numbers with a one-line summary each.
   - categories: Dominant issue_type, models, hardware.
3. Order clusters by priority (most important first).

Rules:
- Target 5-20 clusters depending on issue count.
- Each issue must appear in exactly one cluster.
- Singleton clusters are fine for severe or unique issues.
- Bugs should be grouped specifically by failure mode + component.
- Feature requests can be grouped more broadly by theme.
- Note if a cluster appears to be a regression from a recent release.
- Do NOT bundle unrelated root causes under one vague label.

Below are {issue_count} OPEN issues in "{sig_group}". Each line:
  ISSUE <number> [type: <type>] [models: <models>] [hw: <hw>] | <title> | <body>

Return JSON only — no markdown fences, no commentary:
{{
  "sig_group": "{sig_group}",
  "clusters": [
    {{
      "main_fix": "Gemma 4 FP8 dynamic quantization produces gibberish — likely v0.19.0 regression",
      "priority": 1,
      "issues": [
        {{"number": 39049, "summary": "FP8 quantization on Gemma 4 outputs nonsensical text on H100"}},
        {{"number": 39037, "summary": "Gemma 4 31B-AWQ hangs with 0 running / 4 waiting"}}
      ],
      "categories": {{
        "type": "Bug",
        "models": ["Gemma"],
        "hardware": ["H100"]
      }}
    }}
  ]
}}

Issues:
{issues_block}
"""


PRIORITIZE_SIG_CLUSTERS = """\
You are re-prioritizing and enriching issue clusters for one SIG \
(Special Interest Group) from the vLLM project. You will receive \
clusters that were generated by a previous pass. Your job is to \
re-rank them using strategic context and add enrichment fields.

SIG Group: {sig_group}

CURRENT QUARTER ROADMAP OBJECTIVES FOR THIS SIG:
{roadmap_context}

RECENT RELEASES (what has already shipped):
{release_notes}

CLUSTERS TO RE-PRIORITIZE:
{clusters_block}

YOUR TASK:
1. RE-RANK clusters by strategic priority. Weight these factors:
   - Does this cluster BLOCK a roadmap objective? (highest weight)
   - Is this a REGRESSION from a recent release? (high weight)
   - Comment count and reopen count across issues (user pain signal)
   - Number of affected issues in the cluster
   - Severity of impact (crashes > wrong output > perf regression > UX)

2. ENRICH each cluster with these new fields:
   - severity: "critical" | "high" | "medium" | "low"
     critical = crashes, data corruption, blocks major feature
     high = wrong output, significant perf regression, blocks users
     medium = workarounds exist, affects subset of users
     low = cosmetic, edge case, nice-to-have fix
   - roadmap_impact: Which roadmap objective this blocks or undermines, \
or null if unrelated. Be specific: "Blocks MRV2 default rollout" not \
just "Related to Core Engine".
   - regression_from: Release version this regressed from (e.g. "v0.19.0"), \
or null if not a regression.

3. SORT issues within each cluster by engagement (most comments first).

Return JSON only — no markdown fences, no commentary:
{{
  "sig_group": "{sig_group}",
  "clusters": [
    {{
      "main_fix": "original cluster name (do not change)",
      "priority": 1,
      "severity": "critical",
      "roadmap_impact": "Blocks MRV2 default rollout — V2 engine crashes under concurrent load",
      "regression_from": "v0.19.0",
      "issues": [
        {{"number": 37729, "summary": "original summary (do not change)", "comments": 15, "reopens": 2}}
      ],
      "categories": {{"type": "Bug", "models": ["Qwen"], "hardware": ["H100"]}}
    }}
  ]
}}

IMPORTANT:
- Do NOT merge, split, rename, or remove clusters. Keep the exact same \
cluster names and issue assignments. You are ONLY re-ranking and adding fields.
- Every cluster from the input must appear in the output.
- The priority field should be re-assigned (1 = most important).
"""


RANK_AND_SUMMARIZE_ROADMAP = """\
You are producing an executive summary and priority ranking for the vLLM \
project's issue roadmap. vLLM is the leading open-source high-performance \
LLM inference engine.

CURRENT QUARTER ROADMAP — official planned objectives from the vLLM team:
{roadmap_context}

Use the roadmap above to understand what the team has committed to. When \
ranking SIGs, weigh issues that block or undermine roadmap objectives \
more heavily. Call out in the executive summary when a cluster of open \
issues threatens a planned milestone.

Below are the SIG group summaries from the current issue pool. Each entry \
shows the SIG name, total issue count, number of clusters, and the top 5 \
cluster headlines.

{sig_summaries_block}

YOUR TASK:

1. RANK the SIG groups by priority for the roadmap. Consider:
   - Strategic importance to vLLM's mission and roadmap
   - Issue volume and severity (more critical bugs = higher priority)
   - User impact (issues affecting more users or key enterprise deployments)
   - Momentum (areas with many recent/trending issues)
   Weight strategic importance and user impact more heavily than raw issue count.

2. WRITE an executive summary (4-8 bullet points) for the entire roadmap:
   - Each bullet has a "topic" (short bold label, 2-5 words) and "detail" \
(one sentence explaining what is happening and why it matters)
   - Lead with the single most critical finding across all SIGs
   - Highlight cross-cutting themes (e.g. "Blackwell support issues span 3 SIGs")
   - Call out the most severe user-facing problems
   - Note any emerging trends or areas of concern
   - Be specific and actionable — an engineering leader should read this and \
know what to prioritize this week
   - Do NOT just list SIG names — synthesize across them

Return JSON only — no markdown fences, no commentary:
{{
  "ranked_sigs": [
    {{
      "sig_group": "Core Engine",
      "rank": 1,
      "rationale": "One sentence on why this SIG is ranked here"
    }}
  ],
  "executive_summary": [
    {{"topic": "Short bold topic label", "detail": "What is happening and why it matters"}},
    {{"topic": "Another topic", "detail": "Explanation of the issue"}}
  ]
}}
"""


def format_summarize_issues_block(issues: list[dict]) -> str:
    """Format issues for the per-SIG summarization prompt.

    Each dict should have: issue_number, title, body, issue_type, model_tags, hardware_tags.
    Optionally: comments, reopens for engagement signal.
    """
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:200]
        itype = issue.get("issue_type") or "Other"
        models = issue.get("model_tags") or "General"
        hardware = issue.get("hardware_tags") or "General"
        comments = issue.get("comments", 0)
        reopens = issue.get("reopens", 0)
        lines.append(
            f"ISSUE {number} [type: {itype}] [comments: {comments}] [reopens: {reopens}] "
            f"[models: {models}] [hw: {hardware}] | {title} | {body}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Newsfeed — daily digest of issue activity
# ---------------------------------------------------------------------------

GENERATE_NEWSFEED = """\
You are writing a daily issue digest for the vLLM project — a high-performance \
LLM inference engine. The audience is vLLM maintainers and internal engineers. \
Write like a sharp engineering newsletter: opinionated, specific, no fluff.

DATE: {date_display}

CONTEXT — what shipped recently:
{release_context}

Below are {issue_count} GitHub issues that were created or had significant \
activity on {date_display}. Each has the format:
  ISSUE <number> [type: <type>] [state: <state>] [comments: <N>] \
[models: <models>] [hw: <hw>] | <title> | <body excerpt>

YOUR TASK:
Write a daily digest as a JSON object with these fields:

1. "headline": A punchy, specific one-line headline for the day. Reference \
the single biggest story. Examples of GOOD headlines:
   - "Gemma 4 Dropped 3 Days Ago. The Bugs Are Pouring In."
   - "v0.19.0 Day Two: 31 Issues. The Floodgates Are Open."
   - "Qwen3.5 Is Having a Bad Week on New Hardware"
   BAD headlines (too vague):
   - "Multiple Issues Reported Today"
   - "Bug Fixes and Feature Requests"

2. "opening": A 1-2 sentence hook that opens with "Today in vLLM:" and gives \
the day's narrative in a way that tells the reader whether to pay attention. \
Be specific about what's happening and why it matters. Examples:
   - "Today in vLLM: v0.19.0 shipped on April 1 with Gemma 4 support as \
the flagship feature. Three days later, Gemma 4 is the single biggest source \
of new issues."
   - "Today in vLLM: The busiest day of the week — 31 issues filed. The \
Qwen3.5 thinking mode bug has 14 comments and counting."

3. "callout": An optional "Why this matters" note (1-2 sentences) for the \
lead story. Include only if there's a strong narrative. Set to null otherwise.

4. "sections": Array of sections, each with:
   - "title": Section headline (e.g. "Gemma 4 Bugs Continue", "Stability", \
"Feature Requests & Infrastructure")
   - "items": Array of issue items, each with:
     - "number": Issue number
     - "emoji": A single emoji that captures the issue (use &#NNNNN; HTML entity \
format): 💥=crash, 🐛=bug, 🔧=fix, 🐢=perf, 🚀=feature, 🔍=investigation, \
⚠️=warning, 🔗=dependency, 📊=data, 🛑=blocker, ✅=closed/fixed
     - "title_html": Issue title as HTML (include link: \
<a href="https://github.com/vllm-project/vllm/issues/NUMBER" target="_blank">\
#NUMBER</a>). If closed, add \
<span style="color:var(--green);font-size:11px;">CLOSED</span>
     - "desc": 1-2 sentence description. Be specific about what's broken and \
why it matters. Don't just repeat the title.

5. "bottom_line": A 1-3 sentence "Bottom Line" summary in bold. What should \
an engineering lead take away from today? Be opinionated.

6. "stats": Object with "issues": <total>, "comments": <total comments \
across all issues>, "closed": <how many were closed/resolved>

Rules:
- Group related issues into sections with clear themes (not just "Bugs" and "Other")
- Lead with the most important/engaging story
- 3-5 sections, 2-6 items per section
- Flag regressions from recent releases
- If it's a quiet day, say so — don't manufacture drama
- Every issue number MUST correspond to a real issue from the input

Return JSON only — no markdown fences, no commentary:
{{
  "headline": "...",
  "opening": "Today in vLLM: ...",
  "callout": "Why this matters: ..." or null,
  "sections": [
    {{
      "title": "Section Title",
      "items": [
        {{
          "number": 39049,
          "emoji": "&#128165;",
          "title_html": "Gemma 4 FP8 dynamic quantization = gibberish output (<a href=\\"https://github.com/vllm-project/vllm/issues/39049\\" target=\\"_blank\\">#39049</a>)",
          "desc": "FP8 quantization on Gemma 4 produces nonsensical output."
        }}
      ]
    }}
  ],
  "bottom_line": "<strong>v0.19.0 is a rough release.</strong> Teams should stay on v0.18.1.",
  "stats": {{"issues": 11, "comments": 16, "closed": 1}}
}}

Issues:
{issues_block}
"""


def format_newsfeed_issues_block(issues: list[dict]) -> str:
    """Format issues for the newsfeed generation prompt."""
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:300]
        itype = issue.get("issue_type") or "Other"
        state = issue.get("state") or "open"
        models = issue.get("model_tags") or "General"
        hardware = issue.get("hardware_tags") or "General"
        comments = issue.get("comments", 0)
        lines.append(
            f"ISSUE {number} [type: {itype}] [state: {state}] [comments: {comments}] "
            f"[models: {models}] [hw: {hardware}] | {title} | {body}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Issue enrichment — per-issue problem/fix summaries
# ---------------------------------------------------------------------------

ENRICH_ISSUES_BATCH = """\
You are summarizing GitHub issues from the vLLM project for an engineering \
roadmap report. For each issue, write a concise two-part summary.

**problem**: 1-3 sentences. What is happening? Be specific about the error, \
affected model, hardware, and vLLM version. Translate non-English content \
to English.

**suggested_fix**: 1-3 sentences. Based on the issue details and comments, \
what is the likely root cause and what should be investigated? If no fix is \
obvious, suggest debugging steps.

Be direct and technical. An engineer should read the summary and immediately \
understand what's broken and where to start looking.

Below are {batch_size} issues. Each has the format:
  ISSUE <number> | <title> | <body excerpt> | COMMENTS: <comment excerpts>

Return JSON only — no markdown fences, no commentary:
[
  {{
    "issue_number": 1234,
    "problem": "...",
    "suggested_fix": "..."
  }}
]

Issues:
{issues_block}
"""


def format_enrich_issues_block(issues: list[dict]) -> str:
    """Format issues for the enrichment prompt.

    Each dict should have: issue_number, title, body.
    Optionally: comments (list of dicts with author, body).
    """
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:1000]
        comments_raw = issue.get("comments", [])
        if comments_raw:
            comment_strs = []
            for c in comments_raw[:5]:
                cauthor = c.get("author", "?")
                cbody = c.get("body", "").replace("\n", " ").strip()[:300]
                comment_strs.append(f"@{cauthor}: {cbody}")
            comments_text = " | ".join(comment_strs)
        else:
            comments_text = "(none)"
        lines.append(f"ISSUE {number} | {title} | {body} | COMMENTS: {comments_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers for formatting prompt inputs
# ---------------------------------------------------------------------------

def format_issues_block(issues: list[dict]) -> str:
    """Format issues into the line-per-issue format used by all prompts.

    Each dict should have at minimum: issue_number, title.
    Optionally: body (will be truncated to 200 chars).
    """
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:200]
        lines.append(f"ISSUE {number} | {title} | {body}")
    return "\n".join(lines)



def format_dashboard_issues_block(issues: list[dict]) -> str:
    """Format issues for the dashboard classification prompt.

    Each dict should have: issue_number, title, body, and optionally issue_type.
    Issues with a known issue_type get [known_type: <type>]; others get [known_type: NONE].
    """
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:200]
        known_type = issue.get("issue_type") or "NONE"
        lines.append(f"ISSUE {number} [known_type: {known_type}] | {title} | {body}")
    return "\n".join(lines)


