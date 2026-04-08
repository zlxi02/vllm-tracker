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
# Dashboard summarization — three-step pipeline: prelims → finals → merge
# ---------------------------------------------------------------------------

PRELIMS_SUMMARIZE = """\
You are triaging OPEN issues for one SIG (Special Interest Group) from \
the vLLM project. You will receive a small batch of issues with full \
body text and comment threads. Read each issue deeply.

SIG Group: {sig_group}
SIG Description: {sig_description}

CONTEXT — CURRENT QUARTER ROADMAP OBJECTIVES FOR THIS SIG:
{roadmap_context}

CONTEXT — RECENT vLLM RELEASES (what has already shipped):
{release_notes}

You have detailed issue bodies (up to 10,000 chars) AND comment thread \
excerpts (up to 5,000 chars) for each issue. The comments often contain \
root cause analysis, workarounds, and diagnostic findings. READ THEM \
CAREFULLY — the comment thread is often more informative than the body.

YOUR TASK:
From the {issue_count} issues below, select the TOP 3 most pressing \
issues. "Most pressing" means:
  - Crashes, data corruption, or blocks a roadmap objective (highest)
  - Regressions from a recent release (high)
  - High comment count or reopens = many users affected (high)
  - Wrong output or significant perf regression (medium)
  - Feature requests that many users want (lower)

For each selected issue, write:
  - number: The issue number
  - summary: One-line description of the problem
  - why_pressing: 1-2 sentences explaining why this is urgent
  - cluster_type: "bug", "feature_request", "usage", or "other"
  - severity: "critical" | "high" | "medium" | "low"
  - regression_from: Release version this regressed from, or null
  - categories: Dominant type, models, hardware

Return JSON only — no markdown fences, no commentary:
{{
  "sig_group": "{sig_group}",
  "top_issues": [
    {{
      "number": 37729,
      "summary": "V1 engine deadlocks with fp8 + prefix caching under concurrent load",
      "why_pressing": "Zero throughput with healthy endpoint, no recovery. 15 comments, confirmed on H100 and Blackwell.",
      "cluster_type": "bug",
      "severity": "critical",
      "regression_from": null,
      "categories": {{"type": "Bug", "models": ["Qwen"], "hardware": ["H100"]}}
    }}
  ]
}}

Issues:
{issues_block}
"""


FINALS_RANK = """\
You are producing the final ranked list of the most pressing open issues \
for one SIG (Special Interest Group) from the vLLM project.

SIG Group: {sig_group}
SIG Description: {sig_description}

CONTEXT — CURRENT QUARTER ROADMAP OBJECTIVES FOR THIS SIG:
{roadmap_context}

CONTEXT — RECENT vLLM RELEASES (what has already shipped):
{release_notes}

Below are {issue_count} issues that were identified as the most pressing \
across multiple preliminary rounds. You have full issue bodies and comment \
threads for each.

YOUR TASK:
Read each issue deeply — use the body AND comments to understand the \
actual root cause. Then rank ALL {issue_count} issues from most pressing \
to least pressing. Do NOT group or cluster — each issue is its own entry. \
Return the top 15.

For each issue, write:
  - number: Issue number
  - summary: One-line description of the problem
  - main_fix: Specific actionable description of what needs to be fixed. \
An engineer reading just this should know what code to investigate.
  - why_pressing: 1-2 sentences explaining why this issue matters RIGHT \
NOW. Combine: how long open + activity level, whether it's a regression, \
what it blocks, how many users are affected. This is editorial judgment — \
make it compelling and specific.
  - cluster_type: "bug", "feature_request", "usage", or "other"
  - regression_from: Release version this regressed from, or null
  - priority: Integer rank (1 = most pressing)
  - categories: Dominant type, models, hardware

RANKING PRIORITY (most to least pressing):
1. Critical bugs blocking roadmap objectives or causing crashes
2. Regressions from recent releases
3. High-engagement bugs (many comments, reopens)
4. Bugs producing wrong output
5. Performance regressions
6. High-demand feature requests
7. Usage/configuration issues

SPECIFICITY GUIDE for main_fix:
  GOOD: "V1 scheduler hard-asserts on token count > max_model_len during \
streaming, killing EngineCore instead of finishing with length"
  GOOD: "CUDA graph deadlock under concurrent load with prefix caching — \
forward pass hangs permanently, GPU at 100% but 0 tokens/s"
  BAD: "Engine crashes under concurrent load"
  BAD: "KV cache memory leaks and improper cleanup"

SPECIFICITY GUIDE for why_pressing:
  GOOD: "Filed 18 days ago with 15 comments. Confirmed on H100 and \
Blackwell by multiple users. Blocks production deployment of prefix \
caching — the workaround (--enforce-eager) has 8x throughput penalty."
  GOOD: "Regression from v0.19.0 — broke MTP speculative decoding for \
Qwen3.5 models. 4 comments in first week. No workaround except pinning \
to v0.18.1."
  BAD: "This is a critical bug."
  BAD: "Many users are affected."

Below are {issue_count} issues with full context:
{issues_block}

Return JSON only — no markdown fences, no commentary:
{{
  "sig_group": "{sig_group}",
  "ranked_issues": [
    {{
      "number": 37729,
      "summary": "V1 engine core deadlocks under concurrent load (fp8 + prefix caching + Qwen3.5)",
      "main_fix": "CUDA graph deadlock in V1 scheduler under sustained concurrent load — forward pass hangs permanently at 0 tokens/s",
      "why_pressing": "Filed 18 days ago with 15 comments. Confirmed on H100 and Blackwell. Blocks production prefix caching — workaround (--enforce-eager) has 8x throughput penalty.",
      "cluster_type": "bug",
      "regression_from": null,
      "priority": 1,
      "categories": {{"type": "Bug", "models": ["Qwen"], "hardware": ["H100"]}}
    }}
  ]
}}
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
    Optionally: comments (count), reopens, comment_bodies (list of dicts with user/body).
    Body is truncated to 10,000 chars; comment text gets up to 5,000 chars.
    """
    lines = []
    for issue in issues:
        number = issue["issue_number"]
        title = issue.get("title", "").replace("\n", " ").strip()
        body = issue.get("body", "").replace("\n", " ").strip()[:10000]
        itype = issue.get("issue_type") or "Other"
        models = issue.get("model_tags") or "General"
        hardware = issue.get("hardware_tags") or "General"
        comments = issue.get("comments", 0)
        reopens = issue.get("reopens", 0)

        # Format comment bodies (up to 3,000 chars total)
        comment_bodies = issue.get("comment_bodies", [])
        comment_text = ""
        if comment_bodies:
            parts = []
            chars_left = 5000
            for cb in comment_bodies:
                user = cb.get("user", "?")
                cbody = cb.get("body", "").replace("\n", " ").strip()
                if not cbody:
                    continue
                entry = f"@{user}: {cbody}"
                if len(entry) > chars_left:
                    entry = entry[:chars_left]
                parts.append(entry)
                chars_left -= len(entry)
                if chars_left <= 0:
                    break
            if parts:
                comment_text = " | COMMENTS: " + " || ".join(parts)

        lines.append(
            f"ISSUE {number} [type: {itype}] [comments: {comments}] [reopens: {reopens}] "
            f"[models: {models}] [hw: {hardware}] | {title} | {body}{comment_text}"
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

ENRICH_SINGLE_ISSUE = """\
You are writing a detailed summary of a single GitHub issue from the vLLM \
project for an engineering roadmap report. You have the FULL issue body \
and ALL comments.

Read everything carefully — the comments often contain root cause analysis, \
workarounds, and diagnostic findings that aren't in the original body.

Write four fields:

**short_title**: A concise, scannable title (under 80 chars). Lead with \
the symptom, include key trigger conditions, drop implementation details. \
Examples:
  GOOD: "V1 engine deadlocks at 0 tokens/s under concurrent FP8 + prefix caching load"
  GOOD: "v0.19.0 regression: page size check breaks MTP spec decoding for Qwen3.5-FP8"
  GOOD: "FlashInfer 0.4 produces wrong outputs on hybrid attention models after 2nd turn"
  GOOD: "LMCache CPU offload generates wrong/repetitive output under high concurrency"
  GOOD: "Prefix caching returns different outputs for identical temperature=0 requests"
  BAD: "V1 scheduler CUDA graph synchronization deadlock when async scheduling + prefix caching + FP8 are enabled - forward pass permanently hangs at 0 tokens/s with 11+ concurrent requests"
  BAD: "KV cache page size calculation changed between v0.18.1 and v0.19.0, now fails divisibility check for Qwen3.5-27B-FP8 with MTP speculative decoding"

**problem**: 2-4 sentences. What is broken and how does it manifest? Be \
specific about the error message, stack trace, affected model/hardware, \
vLLM version, and trigger condition. Translate non-English content to English.

**workaround**: If the comments mention a workaround, state it concisely \
(e.g. "Use --enforce-eager (8x throughput penalty)" or "Downgrade to \
v0.18.1"). If no workaround exists, write "None known".

**likely_solve**: 2-4 sentences. Where should an engineer look to fix this? \
Name the specific file, function, or code path if identifiable from the \
body/comments. Describe what the fix would look like. If the root cause is \
unclear, suggest what to investigate and how.

Be direct and technical. An engineer should read these four fields and \
immediately understand: what it is, what's broken, what to tell users, and \
where to start fixing it.

ISSUE #{issue_number}: {title}

BODY:
{body}

COMMENTS:
{comments}

Return JSON only — no markdown fences, no commentary:
{{
  "issue_number": {issue_number},
  "short_title": "V1 engine deadlocks at 0 tokens/s under concurrent FP8 + prefix caching load",
  "problem": "...",
  "workaround": "...",
  "likely_solve": "..."
}}
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
        body = issue.get("body", "").replace("\n", " ").strip()[:10000]
        comments_raw = issue.get("comments", [])
        if comments_raw:
            comment_strs = []
            for c in comments_raw[:5]:
                cauthor = c.get("author", "?")
                cbody = c.get("body", "").replace("\n", " ").strip()[:1000]
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


