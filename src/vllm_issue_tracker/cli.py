from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .config import get_settings
from .ingest import ensure_directories, get_connection, refresh_database
from .report import build_roadmap_report


def _check_full_wipe(settings: Settings) -> bool:
    """Warn before dropping a DB that has classified issues. Returns True to proceed."""
    if not settings.sqlite_path.exists():
        return True
    conn = get_connection(settings.sqlite_path)
    try:
        classified = conn.execute(
            "SELECT COUNT(*) FROM issues WHERE sig_group IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        return True
    finally:
        conn.close()
    if classified == 0:
        return True
    print(f"\n⚠️  WARNING: The database has {classified} classified issues.")
    print("A full reload will DROP all tables and wipe LLM classifications.")
    print(f"Re-classifying will cost ~${classified * 0.0006:.2f} on Sonnet.\n")
    answer = input("Proceed with full wipe? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def command_load(args: argparse.Namespace) -> int:
    settings = get_settings()
    # Default to incremental when DB exists, unless --full is passed
    force_full = getattr(args, "full", False)
    incremental = not force_full and settings.sqlite_path.exists()

    if not incremental and not _check_full_wipe(settings):
        print("Aborted.")
        return 1

    stats = refresh_database(settings, incremental=incremental)
    mode = "incremental" if stats.incremental else "full"
    print(
        f"Loaded issues ({mode}):",
        f"total_csv_rows={stats.total_rows}",
        f"inserted={stats.inserted_rows}",
        f"updated={stats.updated_rows}",
        f"unchanged={stats.unchanged_rows}",
        f"skipped_prs={stats.skipped_prs}",
        f"duplicates={stats.duplicate_issue_ids}",
        f"missing_required={stats.missing_required}",
    )
    return 0


def command_classify() -> int:
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        issue_count = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    finally:
        conn.close()
    print(f"Classification is computed during load. {issue_count} issues available.")
    return 0



def command_quality_check() -> int:
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    conn = get_connection(settings.sqlite_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        missing_title = conn.execute("SELECT COUNT(*) FROM issues WHERE TRIM(title) = ''").fetchone()[0]
        duplicate_ids = conn.execute(
            "SELECT COUNT(*) FROM (SELECT issue_id FROM issues GROUP BY issue_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        pr_rows = conn.execute("SELECT COUNT(*) FROM issues WHERE title LIKE '%%[PR]%%'").fetchone()[0]
    finally:
        conn.close()
    print(
        "Quality checks:",
        f"total={total}",
        f"missing_title={missing_title}",
        f"duplicate_issue_ids={duplicate_ids}",
        f"pr_like_titles={pr_rows}",
    )
    return 0


def _load_dotenv() -> None:
    """Load .env file if present (best-effort, no dependency required)."""
    from dotenv import load_dotenv
    load_dotenv()



def command_dashboard_classify(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    from .llm_classify import run_dashboard_classify
    run_dashboard_classify(settings, force=args.force)
    return 0


def command_dashboard_summarize(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    from .llm_classify import run_dashboard_summarize
    run_dashboard_summarize(settings, sig_filter=args.sig)
    return 0


def command_dashboard_prioritize(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    summary_path = settings.build_dir / "dashboard_summary.json"
    if not summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return 1
    from .llm_classify import run_dashboard_prioritize
    run_dashboard_prioritize(settings, sig_filter=args.sig)
    return 0


def command_dashboard_rank(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    summary_path = settings.build_dir / "dashboard_summary.json"
    if not summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return 1
    from .llm_classify import run_dashboard_rank
    run_dashboard_rank(settings)
    return 0


def command_refresh(args: argparse.Namespace) -> int:
    """Run the full pipeline: load → classify → summarize → rank → build-roadmap.

    With --incremental (default when DB exists), only processes new/updated issues.
    With --full, drops the DB and reprocesses everything.
    """
    _load_dotenv()
    settings = get_settings()
    incremental = not args.full and settings.sqlite_path.exists()

    if not incremental and not _check_full_wipe(settings):
        print("Aborted.")
        return 1

    # Step 1: Load CSV
    print("=" * 60)
    print(f"Step 1/5: Loading CSV ({'incremental' if incremental else 'full'})...")
    stats = refresh_database(settings, incremental=incremental)
    new_issues = stats.inserted_rows + stats.updated_rows
    print(f"  {stats.inserted_rows} new, {stats.updated_rows} updated, {stats.unchanged_rows} unchanged")

    if incremental and new_issues == 0:
        print("\nNo new or updated issues. Skipping pipeline.")
        return 0

    # Step 2: Classify (only unclassified issues)
    print("=" * 60)
    print("Step 2/5: Classifying issues...")
    from .llm_classify import run_dashboard_classify
    run_dashboard_classify(settings, force=False)

    # Step 3: Summarize (all SIGs — could optimize to changed-only later)
    print("=" * 60)
    print("Step 3/5: Summarizing SIGs...")
    from .llm_classify import run_dashboard_summarize
    run_dashboard_summarize(settings)

    # Step 4: Rank
    print("=" * 60)
    print("Step 4/5: Ranking and executive summary...")
    from .llm_classify import run_dashboard_rank
    run_dashboard_rank(settings)

    # Step 5: Build HTML
    print("=" * 60)
    print("Step 5/5: Building roadmap report...")
    path = build_roadmap_report(settings)
    dashboard_report = settings.root_dir / "dashboard" / "report.html"
    if dashboard_report.parent.exists():
        import shutil
        shutil.copy2(path, dashboard_report)

    print("=" * 60)
    print(f"Refresh complete: {path}")
    return 0


def command_build_roadmap() -> int:
    settings = get_settings()
    if not settings.summary_path.exists():
        print("Summary not found. Run `dashboard-summarize` first.")
        return 1
    path = build_roadmap_report(settings)
    print(f"Roadmap report written to {path}")
    # Also copy to dashboard/report.html for the dashboard's roadmap tab
    dashboard_report = settings.root_dir / "dashboard" / "report.html"
    if dashboard_report.parent.exists():
        import shutil
        shutil.copy2(path, dashboard_report)
        print(f"Copied to {dashboard_report}")
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vLLM issue snapshot pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="Load CSV into SQLite (incremental by default)")
    load_parser.add_argument(
        "--full", action="store_true",
        help="Drop DB and reload everything (will prompt if classifications exist)",
    )
    subparsers.add_parser("classify", help="Show classification status")
    subparsers.add_parser("quality-check", help="Run data quality checks")

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Full pipeline: load → classify → summarize → rank → build-roadmap",
    )
    refresh_parser.add_argument(
        "--full", action="store_true",
        help="Force full reload (default: incremental when DB exists)",
    )

    dash_parser = subparsers.add_parser(
        "dashboard-classify",
        help="Classify issues for dashboard (type, SIG, models, hardware)",
    )
    dash_parser.add_argument(
        "--force", action="store_true", help="Re-classify already-classified issues"
    )

    sum_parser = subparsers.add_parser(
        "dashboard-summarize",
        help="Generate per-SIG roadmap summaries from classified issues",
    )
    sum_parser.add_argument(
        "--sig", type=str, default=None,
        help="Summarize only this SIG group (e.g. 'Core Engine')",
    )
    pri_parser = subparsers.add_parser(
        "dashboard-prioritize",
        help="Pass 2: Re-rank and enrich clusters with severity/roadmap impact/regression flags",
    )
    pri_parser.add_argument(
        "--sig", type=str, default=None,
        help="Prioritize only this SIG group (e.g. 'Core Engine')",
    )
    subparsers.add_parser(
        "dashboard-rank",
        help="Rank SIGs by priority and generate executive summary",
    )
    subparsers.add_parser(
        "build-roadmap",
        help="Render roadmap HTML from dashboard summary",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "load": lambda: command_load(args),
        "classify": lambda: command_classify(),
        "quality-check": lambda: command_quality_check(),
        "refresh": lambda: command_refresh(args),
        "dashboard-classify": lambda: command_dashboard_classify(args),
        "dashboard-summarize": lambda: command_dashboard_summarize(args),
        "dashboard-prioritize": lambda: command_dashboard_prioritize(args),
        "dashboard-rank": lambda: command_dashboard_rank(args),
        "build-roadmap": lambda: command_build_roadmap(),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

