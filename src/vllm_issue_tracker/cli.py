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


def command_dashboard_prelims(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    from .llm_classify import run_dashboard_prelims
    run_dashboard_prelims(settings, sig_filter=args.sig)
    return 0


def command_dashboard_finals(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    prelims_dir = settings.build_dir / "prelims"
    if not prelims_dir.exists() or not list(prelims_dir.glob("*.json")):
        print("Prelims not found. Run `dashboard-prelims` first.")
        return 1
    from .llm_classify import run_dashboard_finals
    run_dashboard_finals(settings, sig_filter=args.sig)
    return 0


def command_dashboard_rank(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    summary_path = settings.build_dir / "dashboard_summary.json"
    if not summary_path.exists():
        print("Summary not found. Run `dashboard-finals` first.")
        return 1
    from .llm_classify import run_dashboard_rank
    run_dashboard_rank(settings)
    return 0


def command_dashboard_enrich(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    summary_path = settings.build_dir / "dashboard_summary.json"
    if not summary_path.exists():
        print("Summary not found. Run `dashboard-finals` first.")
        return 1
    from .llm_classify import run_dashboard_enrich
    run_dashboard_enrich(settings, force=args.force)
    return 0


def command_generate_newsfeed(args: argparse.Namespace) -> int:
    _load_dotenv()
    settings = get_settings()
    if not settings.sqlite_path.exists():
        print("Database not found. Run `load` first.")
        return 1
    from .llm_classify import run_generate_newsfeed
    result = run_generate_newsfeed(
        settings,
        target_date=args.date,
        days=args.days,
    )
    digests = result.get("digests", [])
    if not digests:
        print("No digests generated.")
        return 1
    # Rebuild the newsfeed HTML in index.html
    _rebuild_newsfeed_in_dashboard(settings)
    return 0


def _rebuild_newsfeed_in_dashboard(settings: Settings) -> None:
    """Rebuild the newsfeed panel in dashboard/index.html from generated digests."""
    from .llm_classify import render_newsfeed_html

    result = render_newsfeed_html(settings)
    if not result:
        print("No newsfeed data to render.")
        return

    dashboard_html_path = settings.root_dir / "dashboard" / "index.html"
    if not dashboard_html_path.exists():
        print(f"Dashboard not found at {dashboard_html_path}")
        return

    html = dashboard_html_path.read_text(encoding="utf-8")

    import re

    # Replace sidebar buttons
    sidebar_pattern = re.compile(
        r'(<!-- nf-sidebar-buttons-start -->).+?(<!-- nf-sidebar-buttons-end -->)',
        re.DOTALL,
    )
    html = sidebar_pattern.sub(
        rf'\1\n{result["sidebar_buttons"]}\n\2',
        html,
    )

    # Replace day panels
    panels_pattern = re.compile(
        r'(<!-- nf-day-panels-start -->).+?(<!-- nf-day-panels-end -->)',
        re.DOTALL,
    )
    html = panels_pattern.sub(
        rf'\1\n{result["day_panels"]}\n\2',
        html,
    )

    # Update topbar defaults
    html = re.sub(
        r'(<span class="nf-topbar-title" id="nf-topbar-title">)[^<]*(</span>)',
        rf'\g<1>{result["default_title"]}\2',
        html,
    )
    html = re.sub(
        r'(<div class="nf-stat-num" id="nf-stat-issues">)\d*(</div>)',
        rf'\g<1>{result["default_issues"]}\2',
        html,
    )
    html = re.sub(
        r'(<div class="nf-stat-num" id="nf-stat-comments">)\d*(</div>)',
        rf'\g<1>{result["default_comments"]}\2',
        html,
    )
    html = re.sub(
        r'(<div class="nf-stat-num" id="nf-stat-closed">)\d*(</div>)',
        rf'\g<1>{result["default_closed"]}\2',
        html,
    )

    dashboard_html_path.write_text(html, encoding="utf-8")
    print(f"Newsfeed updated in {dashboard_html_path}")


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

    # Step 3: Prelims (batch issues, pick top 3 per batch)
    print("=" * 60)
    print("Step 3/6: Prelims — selecting top issues per batch...")
    from .llm_classify import run_dashboard_prelims
    run_dashboard_prelims(settings)

    # Step 4: Finals (rank top issues with full context, write summary)
    print("=" * 60)
    print("Step 4/6: Finals — ranking top issues...")
    from .llm_classify import run_dashboard_finals
    run_dashboard_finals(settings)

    # Step 5: Rank (executive summary + SIG ranking)
    print("=" * 60)
    print("Step 5/6: Ranking SIGs and executive summary...")
    from .llm_classify import run_dashboard_rank
    run_dashboard_rank(settings)

    # Step 6: Build HTML
    print("=" * 60)
    print("Step 6/6: Building roadmap report...")
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
        print("Summary not found. Run `dashboard-finals` first.")
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

    prelims_parser = subparsers.add_parser(
        "dashboard-prelims",
        help="Prelims: batch issues into groups of 10, pick top 3 per batch",
    )
    prelims_parser.add_argument(
        "--sig", type=str, default=None,
        help="Run prelims for only this SIG group (e.g. 'Core Engine')",
    )
    finals_parser = subparsers.add_parser(
        "dashboard-finals",
        help="Finals: rank top issues from prelims with full context, produce top 15 clusters",
    )
    finals_parser.add_argument(
        "--sig", type=str, default=None,
        help="Run finals for only this SIG group (e.g. 'Core Engine')",
    )
    subparsers.add_parser(
        "dashboard-rank",
        help="Rank SIGs by priority and generate executive summary",
    )
    enrich_parser = subparsers.add_parser(
        "dashboard-enrich",
        help="Generate problem/fix summaries for roadmap issues",
    )
    enrich_parser.add_argument(
        "--force", action="store_true", help="Re-enrich already-enriched issues"
    )
    subparsers.add_parser(
        "build-roadmap",
        help="Render roadmap HTML from dashboard summary",
    )

    nf_parser = subparsers.add_parser(
        "generate-newsfeed",
        help="Generate daily issue digest (default: today in PST)",
    )
    nf_parser.add_argument(
        "--date", type=str, default=None,
        help="Target date YYYY-MM-DD (default: today PST)",
    )
    nf_parser.add_argument(
        "--days", type=int, default=1,
        help="Number of days to generate counting back from --date (default: 1)",
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
        "dashboard-prelims": lambda: command_dashboard_prelims(args),
        "dashboard-finals": lambda: command_dashboard_finals(args),
        "dashboard-rank": lambda: command_dashboard_rank(args),
        "dashboard-enrich": lambda: command_dashboard_enrich(args),
        "build-roadmap": lambda: command_build_roadmap(),
        "generate-newsfeed": lambda: command_generate_newsfeed(args),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

