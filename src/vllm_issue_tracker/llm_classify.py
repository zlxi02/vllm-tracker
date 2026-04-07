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


async def _call_llm(settings: LLMSettings, prompt: str, max_tokens: int = 8192) -> str:
    """Call the configured LLM provider and return the raw text response."""
    if settings.provider == "anthropic":
        return await _call_anthropic(settings, prompt, max_tokens)
    elif settings.provider == "openai":
        return await _call_openai(settings, prompt, max_tokens)
    else:
        raise ValueError(f"Unknown LLM provider: {settings.provider}")


async def _call_anthropic(settings: LLMSettings, prompt: str, max_tokens: int = 8192) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic()
    message = await client.messages.create(
        model=settings.resolved_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def _call_openai(settings: LLMSettings, prompt: str, max_tokens: int = 8192) -> str:
    import openai

    client = openai.AsyncOpenAI()
    response = await client.chat.completions.create(
        model=settings.resolved_model,
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
) -> list[str]:
    """Run multiple LLM calls with bounded concurrency and a progress bar."""
    semaphore = asyncio.Semaphore(settings.max_concurrent)
    results: list[str | None] = [None] * len(prompts)
    pbar = tqdm(total=len(prompts), desc=desc)

    async def _task(idx: int, prompt: str) -> None:
        async with semaphore:
            results[idx] = await _call_llm(settings, prompt, max_tokens=max_tokens)
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
        llm, classify_prompts, desc="Dashboard classify"
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
_SUMMARIZE_MAX_ISSUES = 200

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


async def dashboard_summarize_all(
    conn: sqlite3.Connection,
    settings: Settings,
    sig_filter: str | None = None,
) -> dict:
    """For each SIG group, cluster issues and generate one-line summaries.

    If sig_filter is set, only summarize that one SIG group (for testing).
    """
    from .prompts import (
        DASHBOARD_SUMMARIZE_SIG,
        format_summarize_issues_block,
    )

    llm = settings.llm

    # Build SIG name -> description lookup
    sig_lookup = {ws["name"]: ws["description"] for ws in WORKSTREAM_THEMES}

    # Get all SIG groups that have classified issues
    sig_rows = conn.execute(
        "SELECT DISTINCT sig_group FROM issues WHERE sig_group IS NOT NULL ORDER BY sig_group"
    ).fetchall()
    sig_groups = [row["sig_group"] for row in sig_rows]

    if sig_filter:
        if sig_filter not in sig_groups:
            print(f"SIG '{sig_filter}' not found. Available: {', '.join(sig_groups)}")
            return {"sig_summaries": []}
        sig_groups = [sig_filter]

    if not sig_groups:
        print("No classified issues found. Run `dashboard-classify` first.")
        return {"sig_summaries": []}

    print(f"Summarizing {len(sig_groups)} SIG group{'s' if len(sig_groups) != 1 else ''}...")

    # Build prompts — one per SIG (or batched if SIG is very large)
    prompts = []
    prompt_sigs = []  # track which SIG each prompt belongs to

    # Fetch release notes and roadmap for context (cached across SIGs)
    release_notes = _fetch_release_notes()
    full_roadmap, sig_roadmap_sections = _fetch_current_roadmap()

    for sig in sig_groups:
        rows = conn.execute(
            """
            SELECT issue_number, title, body, issue_type, model_tags, hardware_tags,
                   number_of_comments, number_of_times_reopened
            FROM issues
            WHERE sig_group = ? AND state = 'open'
            ORDER BY created_at DESC
            """,
            (sig,),
        ).fetchall()

        issues = [
            {
                "issue_number": row["issue_number"],
                "title": row["title"],
                "body": row["body"] or "",
                "issue_type": row["issue_type"],
                "model_tags": row["model_tags"] or "General",
                "hardware_tags": row["hardware_tags"] or "General",
                "comments": row["number_of_comments"] or 0,
                "reopens": row["number_of_times_reopened"] or 0,
            }
            for row in rows
        ]

        if not issues:
            continue

        # If too many issues, take the most recent ones
        if len(issues) > _SUMMARIZE_MAX_ISSUES:
            print(f"  {sig}: {len(issues)} issues, sampling {_SUMMARIZE_MAX_ISSUES} most recent")
            issues = issues[:_SUMMARIZE_MAX_ISSUES]

        sig_desc = sig_lookup.get(sig, "")
        # Use per-SIG roadmap section if available, else note no specific section
        roadmap_ctx = sig_roadmap_sections.get(
            sig, f"No specific roadmap section found for {sig}."
        )
        prompts.append(
            DASHBOARD_SUMMARIZE_SIG.format(
                sig_group=sig,
                sig_description=sig_desc,
                issue_count=len(issues),
                issues_block=format_summarize_issues_block(issues),
                release_notes=release_notes,
                roadmap_context=roadmap_ctx,
            )
        )
        prompt_sigs.append(sig)

    raw_responses = await _run_concurrent(
        llm, prompts, desc="Dashboard summarize"
    )

    # Parse responses
    sig_summaries = []
    errors = 0
    for sig, resp in zip(prompt_sigs, raw_responses):
        try:
            parsed = _parse_json_response(resp)
        except (json.JSONDecodeError, IndexError):
            errors += 1
            print(f"  Warning: failed to parse response for {sig}")
            continue

        # Ensure the sig_group is set
        if isinstance(parsed, dict):
            parsed["sig_group"] = sig
            # Sort clusters by priority
            clusters = parsed.get("clusters", [])
            clusters.sort(key=lambda c: c.get("priority", 999))
            sig_summaries.append(parsed)

    if errors:
        print(f"Warning: {errors} SIG groups had parse errors")

    result = {"sig_summaries": sig_summaries}

    # Write to build/dashboard_summary.json
    output_path = settings.build_dir / "dashboard_summary.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Summary written to {output_path}")
    print(f"  {len(sig_summaries)} SIG groups summarized")
    total_clusters = sum(len(s.get("clusters", [])) for s in sig_summaries)
    print(f"  {total_clusters} total clusters")
    return result


# ---------------------------------------------------------------------------
# Dashboard prioritization (Pass 2: re-rank + enrich clusters per SIG)
# ---------------------------------------------------------------------------


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
    rank_response = await _call_llm(llm, rank_prompt, max_tokens=4096)

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
# Convenience runners (sync wrappers)
# ---------------------------------------------------------------------------


def run_dashboard_classify(settings: Settings, force: bool = False) -> int:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_classify_all(conn, settings, force))
    finally:
        conn.close()


def run_dashboard_summarize(settings: Settings, sig_filter: str | None = None) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_summarize_all(conn, settings, sig_filter))
    finally:
        conn.close()


def run_dashboard_prioritize(settings: Settings, sig_filter: str | None = None) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_prioritize_all(conn, settings, sig_filter))
    finally:
        conn.close()


def run_dashboard_rank(settings: Settings) -> dict:
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        return asyncio.run(dashboard_rank_and_summarize(conn, settings))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Small-batch test runner
# ---------------------------------------------------------------------------
