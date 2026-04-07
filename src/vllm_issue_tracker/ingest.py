from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .classify import (
    classify_failure_mode_with_fallback,
    classify_roadmap_tag_with_fallback,
    extract_tag,
    normalize_text,
    summarize_issue,
)
from .config import Settings


ISSUES_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    issue_number INTEGER,
    url TEXT,
    title TEXT,
    body TEXT,
    state TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    creator_login TEXT,
    creator_name TEXT,
    creator_company TEXT,
    labels TEXT,
    number_of_comments INTEGER,
    days_issue_open REAL,
    number_of_times_reopened INTEGER,
    repository TEXT,
    model_tag TEXT,
    hardware_tag TEXT,
    failure_mode_key TEXT,
    failure_mode_label TEXT,
    roadmap_tag TEXT,
    summary_text TEXT,
    issue_type TEXT,
    sig_group TEXT,
    model_tags TEXT,
    hardware_tags TEXT
);
"""

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    login TEXT,
    type TEXT,
    site_admin TEXT,
    name TEXT,
    company TEXT,
    blog TEXT,
    location TEXT,
    hireable TEXT,
    bio TEXT,
    created_at TEXT,
    updated_at TEXT,
    fivetran_synced TEXT
);
"""

COMMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_comments (
    issue_comment_id TEXT PRIMARY KEY,
    issue_id TEXT,
    user_id TEXT,
    created_at TEXT
);
"""

METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class LoadStats:
    total_rows: int = 0
    inserted_rows: int = 0
    updated_rows: int = 0
    unchanged_rows: int = 0
    skipped_prs: int = 0
    duplicate_issue_ids: int = 0
    missing_required: int = 0
    incremental: bool = False


def ensure_directories(settings: Settings) -> None:
    settings.build_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)


def get_connection(sqlite_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "\n".join(
            [
                "DROP TABLE IF EXISTS issues;",
                "DROP TABLE IF EXISTS users;",
                "DROP TABLE IF EXISTS issue_comments;",
                "DROP TABLE IF EXISTS pipeline_metadata;",
                ISSUES_SCHEMA,
                USERS_SCHEMA,
                COMMENTS_SCHEMA,
                METADATA_SCHEMA,
            ]
        )
    )
    conn.commit()


def parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    return value


def parse_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def parse_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


LABEL_TO_ISSUE_TYPE = {
    "bug": "Bug",
    "feature request": "Feature Request",
    "documentation": "Usage/Question",
    "usage": "Usage/Question",
    "new-model": "Model Request",
    "rfc": "RFC/Discussion",
}


def parse_issue_type_from_labels(labels_str: str) -> str | None:
    """Extract issue type from comma-separated GitHub labels.

    Returns None if no type label is found (LLM fallback needed).
    """
    for label in labels_str.split(","):
        label_clean = label.strip().lower()
        if label_clean in LABEL_TO_ISSUE_TYPE:
            return LABEL_TO_ISSUE_TYPE[label_clean]
    return None


def _parse_csv_row(row: dict) -> tuple | None:
    """Parse a CSV row into a tuple ready for insert. Returns None if invalid."""
    issue_id = (row.get("issue_id") or "").strip()
    title = (row.get("title") or "").strip()
    issue_number = (row.get("issue_number") or "").strip()
    url = (row.get("url_link") or "").strip()
    created_at = parse_datetime(row.get("created_at"))
    updated_at = parse_datetime(row.get("updated_at"))

    if not issue_id or not title or not issue_number or not url or not created_at or not updated_at:
        return None

    body = row.get("body") or ""
    labels_raw = (row.get("labels") or "").strip()
    title_text = normalize_text(title)
    text = normalize_text(title, body[:1000])
    model_tag = extract_tag(text, "model")
    hardware_tag = extract_tag(text, "hardware")
    failure_mode_key, failure_mode_label = classify_failure_mode_with_fallback(title_text, text)
    roadmap_tag = classify_roadmap_tag_with_fallback(title_text, text)
    summary_text = summarize_issue(title, failure_mode_label, model_tag, hardware_tag)
    issue_type = parse_issue_type_from_labels(labels_raw)

    return (
        issue_id,
        parse_int(issue_number),
        url,
        title,
        body,
        (row.get("state") or "").strip(),
        created_at,
        updated_at,
        parse_datetime(row.get("closed_at")),
        (row.get("creator_login_name") or "").strip(),
        (row.get("creator_name") or "").strip(),
        (row.get("creator_company") or "").strip(),
        labels_raw,
        parse_int(row.get("number_of_comments")),
        parse_float(row.get("days_issue_open")),
        parse_int(row.get("number_of_times_reopened")),
        (row.get("repository") or "").strip(),
        model_tag,
        hardware_tag,
        failure_mode_key,
        failure_mode_label,
        roadmap_tag,
        summary_text,
        issue_type,
    )


_INSERT_COLS = (
    "issue_id, issue_number, url, title, body, state, created_at, updated_at, closed_at, "
    "creator_login, creator_name, creator_company, labels, number_of_comments, "
    "days_issue_open, number_of_times_reopened, repository, model_tag, hardware_tag, "
    "failure_mode_key, failure_mode_label, roadmap_tag, summary_text, issue_type"
)

_INSERT_SQL = f"INSERT INTO issues ({_INSERT_COLS}) VALUES ({', '.join('?' * 24)})"

# Update everything EXCEPT LLM classification columns (sig_group, model_tags, hardware_tags)
# so incremental loads don't wipe out classification work.
_UPDATE_SQL = """
    UPDATE issues SET
        issue_number=?, url=?, title=?, body=?, state=?, created_at=?, updated_at=?,
        closed_at=?, creator_login=?, creator_name=?, creator_company=?, labels=?,
        number_of_comments=?, days_issue_open=?, number_of_times_reopened=?, repository=?,
        model_tag=?, hardware_tag=?, failure_mode_key=?, failure_mode_label=?,
        roadmap_tag=?, summary_text=?, issue_type=?,
        sig_group=NULL, model_tags=NULL, hardware_tags=NULL
    WHERE issue_id=?
"""


def load_issues(settings: Settings, conn: sqlite3.Connection, incremental: bool = False) -> LoadStats:
    stats = LoadStats(incremental=incremental)
    seen_issue_ids: set[str] = set()

    # In incremental mode, build a lookup of existing issue_id -> updated_at
    existing: dict[str, str] = {}
    if incremental:
        rows = conn.execute("SELECT issue_id, updated_at FROM issues").fetchall()
        existing = {r["issue_id"]: r["updated_at"] or "" for r in rows}

    insert_batch: list[tuple] = []
    update_batch: list[tuple] = []

    with settings.issues_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            stats.total_rows += 1
            if row.get("is_pull_request") == "True":
                stats.skipped_prs += 1
                continue

            parsed = _parse_csv_row(row)
            if parsed is None:
                stats.missing_required += 1
                continue

            issue_id = parsed[0]
            if issue_id in seen_issue_ids:
                stats.duplicate_issue_ids += 1
                continue
            seen_issue_ids.add(issue_id)

            if incremental and issue_id in existing:
                csv_updated_at = parsed[7]  # updated_at is index 7
                if csv_updated_at == existing[issue_id]:
                    stats.unchanged_rows += 1
                    continue
                # Issue was updated — re-insert with cleared LLM columns
                # update tuple: all fields except issue_id, then issue_id at the end for WHERE
                update_batch.append(parsed[1:] + (issue_id,))
                stats.updated_rows += 1
            else:
                insert_batch.append(parsed)
                stats.inserted_rows += 1

            if len(insert_batch) >= 1000:
                conn.executemany(_INSERT_SQL, insert_batch)
                insert_batch.clear()
            if len(update_batch) >= 1000:
                conn.executemany(_UPDATE_SQL, update_batch)
                update_batch.clear()

    if insert_batch:
        conn.executemany(_INSERT_SQL, insert_batch)
    if update_batch:
        conn.executemany(_UPDATE_SQL, update_batch)

    _upsert_metadata(conn, "loaded_at", datetime.now(timezone.utc).isoformat())
    _upsert_metadata(conn, "source_issues_csv", str(settings.issues_csv))
    conn.commit()
    return stats


def load_optional_users(settings: Settings, conn: sqlite3.Connection) -> int:
    if not settings.users_csv.exists():
        return 0
    with settings.users_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [
            (
                row.get("id", ""),
                row.get("login", ""),
                row.get("type", ""),
                row.get("site_admin", ""),
                row.get("name", ""),
                row.get("company", ""),
                row.get("blog", ""),
                row.get("location", ""),
                row.get("hireable", ""),
                row.get("bio", ""),
                row.get("created_at", ""),
                row.get("updated_at", ""),
                row.get("_fivetran_synced", ""),
            )
            for row in reader
        ]
    conn.executemany(
        """
        INSERT INTO users (
            id, login, type, site_admin, name, company, blog, location,
            hireable, bio, created_at, updated_at, fivetran_synced
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_optional_comments(settings: Settings, conn: sqlite3.Connection) -> int:
    if not settings.comments_csv.exists():
        return 0
    with settings.comments_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [
            (
                row.get("issue_comment_id", ""),
                row.get("issue_id", ""),
                row.get("user_id", ""),
                row.get("created_at", ""),
            )
            for row in reader
        ]
    conn.executemany(
        "INSERT INTO issue_comments (issue_comment_id, issue_id, user_id, created_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _upsert_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_metadata (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def refresh_database(settings: Settings, incremental: bool = False) -> LoadStats:
    """Load CSV data into SQLite.

    If incremental=True and the database already exists, only insert new issues
    and update issues whose updated_at has changed. LLM classification columns
    (sig_group, model_tags, hardware_tags) are cleared on updated issues so
    dashboard-classify picks them up on the next run.

    If incremental=False (default) or the database doesn't exist, drops all
    tables and reloads from scratch.
    """
    ensure_directories(settings)
    conn = get_connection(settings.sqlite_path)
    try:
        if incremental and settings.sqlite_path.exists():
            # Ensure schema is up to date without dropping
            conn.executescript("\n".join([ISSUES_SCHEMA, USERS_SCHEMA, COMMENTS_SCHEMA, METADATA_SCHEMA]))
            conn.commit()
        else:
            initialize_database(conn)
        stats = load_issues(settings, conn, incremental=incremental)
        if not incremental:
            load_optional_users(settings, conn)
            load_optional_comments(settings, conn)
        return stats
    finally:
        conn.close()
