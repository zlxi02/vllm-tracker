"""
LLM-based issue classification pipeline.

Phase 1: discover_taxonomy — sample issues, synthesize patterns, dedup + assign to workstreams
Phase 2: classify_all     — label every issue with workstream + bug_cluster
Phase 3: validate         — spot-check category quality
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
from pathlib import Path

from tqdm import tqdm

from .config import LLMSettings, Settings
from .prompts import (
    WORKSTREAM_LIST_BLOCK,
    WORKSTREAM_THEMES,
    format_issues_block,
)


# ---------------------------------------------------------------------------
# LLM client abstraction
# ---------------------------------------------------------------------------


async def _call_llm(
    settings: LLMSettings,
    prompt: str,
    max_tokens: int = 8192,
    thinking_budget: int | None = None,
    model_override: str | None = None,
) -> str:
    """Call the configured LLM provider and return the raw text response.

    Args:
        thinking_budget: Override settings.thinking_budget. Pass 0 to disable.
        model_override: Use a specific model instead of settings.resolved_model.
    """
    if settings.provider == "anthropic":
        return await _call_anthropic(
            settings, prompt, max_tokens,
            thinking_budget=thinking_budget,
            model_override=model_override,
        )
    elif settings.provider == "openai":
        return await _call_openai(settings, prompt, max_tokens, model_override=model_override)
    else:
        raise ValueError(f"Unknown LLM provider: {settings.provider}")


async def _call_anthropic(
    settings: LLMSettings,
    prompt: str,
    max_tokens: int = 8192,
    thinking_budget: int | None = None,
    model_override: str | None = None,
) -> str:
    import anthropic

    model = model_override or settings.resolved_model
    budget = thinking_budget if thinking_budget is not None else settings.thinking_budget

    client = anthropic.AsyncAnthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if budget and budget > 0:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

    # Use streaming to avoid timeout on large/thinking requests.
    # With thinking enabled, we must skip thinking blocks and only collect text blocks.
    async with client.messages.stream(**kwargs) as stream:
        message = await stream.get_final_message()
    for block in message.content:
        if block.type == "text":
            return block.text
    return message.content[-1].text


async def _call_openai(
    settings: LLMSettings,
    prompt: str,
    max_tokens: int = 8192,
    model_override: str | None = None,
) -> str:
    import openai

    model = model_override or settings.resolved_model
    client = openai.AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _parse_json_response(text: str) -> list | dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try to find JSON array or object
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    print(f"ERROR: Failed to parse JSON from LLM response ({len(text)} chars)")
    print(f"First 500 chars: {text[:500]}")
    print(f"Last 500 chars: {text[-500:]}")
    raise json.JSONDecodeError("No valid JSON found in LLM response", text, 0)


# ---------------------------------------------------------------------------
# Concurrency helper
# ---------------------------------------------------------------------------


async def _run_concurrent(
    settings: LLMSettings,
    prompts: list[str],
    desc: str,
    max_tokens: int = 8192,
    thinking_budget: int | None = None,
    model_override: str | None = None,
) -> list[str]:
    """Run multiple LLM calls with bounded concurrency and a progress bar."""
    semaphore = asyncio.Semaphore(settings.max_concurrent)
    results: list[str | None] = [None] * len(prompts)
    pbar = tqdm(total=len(prompts), desc=desc)

    async def _task(idx: int, prompt: str) -> None:
        async with semaphore:
            results[idx] = await _call_llm(
                settings, prompt, max_tokens=max_tokens,
                thinking_budget=thinking_budget,
                model_override=model_override,
            )
            pbar.update(1)

    await asyncio.gather(*[_task(i, p) for i, p in enumerate(prompts)])
    pbar.close()
    return results  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _sample_issues(
    conn: sqlite3.Connection,
    n_batches: int,
    batch_size: int,
    seed: int = 42,
) -> list[list[dict]]:
    """Pull n_batches * batch_size random issues, split into batches."""
    total_needed = n_batches * batch_size
    rows = conn.execute(
        "SELECT issue_number, title, body FROM issues ORDER BY issue_number"
    ).fetchall()
    rng = random.Random(seed)
    sampled = rng.sample(rows, min(total_needed, len(rows)))
    issues = [
        {
            "issue_number": row["issue_number"],
            "title": row["title"],
            "body": row["body"] or "",
        }
        for row in sampled
    ]
    return [issues[i : i + batch_size] for i in range(0, len(issues), batch_size)]


def _build_issue_lookup(batches: list[list[dict]]) -> dict[int, str]:
    """Map issue_number -> title for all sampled issues."""
    lookup = {}
    for batch in batches:
        for issue in batch:
            lookup[issue["issue_number"]] = issue["title"]
    return lookup


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _ensure_dashboard_columns(conn: sqlite3.Connection) -> None:
    """Add dashboard classification columns if they don't exist."""
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(issues)").fetchall()
    }
    for col in ("issue_type", "sig_group", "model_tags", "hardware_tags"):
        if col not in existing:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {col} TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Dashboard classification (issue_type, sig_group, model_tags, hardware_tags)
# ---------------------------------------------------------------------------


async def dashboard_classify_all(
    conn: sqlite3.Connection,
    settings: Settings,
    force: bool = False,
) -> int:
    """Classify every issue with issue_type, sig_group, model_tags, hardware_tags."""
    from .prompts import (
        DASHBOARD_CLASSIFY_BATCH,
        WORKSTREAM_LIST_BLOCK,
        format_dashboard_issues_block,
    )

    llm = settings.llm
    _ensure_dashboard_columns(conn)

    valid_themes = {ws["name"] for ws in WORKSTREAM_THEMES}
    valid_issue_types = {"Bug", "Feature Request", "Usage/Question", "Model Request", "RFC/Discussion", "Other"}

    # Build a fuzzy lookup: strip parenthetical SIG tags and normalize
    import re
    _sig_fuzzy = {}
    for name in valid_themes:
        _sig_fuzzy[name.lower()] = name
        # Also match with SIG tag suffix like "Core Engine (#sig-core)"
        _sig_fuzzy[name.lower().split("(")[0].strip()] = name

    def _resolve_sig(raw: str | None) -> str | None:
        if not raw:
            return None
        if raw in valid_themes:
            return raw
        # Strip parenthetical SIG tag and try again
        cleaned = re.sub(r'\s*\(#?[\w-]+\)\s*$', '', raw).strip()
        if cleaned in valid_themes:
            return cleaned
        # Case-insensitive fuzzy match
        return _sig_fuzzy.get(cleaned.lower())

    # Load issues — skip already-classified unless force
    where_clause = "" if force else "WHERE sig_group IS NULL"
    rows = conn.execute(
        f"SELECT issue_number, title, body, issue_type FROM issues {where_clause} ORDER BY issue_number"
    ).fetchall()

    issues = [
        {
            "issue_number": row["issue_number"],
            "title": row["title"],
            "body": row["body"] or "",
            "issue_type": row["issue_type"],  # may be None or pre-filled from labels
        }
        for row in rows
    ]

    if not issues:
        print("No issues to classify (all already classified). Use --force to re-classify.")
        return 0

    print(f"Classifying {len(issues)} issues...")

    # Split into batches
    batches = [
        issues[i : i + llm.batch_size]
        for i in range(0, len(issues), llm.batch_size)
    ]

    classify_prompts = [
        DASHBOARD_CLASSIFY_BATCH.format(
            workstream_list=WORKSTREAM_LIST_BLOCK,
            batch_size=len(batch),
            issues_block=format_dashboard_issues_block(batch),
        )
        for batch in batches
    ]

    raw_responses = await _run_concurrent(
        llm, classify_prompts, desc="Dashboard classify",
        model_override=llm.sonnet_model, thinking_budget=0
    )

    classified = 0
    errors = 0
    for resp in raw_responses:
        try:
            results = _parse_json_response(resp)
        except (json.JSONDecodeError, IndexError):
            errors += 1
            continue
        for item in results:
            sig_group = _resolve_sig(item.get("sig_group"))

            # Validate issue_type — reject if LLM returned a SIG name instead
            issue_type_raw = item.get("issue_type")
            if issue_type_raw and issue_type_raw not in valid_issue_types:
                issue_type_raw = "Other"

            model_tags = json.dumps(item.get("model_tags", ["General"]))
            hardware_tags = json.dumps(item.get("hardware_tags", ["General"]))

            # COALESCE preserves label-derived issue_type; LLM only fills NULLs
            conn.execute(
                """
                UPDATE issues
                SET issue_type = COALESCE(issue_type, ?),
                    sig_group = ?,
                    model_tags = ?,
                    hardware_tags = ?
                WHERE issue_number = ?
                """,
                (
                    issue_type_raw,
                    sig_group,
                    model_tags,
                    hardware_tags,
                    item.get("issue_number"),
                ),
            )
            classified += 1
    conn.commit()

    if errors:
        print(f"Warning: {errors} batches had parse errors")
    print(f"Dashboard-classified {classified} issues")
    return classified


# ---------------------------------------------------------------------------
# Dashboard summarization (per-SIG roadmap-style clusters)
# ---------------------------------------------------------------------------

# Maximum issues to send per SIG in one prompt. If a SIG has more, we batch.
_SUMMARIZE_MAX_ISSUES = 100
_SUMMARIZE_ACTIONABLE_TYPES = ("Bug", "Feature Request", "Usage/Question", "Other")
_COMMENT_BODY_CSV = "data/issue_comments_body.csv"
_COMMENT_TOKEN_BUDGET = 5000  # chars budget for comments per issue


def _load_comment_bodies(settings: "Settings", issue_ids: set[str]) -> dict[str, list[dict]]:
    """Load comment bodies from CSV for a set of issue_ids.

    Returns {issue_id: [{"user": user_id, "body": text, "created_at": date}, ...]},
    most recent first, trimmed to fit within _COMMENT_TOKEN_BUDGET chars per issue.
    Also includes "last_activity" — the date of the most recent comment.
    """
    import csv
    import sys

    csv.field_size_limit(sys.maxsize)
    csv_path = settings.root_dir / _COMMENT_BODY_CSV
    if not csv_path.exists():
        print(f"  Warning: {csv_path} not found, skipping comment context")
        return {}

    raw_comments: dict[str, list[dict]] = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iid = row.get("issue_id", "")
            if iid not in issue_ids:
                continue
            body = (row.get("body") or "").strip()
            if not body:
                continue
            raw_comments.setdefault(iid, []).append({
                "user": row.get("user_id", "?"),
                "body": body,
                "created_at": row.get("created_at", ""),
            })

    # For each issue: sort most recent first, fill up to token budget
    comments: dict[str, list[dict]] = {}
    for iid, all_comments in raw_comments.items():
        all_comments.sort(key=lambda c: c["created_at"], reverse=True)

        # Track last activity date
        last_activity = all_comments[0]["created_at"] if all_comments else ""

        # Fill from most recent, respecting char budget
        selected = []
        chars_used = 0
        for c in all_comments:
            entry_len = len(c["body"]) + len(c["user"]) + 10  # overhead for formatting
            if chars_used + entry_len > _COMMENT_TOKEN_BUDGET and selected:
                break  # always include at least one comment
            selected.append(c)
            chars_used += entry_len

        selected.reverse()  # restore chronological order for readability
        # Attach last_activity to the first comment as metadata
        if selected:
            selected[0]["_last_activity"] = last_activity
        comments[iid] = selected

    return comments


def _tiered_select(issues: list[dict], cap: int) -> list[dict]:
    """Select up to `cap` issues using tiered priority sampling.

    Tiers progressively relax engagement and recency filters:
    Tier 1: Top 10% comments + activity within 30d + open 90+ days
    Tier 2: Top 33% comments + activity within 30d + open 45+ days
    Tier 3: Top 10% comments + activity within 30d
    Tier 4: Top 33% comments + activity within 30d
    Tier 5: Activity within 30d
    Tier 6: Everything else by recency
    """
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    forty_five_days_ago = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    ninety_days_ago = (now - timedelta(days=90)).strftime("%Y-%m-%d")

    # Compute comment thresholds
    sorted_by_comments = sorted(issues, key=lambda i: i["comments"], reverse=True)
    top10_idx = max(1, len(issues) // 10)
    top33_idx = max(1, len(issues) // 3)
    top10_threshold = sorted_by_comments[top10_idx - 1]["comments"]
    top33_threshold = sorted_by_comments[top33_idx - 1]["comments"]

    selected: list[dict] = []
    seen: set[int] = set()

    def _add(issue: dict) -> bool:
        if issue["issue_number"] in seen or len(selected) >= cap:
            return False
        seen.add(issue["issue_number"])
        selected.append(issue)
        return True

    def _is_active(iss: dict, cutoff: str) -> bool:
        return iss["updated_at"] >= cutoff

    def _is_long_running(iss: dict, cutoff: str) -> bool:
        return iss["created_at"] < cutoff

    # Tier 1: Top 10% comments + active 30d + open 90+ days
    for iss in issues:
        if len(selected) >= cap:
            break
        if (iss["comments"] >= top10_threshold
                and _is_active(iss, thirty_days_ago)
                and _is_long_running(iss, ninety_days_ago)):
            _add(iss)

    # Tier 2: Top 33% comments + active 30d + open 45+ days
    for iss in issues:
        if len(selected) >= cap:
            break
        if (iss["comments"] >= top33_threshold
                and _is_active(iss, thirty_days_ago)
                and _is_long_running(iss, forty_five_days_ago)):
            _add(iss)

    # Tier 3: Top 10% comments + active 30d
    for iss in issues:
        if len(selected) >= cap:
            break
        if (iss["comments"] >= top10_threshold
                and _is_active(iss, thirty_days_ago)):
            _add(iss)

    # Tier 4: Top 33% comments + active 30d
    for iss in issues:
        if len(selected) >= cap:
            break
        if (iss["comments"] >= top33_threshold
                and _is_active(iss, thirty_days_ago)):
            _add(iss)

    # Tier 5: Any activity within 30d
    for iss in issues:
        if len(selected) >= cap:
            break
        if _is_active(iss, thirty_days_ago):
            _add(iss)

    # Tier 6: Backfill by recency
    for iss in issues:
        if len(selected) >= cap:
            break
        _add(iss)

    return selected


# Current quarter roadmap issue — override with ROADMAP_ISSUE env var
_ROADMAP_ISSUE = os.environ.get("ROADMAP_ISSUE", "32455")


def _fetch_current_roadmap() -> tuple[str, dict[str, str]]:
    """Fetch the current quarter roadmap from GitHub and parse into per-SIG sections.

    Returns (full_roadmap_text, {sig_name: section_text}).
    The sig_name keys are matched against WORKSTREAM_THEMES names.
    """
    import subprocess, re

    body = ""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", _ROADMAP_ISSUE,
             "--repo", "vllm-project/vllm", "--json", "body", "--jq", ".body"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            body = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if not body:
        return _ROADMAP_FALLBACK, {}

    # Parse into per-SIG sections by splitting on ### headers
    sig_sections: dict[str, str] = {}
    # Map roadmap header names to our canonical SIG names
    header_to_sig = {
        "core engine": "Core Engine",
        "large scale serving": "Large Scale Serving",
        "speed of light": "Performance",
        "performance": "Performance",
        "torch compile": "Torch Compile",
        "frontend": "Frontend / API",
        "rl": "RL / Post-training",
        "multimodality": "Multimodality",
        "multi-modality": "Multimodality",
        "quantization": "Quantization / Model Acceleration",
        "speculative decoding": "Quantization / Model Acceleration",
        "model support program": "Model Support",
        "model support": "Model Support",
        "documentation, recipes, blog": "Docs / UX",
        "docs": "Docs / UX",
        "ci, build, and release": "Installation / Build / CI",
        "ci": "Installation / Build / CI",
    }

    sections = re.split(r'^### +', body, flags=re.MULTILINE)
    for section in sections[1:]:  # skip preamble before first ###
        header_line = section.split('\n', 1)[0].strip()
        header_key = header_line.lower()
        sig_name = header_to_sig.get(header_key)
        if not sig_name:
            # Try partial match
            for key, name in header_to_sig.items():
                if key in header_key:
                    sig_name = name
                    break
        if sig_name:
            chunk = "### " + section.strip()
            # Concatenate if multiple roadmap headers map to the same SIG
            if sig_name in sig_sections:
                sig_sections[sig_name] += "\n\n" + chunk
            else:
                sig_sections[sig_name] = chunk

    return body, sig_sections


_ROADMAP_FALLBACK = """\
Q1 2026 Roadmap (issue #32455) — could not fetch from GitHub.

Key objectives by SIG:
- Core Engine: model runner V2 on by default, CPU KV cache production ready, \
attention backend redesign, stable model implementation API, spec decoding (MTP, EAGLE-3).
- Large Scale Serving: GB200 SoTA DeepSeek recipes, FusedMoE refactor, P/D on AMD ROCm, \
Elastic EP beta.
- Performance: performance dashboard, model bash for DSV3.2/K2/gpt-oss/Qwen3/Gemma3, \
profiling tooling.
- Torch Compile: optimization levels by default, vLLM IR migration, Helion integration, \
compile time improvements.
- Frontend / API: structural tag tool parser refactor, Responses API, renderer refactoring.
- RL / Post-training: modular weight sync, reproduction runs with SOTA RL techniques, \
harden external launcher mode.
- Multimodality: streaming inputs, input processing improvements.
- Quantization: native online quantization UX refactor, nvfp4+mxfp4 recipes, \
kernel backend registration.
- Installation / Build / CI: two-week release cadence, automatic test quarantine, \
auto-bisect workflow, CI dashboard.
- Model Support: automation and tracking, model authoring tool/framework, testing pipeline.
- Docs / UX: enhanced recipes, technical blog posts, educational materials.
"""


def _fetch_release_notes(n: int = 5) -> str:
    """Fetch the N most recent vLLM release notes via gh CLI.

    Falls back to a brief summary if gh is not available.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "gh", "api", "repos/vllm-project/vllm/releases",
                "--paginate", "--jq",
                f'.[0:{n}] | .[] | "## " + .tag_name + " (" + (.published_at[:10]) + ")\\n" + .body + "\\n"',
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback if gh CLI not available
    return (
        "v0.19.0 (Apr 1): Gemma 4, zero-bubble async scheduling, MRV2, ViT CUDA graphs, CPU KV offloading.\n"
        "v0.18.0 (Mar 20): gRPC serving, GPU-less render, NGram GPU spec decode, elastic EP M2.\n"
        "v0.17.0 (Mar 7): PyTorch 2.10, FlashAttention 4, MRV2 PP, Qwen3.5, --performance-mode.\n"
        "v0.16.0 (Feb 18): DeepSeek R1 day-one, structured output v2, PP for MoE.\n"
        "v0.15.0 (Feb 4): FlashMLA, torch.compile for DeepSeek, async scheduling."
    )


def _select_and_enrich_issues(
    conn: sqlite3.Connection, settings: Settings, sig: str,
) -> list[dict]:
    """Select and enrich issues for a SIG: filter, tier, attach comments."""
    from .prompts import format_summarize_issues_block  # noqa: F811

    type_placeholders = ",".join("?" * len(_SUMMARIZE_ACTIONABLE_TYPES))
    rows = conn.execute(
        f"""
        SELECT issue_number, issue_id, title, body, issue_type, model_tags, hardware_tags,
               number_of_comments, number_of_times_reopened, created_at, updated_at
        FROM issues
        WHERE sig_group = ? AND state = 'open'
          AND issue_type IN ({type_placeholders})
        ORDER BY created_at DESC
        """,
        (sig, *_SUMMARIZE_ACTIONABLE_TYPES),
    ).fetchall()

    issues = [
        {
            "issue_number": row["issue_number"],
            "issue_id": row["issue_id"] or "",
            "title": row["title"],
            "body": row["body"] or "",
            "issue_type": row["issue_type"],
            "model_tags": row["model_tags"] or "General",
            "hardware_tags": row["hardware_tags"] or "General",
            "comments": row["number_of_comments"] or 0,
            "reopens": row["number_of_times_reopened"] or 0,
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
        }
        for row in rows
    ]

    if not issues:
        return []

    # Tiered selection
    if len(issues) > _SUMMARIZE_MAX_ISSUES:
        issues = _tiered_select(issues, _SUMMARIZE_MAX_ISSUES)
        print(f"  {sig}: {len(rows)} eligible issues, sampled {len(issues)} via tiered selection")
    else:
        print(f"  {sig}: {len(issues)} eligible issues (all included)")

    # Attach comment bodies and last activity date
    selected_issue_ids = {iss["issue_id"] for iss in issues if iss["issue_id"]}
    comment_map = _load_comment_bodies(settings, selected_issue_ids)
    for iss in issues:
        comments = comment_map.get(iss["issue_id"], [])
        iss["comment_bodies"] = comments
        # Extract last_activity from metadata on first comment
        if comments and comments[0].get("_last_activity"):
            iss["last_activity"] = comments[0]["_last_activity"][:10]
        else:
            iss["last_activity"] = iss.get("updated_at", "")[:10]

    return issues


def _sig_filename(sig: str) -> str:
    """Convert SIG name to a safe filename: 'Core Engine' -> 'core_engine'."""
    return sig.lower().replace(" ", "_").replace("/", "_")


def _get_sig_groups(conn: sqlite3.Connection, sig_filter: str | None = None) -> list[str]:
    """Get SIG group names, optionally filtered to one."""
    sig_rows = conn.execute(
        "SELECT DISTINCT sig_group FROM issues WHERE sig_group IS NOT NULL ORDER BY sig_group"
    ).fetchall()
    sig_groups = [row["sig_group"] for row in sig_rows]
    if sig_filter:
        if sig_filter not in sig_groups:
            print(f"SIG '{sig_filter}' not found. Available: {', '.join(sig_groups)}")
            return []
        return [sig_filter]
    return sig_groups


# --- Step 1: Prelims ---

async def dashboard_prelims(
    conn: sqlite3.Connection,
    settings: Settings,
    sig_filter: str | None = None,
) -> dict:
    """Prelims: send batches of 10 issues, pick top 3 from each batch.

    Writes results to build/prelims_results.json.
    """
    from .prompts import PRELIMS_SUMMARIZE, format_summarize_issues_block

    llm = settings.llm
    sig_lookup = {ws["name"]: ws["description"] for ws in WORKSTREAM_THEMES}
    sig_groups = _get_sig_groups(conn, sig_filter)
    if not sig_groups:
        return {"prelims": []}

    release_notes = _fetch_release_notes()
    full_roadmap, sig_roadmap_sections = _fetch_current_roadmap()

    prompts = []
    prompt_sigs = []

    for sig in sig_groups:
        issues = _select_and_enrich_issues(conn, settings, sig)
        if not issues:
            continue

        sig_desc = sig_lookup.get(sig, "")
        roadmap_ctx = sig_roadmap_sections.get(sig, f"No specific roadmap section found for {sig}.")

        # Batches of 10
        batch_size = 10
        batches = [issues[i:i + batch_size] for i in range(0, len(issues), batch_size)]
        for batch in batches:
            prompts.append(
                PRELIMS_SUMMARIZE.format(
                    sig_group=sig,
                    sig_description=sig_desc,
                    issue_count=len(batch),
                    issues_block=format_summarize_issues_block(batch),
                    release_notes=release_notes,
                    roadmap_context=roadmap_ctx,
                )
            )
            prompt_sigs.append(sig)

    print(f"Prelims: {len(prompts)} batches across {len(set(prompt_sigs))} SIGs")

    raw_responses = await _run_concurrent(
        llm, prompts, desc="Prelims",
        max_tokens=16384,
    )

    # Parse and collect top issues per SIG
    sig_top_issues: dict[str, list[dict]] = {}
    errors = 0
    for sig, resp in zip(prompt_sigs, raw_responses):
        try:
            parsed = _parse_json_response(resp)
        except (json.JSONDecodeError, IndexError):
            errors += 1
            print(f"  Warning: failed to parse prelims batch for {sig}")
            continue
        if isinstance(parsed, dict):
            top = parsed.get("top_issues", [])
            sig_top_issues.setdefault(sig, []).extend(top)

    if errors:
        print(f"Warning: {errors} prelims batches had parse errors")

    # Write per-SIG files
    prelims_dir = settings.build_dir / "prelims"
    prelims_dir.mkdir(parents=True, exist_ok=True)

    result = {"prelims": []}
    for sig, top_issues in sig_top_issues.items():
        seen = set()
        deduped = []
        for iss in top_issues:
            if iss["number"] not in seen:
                seen.add(iss["number"])
                deduped.append(iss)
        sig_data = {"sig_group": sig, "top_issues": deduped}
        result["prelims"].append(sig_data)

        sig_path = prelims_dir / f"{_sig_filename(sig)}.json"
        sig_path.write_text(json.dumps(sig_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {sig}: {len(deduped)} top issues → {sig_path.name}")

    print(f"Prelims written to {prelims_dir}/")
    return result


# --- Step 2: Finals ---

async def dashboard_finals(
    conn: sqlite3.Connection,
    settings: Settings,
    sig_filter: str | None = None,
) -> dict:
    """Finals: take top issues from prelims, re-read with full context, rank top 15.

    Reads build/prelims_results.json, writes build/dashboard_summary.json.
    """
    from .prompts import FINALS_RANK, format_summarize_issues_block

    llm = settings.llm
    sig_lookup = {ws["name"]: ws["description"] for ws in WORKSTREAM_THEMES}
    prelims_dir = settings.build_dir / "prelims"

    if not prelims_dir.exists():
        print("Prelims not found. Run `dashboard-prelims` first.")
        return {"sig_summaries": []}

    # Load per-SIG prelims files
    sig_prelims = []
    for f in sorted(prelims_dir.glob("*.json")):
        sig_data = json.loads(f.read_text(encoding="utf-8"))
        sig_prelims.append(sig_data)

    if sig_filter:
        sig_prelims = [s for s in sig_prelims if s["sig_group"] == sig_filter]

    if not sig_prelims:
        print("No prelims files found." + (f" (filter: {sig_filter})" if sig_filter else ""))
        return {"sig_summaries": []}

    release_notes = _fetch_release_notes()
    full_roadmap, sig_roadmap_sections = _fetch_current_roadmap()

    prompts = []
    prompt_sigs = []
    issue_last_activity: dict[int, str] = {}  # issue_number -> last_activity date
    sig_valid_issue_nums: dict[str, set[int]] = {}  # sig -> set of valid issue numbers

    for sig_data in sig_prelims:
        sig = sig_data["sig_group"]
        top_issue_nums = {iss["number"] for iss in sig_data.get("top_issues", [])}
        if not top_issue_nums:
            continue

        # Fetch full issue data + comments for the top issues
        placeholders = ",".join("?" * len(top_issue_nums))
        rows = conn.execute(
            f"""
            SELECT issue_number, issue_id, title, body, issue_type, model_tags, hardware_tags,
                   number_of_comments, number_of_times_reopened, created_at, updated_at
            FROM issues WHERE issue_number IN ({placeholders})
            """,
            list(top_issue_nums),
        ).fetchall()

        issues = [
            {
                "issue_number": row["issue_number"],
                "issue_id": row["issue_id"] or "",
                "title": row["title"],
                "body": row["body"] or "",
                "issue_type": row["issue_type"],
                "model_tags": row["model_tags"] or "General",
                "hardware_tags": row["hardware_tags"] or "General",
                "comments": row["number_of_comments"] or 0,
                "reopens": row["number_of_times_reopened"] or 0,
                "created_at": row["created_at"] or "",
                "updated_at": row["updated_at"] or "",
            }
            for row in rows
        ]

        # Attach comment bodies and last activity date
        issue_ids = {iss["issue_id"] for iss in issues if iss["issue_id"]}
        comment_map = _load_comment_bodies(settings, issue_ids)
        for iss in issues:
            comments = comment_map.get(iss["issue_id"], [])
            iss["comment_bodies"] = comments
            if comments and comments[0].get("_last_activity"):
                iss["last_activity"] = comments[0]["_last_activity"][:10]
            else:
                iss["last_activity"] = iss.get("updated_at", "")[:10]

        sig_desc = sig_lookup.get(sig, "")
        roadmap_ctx = sig_roadmap_sections.get(sig, f"No specific roadmap section found for {sig}.")

        # Track which issue numbers belong to this SIG
        sig_valid_nums = {iss["issue_number"] for iss in issues}
        sig_valid_issue_nums[sig] = sig_valid_nums

        # Build lookup for last_activity per issue number
        for iss in issues:
            issue_last_activity[iss["issue_number"]] = iss.get("last_activity", "")

        prompts.append(
            FINALS_RANK.format(
                sig_group=sig,
                sig_description=sig_desc,
                issue_count=len(issues),
                issues_block=format_summarize_issues_block(issues),
                release_notes=release_notes,
                roadmap_context=roadmap_ctx,
            )
        )
        prompt_sigs.append(sig)
        print(f"  {sig}: {len(issues)} issues for finals ranking")

    print(f"Finals: {len(prompts)} SIG prompts")

    raw_responses = await _run_concurrent(
        llm, prompts, desc="Finals",
        max_tokens=16384,
    )

    finals_dir = settings.build_dir / "finals"
    finals_dir.mkdir(parents=True, exist_ok=True)

    sig_summaries = []
    errors = 0
    for sig, resp in zip(prompt_sigs, raw_responses):
        try:
            parsed = _parse_json_response(resp)
        except (json.JSONDecodeError, IndexError):
            errors += 1
            print(f"  Warning: failed to parse finals for {sig}")
            continue
        if isinstance(parsed, dict):
            ranked = parsed.get("ranked_issues", [])
            if not ranked:
                print(f"  Warning: {sig} returned 0 ranked_issues. Keys in response: {list(parsed.keys())}")
                if len(str(parsed)) < 500:
                    print(f"  Response: {parsed}")
            # Filter out hallucinated issue numbers not in this SIG's input
            valid_nums = sig_valid_issue_nums.get(sig, set())
            if valid_nums:
                before = len(ranked)
                ranked = [r for r in ranked if r.get("number") in valid_nums]
                if len(ranked) < before:
                    print(f"  Warning: {sig} had {before - len(ranked)} hallucinated issue(s) removed")
            ranked.sort(key=lambda c: c.get("priority", 999))
            ranked = ranked[:15]

            # Save per-SIG finals file
            sig_data = {"sig_group": sig, "ranked_issues": ranked}
            sig_path = finals_dir / f"{_sig_filename(sig)}.json"
            sig_path.write_text(json.dumps(sig_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  {sig}: {len(ranked)} ranked issues → {sig_path.name}")

            # Convert to clusters format for dashboard_summary.json compatibility
            # Each ranked issue becomes a singleton cluster
            clusters = []
            for iss in ranked:
                clusters.append({
                    "main_fix": iss.get("main_fix", iss.get("summary", "")),
                    "cluster_type": iss.get("cluster_type", "bug"),
                    "why_pressing": iss.get("why_pressing", ""),
                    "severity": iss.get("severity", "medium"),
                    "regression_from": iss.get("regression_from"),
                    "priority": iss.get("priority", 999),
                    "last_activity": issue_last_activity.get(iss["number"], ""),
                    "issues": [{"number": iss["number"], "summary": iss.get("summary", "")}],
                    "categories": iss.get("categories", {}),
                })
            sig_summaries.append({"sig_group": sig, "clusters": clusters})

    if errors:
        print(f"Warning: {errors} finals had parse errors")

    # Write dashboard_summary.json — merge with existing if running for a single SIG
    output_path = settings.build_dir / "dashboard_summary.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if sig_filter and output_path.exists():
        # Merge: load existing, replace just the filtered SIG(s)
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        existing_summaries = existing.get("sig_summaries", [])
        updated_sigs = {s["sig_group"] for s in sig_summaries}
        # Keep existing SIGs that weren't re-run
        merged = [s for s in existing_summaries if s["sig_group"] not in updated_sigs]
        merged.extend(sig_summaries)
        # Preserve existing executive_summary and ranking
        result = existing
        result["sig_summaries"] = merged
    else:
        result = {"sig_summaries": sig_summaries}

    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Finals written to {finals_dir}/ and {output_path}")
    total_issues = sum(len(s.get("clusters", [])) for s in sig_summaries)
    print(f"  {len(sig_summaries)} SIGs, {total_issues} ranked issues")
    return result


# ---------------------------------------------------------------------------
# Dashboard ranking + executive summary (reads existing summary, adds ranking)
# ---------------------------------------------------------------------------


# Keep _format_clusters_for_prioritize for potential future use
def _format_clusters_for_prioritize(clusters: list[dict]) -> str:
    """Format clusters as input for the prioritization prompt."""
    parts = []
    for c in clusters:
        issues_str = "; ".join(
            f"#{iss['number']} ({iss.get('comments', 0)} comments, {iss.get('reopens', 0)} reopens): {iss.get('summary', '')}"
            for iss in c.get("issues", [])
        )
        cats = c.get("categories", {})
        parts.append(
            f"CLUSTER: {c.get('main_fix', '')}\n"
            f"  Issues ({len(c.get('issues', []))}): {issues_str}\n"
            f"  Type: {cats.get('type', 'Unknown')} | Models: {cats.get('models', [])} | HW: {cats.get('hardware', [])}"
        )
    return "\n\n".join(parts)


async def dashboard_prioritize_all(
    conn: sqlite3.Connection,
    settings: Settings,
    sig_filter: str | None = None,
) -> dict:
    """Pass 2: Re-rank and enrich clusters per SIG using roadmap + release context.

    Reads existing dashboard_summary.json, sends each SIG's clusters through
    a prioritization prompt, and writes back the enriched result.
    """
    from .prompts import PRIORITIZE_SIG_CLUSTERS

    llm = settings.llm
    summary_path = settings.build_dir / "dashboard_summary.json"

    if not summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return {}

    result = json.loads(summary_path.read_text(encoding="utf-8"))
    sig_summaries = result.get("sig_summaries", [])

    if not sig_summaries:
        print("No SIG summaries found.")
        return result

    # Filter to single SIG if requested
    if sig_filter:
        sig_summaries = [s for s in sig_summaries if s["sig_group"] == sig_filter]
        if not sig_summaries:
            available = [s["sig_group"] for s in result.get("sig_summaries", [])]
            print(f"SIG '{sig_filter}' not found. Available: {', '.join(available)}")
            return result

    release_notes = _fetch_release_notes()
    full_roadmap, sig_roadmap_sections = _fetch_current_roadmap()

    sig_lookup = {ws["name"]: ws["description"] for ws in WORKSTREAM_THEMES}

    prompts = []
    prompt_indices = []  # track which sig_summaries index each prompt maps to

    # Build a mapping from sig_group to index in the full result
    full_summaries = result.get("sig_summaries", [])
    sig_to_idx = {s["sig_group"]: i for i, s in enumerate(full_summaries)}

    for sig_data in sig_summaries:
        sig = sig_data["sig_group"]
        clusters = sig_data.get("clusters", [])
        if not clusters:
            continue

        roadmap_ctx = sig_roadmap_sections.get(
            sig, f"No specific roadmap section found for {sig}."
        )
        clusters_block = _format_clusters_for_prioritize(clusters)

        prompts.append(
            PRIORITIZE_SIG_CLUSTERS.format(
                sig_group=sig,
                roadmap_context=roadmap_ctx,
                release_notes=release_notes,
                clusters_block=clusters_block,
            )
        )
        prompt_indices.append(sig_to_idx[sig])

    print(f"Prioritizing {len(prompts)} SIG group{'s' if len(prompts) != 1 else ''}...")

    raw_responses = await _run_concurrent(
        llm, prompts, desc="Dashboard prioritize", max_tokens=16384
    )

    errors = 0
    for idx, resp in zip(prompt_indices, raw_responses):
        try:
            parsed = _parse_json_response(resp)
        except (json.JSONDecodeError, IndexError):
            errors += 1
            print(f"  Warning: failed to parse prioritization for {full_summaries[idx]['sig_group']}")
            continue

        if isinstance(parsed, dict):
            enriched_clusters = parsed.get("clusters", [])
            enriched_clusters.sort(key=lambda c: c.get("priority", 999))
            full_summaries[idx]["clusters"] = enriched_clusters

    if errors:
        print(f"Warning: {errors} SIG groups had parse errors")

    result["sig_summaries"] = full_summaries
    summary_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Updated {summary_path}")
    enriched_count = sum(
        1 for s in full_summaries
        if any(c.get("severity") for c in s.get("clusters", []))
    )
    print(f"  {enriched_count} SIG groups enriched with severity/roadmap_impact/regression_from")
    return result


# ---------------------------------------------------------------------------
# Dashboard ranking + executive summary (reads existing summary, adds ranking)
# ---------------------------------------------------------------------------


async def dashboard_rank_and_summarize(
    conn: sqlite3.Connection,
    settings: Settings,
) -> dict:
    """Rank SIG groups by strategic priority and generate executive summary.

    Reads the existing dashboard_summary.json, calls LLM for ranking,
    and writes back with ranking + executive_summary added.
    """
    from .prompts import RANK_AND_SUMMARIZE_ROADMAP

    llm = settings.llm
    summary_path = settings.build_dir / "dashboard_summary.json"

    if not summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return {}

    result = json.loads(summary_path.read_text(encoding="utf-8"))
    sig_summaries = result.get("sig_summaries", [])

    if not sig_summaries:
        print("No SIG summaries found.")
        return result

    # Get total issue counts per SIG from DB
    sig_issue_counts = {}
    for row in conn.execute(
        "SELECT sig_group, COUNT(*) as cnt FROM issues WHERE sig_group IS NOT NULL GROUP BY sig_group"
    ).fetchall():
        sig_issue_counts[row["sig_group"]] = row["cnt"]

    # Build the SIG summaries block for the ranking prompt
    sig_block_parts = []
    for sig_data in sig_summaries:
        sig = sig_data["sig_group"]
        clusters = sig_data.get("clusters", [])
        total = sig_issue_counts.get(sig, 0)
        top_clusters = [c.get("main_fix", "") for c in clusters[:5]]
        sig_block_parts.append(
            f"SIG: {sig}\n"
            f"  Total issues: {total}\n"
            f"  Clusters: {len(clusters)}\n"
            f"  Top clusters:\n"
            + "\n".join(f"    - {c}" for c in top_clusters)
        )
    sig_summaries_block = "\n\n".join(sig_block_parts)

    print("Ranking SIGs and generating executive summary...")
    full_roadmap, _ = _fetch_current_roadmap()
    rank_prompt = RANK_AND_SUMMARIZE_ROADMAP.format(
        sig_summaries_block=sig_summaries_block,
        roadmap_context=full_roadmap,
    )
    rank_response = await _call_llm(llm, rank_prompt, max_tokens=16384)

    executive_summary = []
    try:
        rank_data = _parse_json_response(rank_response)
        rank_order = {
            item["sig_group"]: item["rank"]
            for item in rank_data.get("ranked_sigs", [])
        }
        rationale_map = {
            item["sig_group"]: item.get("rationale", "")
            for item in rank_data.get("ranked_sigs", [])
        }
        for sig_data in sig_summaries:
            sig_data["rank"] = rank_order.get(sig_data["sig_group"], 999)
            sig_data["rank_rationale"] = rationale_map.get(sig_data["sig_group"], "")
        sig_summaries.sort(key=lambda s: s.get("rank", 999))

        executive_summary = rank_data.get("executive_summary", [])
        print(f"  Ranked {len(rank_order)} SIGs, {len(executive_summary)} summary bullets")
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        print(f"  Warning: failed to parse ranking response: {e}")

    result["sig_summaries"] = sig_summaries
    result["executive_summary"] = executive_summary

    summary_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Updated {summary_path}")
    return result


# ---------------------------------------------------------------------------
# Issue enrichment — per-issue problem/fix summaries
# ---------------------------------------------------------------------------


async def dashboard_enrich_issues(
    conn: sqlite3.Connection,
    settings: Settings,
    force: bool = False,
) -> dict:
    """Generate problem/workaround/likely_solve summaries for issues in the roadmap.

    Reads dashboard_summary.json to find which issue numbers appear in the
    roadmap. Fetches issue details from SQLite, sends batches to LLM,
    and writes enrichments to build/issue_enrichments.json.

    If force=False, skips issues that already have enrichments.
    """
    from .prompts import ENRICH_SINGLE_ISSUE

    llm = settings.llm
    summary_path = settings.build_dir / "dashboard_summary.json"
    enrichments_path = settings.build_dir / "issue_enrichments.json"

    if not summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return {}

    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    # Collect all issue numbers that appear in the roadmap
    roadmap_issue_nums: set[int] = set()
    for sig_data in summary.get("sig_summaries", []):
        for cluster in sig_data.get("clusters", []):
            for iss in cluster.get("issues", []):
                roadmap_issue_nums.add(iss["number"])

    # Load existing enrichments
    existing: dict[int, dict] = {}
    if enrichments_path.exists():
        raw = json.loads(enrichments_path.read_text(encoding="utf-8"))
        existing = {e["issue_number"]: e for e in raw.get("enrichments", [])}

    # Determine which issues need enrichment
    if force:
        todo_nums = roadmap_issue_nums
    else:
        todo_nums = roadmap_issue_nums - set(existing.keys())

    if not todo_nums:
        print(f"All {len(roadmap_issue_nums)} roadmap issues already enriched. Use --force to re-enrich.")
        return {"enrichments": list(existing.values())}

    print(f"Enriching {len(todo_nums)} issues ({len(existing)} already done, {len(roadmap_issue_nums)} total in roadmap)...")

    # Fetch issue details + comments from SQLite
    placeholders = ",".join("?" * len(todo_nums))
    rows = conn.execute(
        f"""
        SELECT issue_number, title, body, state, created_at, creator_login
        FROM issues WHERE issue_number IN ({placeholders})
        """,
        list(todo_nums),
    ).fetchall()

    # Fetch comment bodies from CSV
    issue_id_rows = conn.execute(
        f"SELECT issue_number, issue_id FROM issues WHERE issue_number IN ({placeholders})",
        list(todo_nums),
    ).fetchall()
    num_to_id = {r["issue_number"]: r["issue_id"] for r in issue_id_rows}
    id_to_num = {v: k for k, v in num_to_id.items()}

    comment_map = _load_comment_bodies(settings, set(num_to_id.values()))
    comments_by_num: dict[int, list[dict]] = {}
    for iid, comments_list in comment_map.items():
        num = id_to_num.get(iid)
        if num:
            comments_by_num[num] = [{"author": c["user"], "body": c["body"]} for c in comments_list]

    # Build one prompt per issue with full body + full comments
    prompts = []
    prompt_nums = []
    for row in rows:
        num = row["issue_number"]
        body = row["body"] or ""
        title = row["title"] or ""

        # Format comments
        issue_comments = comments_by_num.get(num, [])
        if issue_comments:
            comments_text = "\n\n".join(
                f"@{c['author']}: {c['body']}" for c in issue_comments if c.get("body")
            )
        else:
            comments_text = "(no comments)"

        prompts.append(ENRICH_SINGLE_ISSUE.format(
            issue_number=num,
            title=title,
            body=body,
            comments=comments_text,
        ))
        prompt_nums.append(num)

    raw_responses = await _run_concurrent(
        llm, prompts, desc="Enrich issues", max_tokens=8192,
        model_override=llm.sonnet_model, thinking_budget=0,
    )

    # Parse responses and merge with existing
    new_count = 0
    for num, resp in zip(prompt_nums, raw_responses):
        try:
            parsed = _parse_json_response(resp)
            if isinstance(parsed, dict):
                existing[num] = {
                    "issue_number": num,
                    "short_title": parsed.get("short_title", ""),
                    "problem": parsed.get("problem", ""),
                    "workaround": parsed.get("workaround", "None known"),
                    "likely_solve": parsed.get("likely_solve", ""),
                }
                new_count += 1
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            print(f"  Warning: failed to parse enrichment for #{num}: {e}")

    # Write enrichments
    output = {"enrichments": list(existing.values())}
    enrichments_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Enriched {new_count} issues. Total: {len(existing)}. Written to {enrichments_path}")
    return output


# ---------------------------------------------------------------------------
# Newsfeed — daily digest generation
# ---------------------------------------------------------------------------


async def generate_newsfeed(
    conn: sqlite3.Connection,
    settings: Settings,
    target_date: str | None = None,
    days: int = 1,
) -> dict:
    """Generate a daily newsfeed digest for issues active on a given date.

    target_date: YYYY-MM-DD string, defaults to today (PST).
    days: how many days to generate (counting backwards from target_date).
    """
    from datetime import datetime, timedelta, timezone
    from .prompts import GENERATE_NEWSFEED, format_newsfeed_issues_block

    llm = settings.llm

    if target_date is None:
        # Default to today in PST
        pst = timezone(timedelta(hours=-7))
        target_date = datetime.now(pst).strftime("%Y-%m-%d")

    release_context = _fetch_release_notes(n=3)

    newsfeed_dir = settings.build_dir / "newsfeed"
    newsfeed_dir.mkdir(parents=True, exist_ok=True)

    all_digests = []

    for day_offset in range(days):
        dt = datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=day_offset)
        date_str = dt.strftime("%Y-%m-%d")
        next_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        date_display = dt.strftime("%A, %B %-d")

        output_path = newsfeed_dir / f"{date_str}.json"

        # Query issues created or updated on this date
        rows = conn.execute(
            """
            SELECT issue_number, title, body, state, issue_type,
                   model_tags, hardware_tags, number_of_comments,
                   created_at, updated_at
            FROM issues
            WHERE (created_at >= ? AND created_at < ?)
               OR (updated_at >= ? AND updated_at < ?
                   AND created_at < ?)
            ORDER BY number_of_comments DESC, created_at DESC
            """,
            (date_str, next_date, date_str, next_date, date_str),
        ).fetchall()

        issues = []
        for r in rows:
            issues.append({
                "issue_number": r["issue_number"],
                "title": r["title"],
                "body": r["body"] or "",
                "state": r["state"] or "open",
                "issue_type": r["issue_type"],
                "model_tags": r["model_tags"] or "General",
                "hardware_tags": r["hardware_tags"] or "General",
                "comments": r["number_of_comments"] or 0,
            })

        if not issues:
            print(f"  {date_str}: No issues found, skipping.")
            continue

        print(f"  {date_str}: {len(issues)} issues found, generating digest...")

        issues_block = format_newsfeed_issues_block(issues[:60])
        prompt = GENERATE_NEWSFEED.format(
            date_display=date_display,
            release_context=release_context,
            issue_count=len(issues),
            issues_block=issues_block,
        )

        raw = await _call_llm(llm, prompt, max_tokens=8192, thinking_budget=0)
        digest = _parse_json_response(raw)
        digest["date"] = date_str
        digest["date_display"] = date_display

        output_path.write_text(
            json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        all_digests.append(digest)
        print(f"  {date_str}: Done — \"{digest.get('headline', '?')}\"")

    # Write index of all available digests
    _rebuild_newsfeed_index(newsfeed_dir)

    return {"digests": all_digests}


def _rebuild_newsfeed_index(newsfeed_dir: Path) -> None:
    """Rebuild newsfeed/index.json from all daily digest files."""
    entries = []
    for f in sorted(newsfeed_dir.glob("2*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "date": data.get("date", f.stem),
                "date_display": data.get("date_display", ""),
                "headline": data.get("headline", ""),
                "stats": data.get("stats", {}),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    index_path = newsfeed_dir / "index.json"
    index_path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Newsfeed index: {len(entries)} days. Written to {index_path}")


def render_newsfeed_html(settings: Settings) -> str:
    """Render newsfeed panel HTML from all daily digest JSON files.

    Returns the inner HTML for the newsfeed panel (day panels + sidebar buttons).
    """
    newsfeed_dir = settings.build_dir / "newsfeed"
    if not newsfeed_dir.exists():
        return ""

    digests = []
    for f in sorted(newsfeed_dir.glob("2*.json"), reverse=True):
        try:
            digests.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError):
            continue

    if not digests:
        return ""

    from datetime import datetime

    # Build sidebar buttons
    sidebar_buttons = []
    for i, d in enumerate(digests):
        date_str = d.get("date", "")
        stats = d.get("stats", {})
        issue_count = stats.get("issues", 0)
        comments = stats.get("comments", 0)
        closed = stats.get("closed", 0)
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        short_date = dt.strftime("%a, %B %-d")
        panel_id = date_str.replace("-", "")
        active = " active" if i == 0 else ""
        today_label = ""
        if i == 0:
            today_label = '<span style="color:var(--primary);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;">Latest</span><br>'
        title_display = d.get("date_display", short_date)
        sidebar_buttons.append(
            f'<button class="nf-date-btn{active}" data-nf="{panel_id}" '
            f'data-title="{title_display}" '
            f'data-issues="{issue_count}" data-comments="{comments}" data-closed="{closed}">'
            f'{today_label}{short_date} <span class="nf-date-count">{issue_count} issues</span></button>'
        )

    # Build day panels
    day_panels = []
    for i, d in enumerate(digests):
        date_str = d.get("date", "")
        panel_id = date_str.replace("-", "")
        active = " active" if i == 0 else ""

        headline = d.get("headline", "Daily Digest")
        opening = d.get("opening", "")
        callout = d.get("callout")
        sections = d.get("sections", [])
        bottom_line = d.get("bottom_line", "")

        callout_html = ""
        if callout:
            callout_html = f'<div class="nf-callout"><strong>Why this matters:</strong> {callout}</div>'

        sections_html = []
        # First section: headline + opening + callout + first section items
        first_section_items = ""
        if sections:
            first = sections[0]
            items_html = "".join(
                f'<div class="nf-item"><span class="nf-emoji">{item.get("emoji", "&#128196;")}</span>'
                f'<div class="nf-item-body"><div class="nf-item-title">{item.get("title_html", "")}</div>'
                f'<div class="nf-item-desc">{item.get("desc", "")}</div></div></div>'
                for item in first.get("items", [])
            )
            first_section_items = items_html

        sections_html.append(
            f'<div class="nf-section">'
            f'<h2>{headline}</h2>'
            f'<p class="nf-lead">{opening}</p>'
            f'{callout_html}'
            f'{first_section_items}'
            f'</div>'
        )

        # Remaining sections
        for section in sections[1:]:
            items_html = "".join(
                f'<div class="nf-item"><span class="nf-emoji">{item.get("emoji", "&#128196;")}</span>'
                f'<div class="nf-item-body"><div class="nf-item-title">{item.get("title_html", "")}</div>'
                f'<div class="nf-item-desc">{item.get("desc", "")}</div></div></div>'
                for item in section.get("items", [])
            )
            sections_html.append(
                f'<div class="nf-section">'
                f'<h2>{section.get("title", "")}</h2>'
                f'{items_html}'
                f'</div>'
            )

        # Bottom line
        sections_html.append(
            f'<div class="nf-section">'
            f'<h2>The Bottom Line</h2>'
            f'<p class="nf-lead" style="margin-bottom:0;">{bottom_line}</p>'
            f'</div>'
        )
        sections_html.append(
            f'<div style="text-align:center;padding:12px;color:var(--text-secondary);font-size:12px;">'
            f'Generated from vLLM GitHub issues &middot; {d.get("date_display", date_str)}</div>'
        )

        day_panels.append(
            f'<div class="nf-day-panel{active}" id="nf-{panel_id}">\n'
            + "\n".join(sections_html)
            + "\n</div>"
        )

    # First digest stats for the topbar defaults
    first = digests[0]
    first_stats = first.get("stats", {})
    first_title = first.get("date_display", "")

    return {
        "sidebar_buttons": "\n".join(sidebar_buttons),
        "day_panels": "\n\n".join(day_panels),
        "default_title": first_title,
        "default_issues": first_stats.get("issues", 0),
        "default_comments": first_stats.get("comments", 0),
        "default_closed": first_stats.get("closed", 0),
    }


# ---------------------------------------------------------------------------
# Convenience runners (sync wrappers)
# ---------------------------------------------------------------------------


def run_dashboard_classify(settings: Settings, force: bool = False) -> int:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_classify_all(conn, settings, force))
    finally:
        conn.close()


def run_dashboard_prelims(settings: Settings, sig_filter: str | None = None) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_prelims(conn, settings, sig_filter))
    finally:
        conn.close()


def run_dashboard_finals(settings: Settings, sig_filter: str | None = None) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_finals(conn, settings, sig_filter))
    finally:
        conn.close()


def run_dashboard_rank(settings: Settings) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_rank_and_summarize(conn, settings))
    finally:
        conn.close()


def run_generate_newsfeed(settings: Settings, target_date: str | None = None, days: int = 1) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(generate_newsfeed(conn, settings, target_date, days))
    finally:
        conn.close()


def run_dashboard_enrich(settings: Settings, force: bool = False) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_enrich_issues(conn, settings, force))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Small-batch test runner
# ---------------------------------------------------------------------------
