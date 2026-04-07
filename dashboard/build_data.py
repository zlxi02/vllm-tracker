#!/usr/bin/env python3
"""Process vLLM issue CSVs into a compact JSON for the dashboard.

Prefers reading from the SQLite database (build/vllm_issue_snapshot.sqlite3)
when it exists and has dashboard classifications (sig_group, model_tags, etc.).
Falls back to CSV + regex classification otherwise.
"""

import csv
import json
import re
import os
import sqlite3
import sys
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "build", "vllm_issue_snapshot.sqlite3")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "dashboard_data.json")
BODIES_DIR = os.path.join(os.path.dirname(__file__), "data", "bodies")
BODIES_CHUNK_SIZE = 500

BODY_PREVIEW_LENGTH = 500
BODY_EXPANDED_LENGTH = 10000  # longer body for expanded view

# Collapse <details> blocks (env dumps) to just their <summary> line
DETAILS_RE = re.compile(r"<details>\s*<summary>(.*?)</summary>[\s\S]*?</details>", re.IGNORECASE)

def collapse_details(text):
    """Replace <details> blocks with a one-line summary to save space."""
    return DETAILS_RE.sub(r"[Collapsed: \1]", text)

# --- Category inference regexes ---

ISSUE_TYPE_PREFIX = re.compile(r"^\[(\w[\w\s/]*)\]", re.IGNORECASE)

ISSUE_TYPE_MAP = {
    "bug": "bug",
    "bug/question": "bug",
    "bug/regression": "bug",
    "bugfix": "bug",
    "bugfix proposal": "bug",
    "fatal bug": "bug",
    "error": "bug",
    "ui_bug": "bug",
    "feature": "feature",
    "feature request": "feature",
    "new feature": "feature",
    "feat": "feature",
    "enhancement": "feature",
    "improvement": "feature",
    "request": "feature",
    "model support request": "feature",
    "model support": "feature",
    "new model": "feature",
    "usage": "usage",
    "question": "usage",
    "questions": "usage",
    "help wanted": "usage",
    "rfc": "rfc",
    "discussion": "rfc",
    "doc": "docs",
    "docs": "docs",
    "documentation": "docs",
    "documentation request": "docs",
    "doc/feature": "docs",
    "performance": "performance",
    "perf": "performance",
    "benchmark": "performance",
    "benchmark script": "performance",
    "ci": "ci/build",
    "ci failure": "ci/build",
    "ci failed": "ci/build",
    "ci/build": "ci/build",
    "build": "ci/build",
    "build/ci bug": "ci/build",
    "test": "ci/build",
    "tests": "ci/build",
    "installation": "install",
    "installation/runtime": "install",
    "docker": "install",
    "docker hub": "install",
    "dependency issue": "install",
    "tracking": "tracking",
    "tracking issue": "tracking",
    "tracking feature": "tracking",
    "tracker": "tracking",
    "issue tracker": "tracking",
    "roadmap": "tracking",
    "refactor": "refactor",
    "chore": "refactor",
    "fix": "bug",
    "draft": "other",
    "wip": "other",
}

HARDWARE_PATTERNS = [
    (re.compile(r"\b(rocm|amd|mi\d{2,3}|radeon)\b", re.I), "AMD"),
    (re.compile(r"\b(cuda|nvidia|a100|h100|h200|l40|rtx|geforce|v100|t4|l4|b200|gb200|blackwell|hopper|ampere)\b", re.I), "NVIDIA"),
    (re.compile(r"\b(tpu|google cloud tpu)\b", re.I), "TPU"),
    (re.compile(r"\b(intel.gpu|xpu|gaudi|habana|arc\s*\w*gpu)\b", re.I), "Intel"),
    (re.compile(r"\b(cpu.only|cpu.backend|--device\s*cpu)\b", re.I), "CPU"),
    (re.compile(r"\b(neuron|aws.neuron|inferentia|trainium)\b", re.I), "Neuron"),
]

MODEL_PATTERNS = [
    (re.compile(r"\b(llama|llama[\s-]?\d)\b", re.I), "Llama"),
    (re.compile(r"\b(deepseek)\b", re.I), "DeepSeek"),
    (re.compile(r"\b(qwen|qwen[\s-]?\d)\b", re.I), "Qwen"),
    (re.compile(r"\b(mixtral|mistral)\b", re.I), "Mistral"),
    (re.compile(r"\b(gemma)\b", re.I), "Gemma"),
    (re.compile(r"\b(phi[\s-]?\d|microsoft.phi)\b", re.I), "Phi"),
    (re.compile(r"\b(gpt[\s-]?\d|chatgpt)\b", re.I), "GPT"),
    (re.compile(r"\b(chatglm|glm)\b", re.I), "GLM"),
    (re.compile(r"\b(falcon)\b", re.I), "Falcon"),
    (re.compile(r"\b(yi[\s-]?\d|01[\s-]?ai)\b", re.I), "Yi"),
    (re.compile(r"\b(internlm)\b", re.I), "InternLM"),
    (re.compile(r"\b(baichuan)\b", re.I), "Baichuan"),
    (re.compile(r"\b(starcoder|bigcode)\b", re.I), "StarCoder"),
    (re.compile(r"\b(command[\s-]?r)\b", re.I), "Command-R"),
    (re.compile(r"\b(cohere)\b", re.I), "Cohere"),
    (re.compile(r"\b(jamba|ai21)\b", re.I), "Jamba"),
    (re.compile(r"\b(mamba)\b", re.I), "Mamba"),
    (re.compile(r"\b(minimax)\b", re.I), "MiniMax"),
    (re.compile(r"\b(nemotron)\b", re.I), "Nemotron"),
    (re.compile(r"\b(granite)\b", re.I), "Granite"),
]

FAILURE_PATTERNS = [
    (re.compile(r"\b(crash|segfault|sigkill|sigsegv|core.dump|abort|fatal)\b", re.I), "Crash"),
    (re.compile(r"\b(oom|out.of.memory|cuda.out.of.memory|memory.error|kv.cache.full)\b", re.I), "OOM"),
    (re.compile(r"\b(slow|latency|throughput|performance|regression|ttft|tpot|tokens.per.second)\b", re.I), "Performance"),
    (re.compile(r"\b(install|pip|build.from.source|setup\.py|cmake|compilation.error)\b", re.I), "Install"),
    (re.compile(r"\b(compile|triton|kernel|torch\.compile|inductor)\b", re.I), "Compile"),
    (re.compile(r"\b(incorrect.output|wrong.output|garbage|hallucin|mismatch|accuracy)\b", re.I), "Incorrect Output"),
    (re.compile(r"\b(hang|deadlock|stuck|freeze|timeout|unresponsive)\b", re.I), "Hang"),
    (re.compile(r"\b(distributed|multi.node|ray|tensor.parallel|pipeline.parallel|nccl)\b", re.I), "Distributed"),
    (re.compile(r"\b(tool.call|function.call|structured.output|json.schema|guided.decoding)\b", re.I), "Tool/Structured"),
]


def infer_issue_type(title):
    m = ISSUE_TYPE_PREFIX.match(title)
    if m:
        raw = m.group(1).strip().lower()
        return ISSUE_TYPE_MAP.get(raw, raw)
    return "other"


def match_patterns(text, patterns):
    # Pad non-ASCII/ASCII boundaries with spaces so \b works near CJK characters
    padded = re.sub(r'(?<=[^\x00-\x7f])(?=[a-zA-Z0-9])|(?<=[a-zA-Z0-9])(?=[^\x00-\x7f])', ' ', text)
    matches = []
    for regex, label in patterns:
        if regex.search(padded):
            if label not in matches:
                matches.append(label)
    return matches


def load_label_colors():
    """Load label name -> hex color mapping from labels.csv."""
    labels_path = os.path.join(DATA_DIR, "labels.csv")
    colors = {}
    if os.path.exists(labels_path):
        with open(labels_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("label", "").strip()
                color = row.get("color", "").strip()
                if name and color:
                    colors[name] = color if color.startswith("#") else f"#{color}"
    return colors


def process_issues():
    issues_path = os.path.join(DATA_DIR, "github_issues.csv")
    if not os.path.exists(issues_path):
        print(f"Error: {issues_path} not found")
        sys.exit(1)

    label_colors = load_label_colors()
    issues = []
    csv.field_size_limit(10 * 1024 * 1024)  # 10MB field limit for large body fields

    with open(issues_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row.get("title", "")
            body = row.get("body", "") or ""
            search_text = f"{title} {body}"

            body_collapsed = collapse_details(body)

            body_preview = body_collapsed[:BODY_PREVIEW_LENGTH].strip()
            if len(body_collapsed) > BODY_PREVIEW_LENGTH:
                body_preview += "..."

            body_expanded = body_collapsed[:BODY_EXPANDED_LENGTH].strip()
            if len(body_collapsed) > BODY_EXPANDED_LENGTH:
                body_expanded += "..."

            # Parse labels into [{name, color}] objects
            raw_labels = row.get("labels", "") or ""
            labels = []
            if raw_labels.strip():
                for lbl in raw_labels.split(","):
                    lbl = lbl.strip()
                    if lbl:
                        labels.append({"n": lbl, "c": label_colors.get(lbl, "#6c757d")})

            closed_at = row.get("closed_at", "") or ""
            reopen_count = int(row.get("number_of_times_reopened", 0) or 0)

            issue_num = int(row.get("issue_number", 0))
            # Compute longevity using updated_at as activity proxy (no comments table in CSV path)
            days_open = round(float(row.get("days_issue_open", 0) or 0), 1)
            state = row.get("state", "")
            updated_at = row.get("updated_at", "")
            try:
                ua_dt = datetime.fromisoformat(updated_at.replace("+00:00", "+00:00"))
                days_since_act = (datetime.now(timezone.utc) - ua_dt).total_seconds() / 86400
            except (ValueError, TypeError):
                days_since_act = days_open
            longevity = compute_longevity(state, days_open, days_since_act)

            issue = {
                "n": issue_num,
                "t": title,
                "bp": body_preview,
                "_bd": body_expanded,  # temporary; stripped before main JSON
                "s": state,
                "ca": row.get("created_at", "")[:10],  # date only
                "ua": updated_at[:10],
                "cla": closed_at[:10] if closed_at else "",
                "url": row.get("url_link", ""),
                "nc": int(row.get("number_of_comments", 0) or 0),
                "cr": row.get("creator_login_name", ""),
                "co": row.get("creator_company", "") or "",
                "do": days_open,
                "as": row.get("assignees", "") or "",
                "lb": labels,
                "ro": reopen_count,
                # Inferred categories
                "ty": infer_issue_type(title),
                "hw": match_patterns(search_text, HARDWARE_PATTERNS),
                "mo": match_patterns(search_text, MODEL_PATTERNS),
                "fm": match_patterns(search_text, FAILURE_PATTERNS),
                "sig": "",  # not available without LLM classification
                "lo": longevity,
            }
            issues.append(issue)

    # Sort by created_at descending (newest first)
    issues.sort(key=lambda x: x["ca"], reverse=True)
    return issues


def build_filter_options(issues):
    """Collect unique values for each filter dimension."""
    types = set()
    hardware = set()
    models = set()
    failure_modes = set()
    sigs = set()

    for iss in issues:
        types.add(iss["ty"])
        for h in iss["hw"]:
            hardware.add(h)
        for m in iss["mo"]:
            models.add(m)
        for fm in iss["fm"]:
            failure_modes.add(fm)
        if iss.get("sig"):
            sigs.add(iss["sig"])

    return {
        "types": sorted(types),
        "hardware": sorted(hardware),
        "models": sorted(models),
        "failure_modes": sorted(failure_modes),
        "sigs": sorted(sigs),
    }


def write_body_chunks(issues):
    """Write chunked body files and return a mapping of issue_number -> chunk_id."""
    os.makedirs(BODIES_DIR, exist_ok=True)
    # Clean old chunks
    for f in os.listdir(BODIES_DIR):
        if f.endswith(".json"):
            os.remove(os.path.join(BODIES_DIR, f))

    # Sort by issue number for deterministic chunking
    by_num = sorted(issues, key=lambda x: x["n"])
    chunk_map = {}  # issue_number -> chunk_id
    total_size = 0
    for i in range(0, len(by_num), BODIES_CHUNK_SIZE):
        chunk_id = i // BODIES_CHUNK_SIZE
        chunk = by_num[i : i + BODIES_CHUNK_SIZE]
        bodies = {}
        for iss in chunk:
            bodies[str(iss["n"])] = iss.get("_bd", "")
            chunk_map[iss["n"]] = chunk_id
        path = os.path.join(BODIES_DIR, f"{chunk_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bodies, f, ensure_ascii=False)
        total_size += os.path.getsize(path)

    num_chunks = (len(by_num) + BODIES_CHUNK_SIZE - 1) // BODIES_CHUNK_SIZE
    print(f"  Written {num_chunks} body chunks ({total_size / (1024*1024):.1f} MB total)")
    return chunk_map


def _sqlite_has_dashboard_data():
    """Check if SQLite DB exists and has dashboard classifications."""
    if not os.path.exists(SQLITE_PATH):
        return False
    try:
        conn = sqlite3.connect(SQLITE_PATH)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "sig_group" not in cols:
            conn.close()
            return False
        count = conn.execute("SELECT COUNT(*) FROM issues WHERE sig_group IS NOT NULL").fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def compute_longevity(state, days_open, days_since_activity):
    """Compute issue longevity label from age and activity gap.

    Returns one of: "New", "Long-running", "Stale", or "" (no label).
    Only open issues get a longevity label.
    """
    if state != "open":
        return ""
    if days_open < 14:
        return "New"
    if days_open >= 90:
        if days_since_activity < 30:
            return "Long-running"
        return "Stale"
    return ""


def process_issues_from_sqlite():
    """Read issues from SQLite with LLM-classified fields."""
    label_colors = load_label_colors()
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row

    # Get last comment date per issue for longevity computation
    last_comment = {}
    for row in conn.execute(
        """
        SELECT i.issue_id, MAX(c.created_at) as last_comment_at
        FROM issues i
        LEFT JOIN issue_comments c ON c.issue_id = i.issue_id
        GROUP BY i.issue_id
        """
    ).fetchall():
        last_comment[row["issue_id"]] = row["last_comment_at"]

    rows = conn.execute(
        """
        SELECT issue_id, issue_number, title, body, state, created_at, updated_at, closed_at,
               url, creator_login, creator_company, labels, number_of_comments,
               days_issue_open, number_of_times_reopened,
               issue_type, sig_group, model_tags, hardware_tags, failure_mode_key
        FROM issues
        ORDER BY created_at DESC
        """
    ).fetchall()
    conn.close()

    issues = []
    for row in rows:
        title = row["title"] or ""
        body = row["body"] or ""
        body_collapsed = collapse_details(body)

        body_preview = body_collapsed[:BODY_PREVIEW_LENGTH].strip()
        if len(body_collapsed) > BODY_PREVIEW_LENGTH:
            body_preview += "..."
        body_expanded = body_collapsed[:BODY_EXPANDED_LENGTH].strip()
        if len(body_collapsed) > BODY_EXPANDED_LENGTH:
            body_expanded += "..."

        # Parse labels
        raw_labels = row["labels"] or ""
        labels = []
        if raw_labels.strip():
            for lbl in raw_labels.split(","):
                lbl = lbl.strip()
                if lbl:
                    labels.append({"n": lbl, "c": label_colors.get(lbl, "#6c757d")})

        closed_at = row["closed_at"] or ""

        # Use LLM-classified fields, with fallback
        issue_type = row["issue_type"] or infer_issue_type(title)
        sig_group = row["sig_group"] or "Uncategorized"

        # model_tags and hardware_tags are JSON arrays in SQLite
        try:
            model_tags = json.loads(row["model_tags"]) if row["model_tags"] else []
        except (json.JSONDecodeError, TypeError):
            model_tags = []
        try:
            hardware_tags = json.loads(row["hardware_tags"]) if row["hardware_tags"] else []
        except (json.JSONDecodeError, TypeError):
            hardware_tags = []

        # Filter out "General" for display — dashboard uses empty array for "no specific"
        if model_tags == ["General"]:
            model_tags = []
        if hardware_tags == ["General"]:
            hardware_tags = []

        # Failure modes: still use regex since LLM doesn't classify these
        search_text = f"{title} {body}"
        failure_modes = match_patterns(search_text, FAILURE_PATTERNS)

        # Compute longevity label from age + last activity
        days_open = row["days_issue_open"] or 0
        last_act = last_comment.get(row["issue_id"]) or row["created_at"] or ""
        try:
            last_act_dt = datetime.fromisoformat(last_act.replace("+00:00", "+00:00"))
            days_since_act = (datetime.now(timezone.utc) - last_act_dt).total_seconds() / 86400
        except (ValueError, TypeError):
            days_since_act = days_open  # fallback: assume no activity since creation
        longevity = compute_longevity(row["state"] or "", days_open, days_since_act)

        issue = {
            "n": row["issue_number"],
            "t": title,
            "bp": body_preview,
            "_bd": body_expanded,
            "s": row["state"] or "",
            "ca": (row["created_at"] or "")[:10],
            "ua": (row["updated_at"] or "")[:10],
            "cla": closed_at[:10] if closed_at else "",
            "url": row["url"] or "",
            "nc": row["number_of_comments"] or 0,
            "cr": row["creator_login"] or "",
            "co": row["creator_company"] or "",
            "do": round(row["days_issue_open"] or 0, 1),
            "as": "",
            "lb": labels,
            "ro": row["number_of_times_reopened"] or 0,
            "ty": issue_type,
            "hw": hardware_tags,
            "mo": model_tags,
            "fm": failure_modes,
            "sig": sig_group,
            "lo": longevity,
        }
        issues.append(issue)

    return issues


def main():
    # Prefer SQLite with dashboard classifications; fall back to CSV
    if _sqlite_has_dashboard_data():
        print("Processing issues from SQLite (dashboard-classified)...")
        issues = process_issues_from_sqlite()
    else:
        print("Processing issues from CSV (regex classification)...")
        issues = process_issues()
    print(f"  Processed {len(issues)} issues")

    filters = build_filter_options(issues)
    print(f"  Types: {filters['types']}")
    print(f"  Hardware: {filters['hardware']}")
    print(f"  Models: {filters['models']}")
    print(f"  Failure modes: {filters['failure_modes']}")
    if filters.get("sigs"):
        print(f"  SIG groups: {filters['sigs']}")

    # Write chunked body files
    chunk_map = write_body_chunks(issues)

    # Strip full bodies from main JSON; add chunk id
    for iss in issues:
        iss.pop("_bd", None)
        iss["bc"] = chunk_map.get(iss["n"], 0)  # body chunk id

    output = {"issues": issues, "filters": filters, "total": len(issues)}

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)
    print(f"  Written to {OUTPUT_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
