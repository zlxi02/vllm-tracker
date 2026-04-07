from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .prompts import WORKSTREAM_THEMES

GITHUB_BASE = "https://github.com/vllm-project/vllm/issues/"


def build_roadmap_report(settings: Settings) -> Path:
    """Build a roadmap-style HTML report from dashboard_summary.json."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    if not settings.summary_path.exists():
        raise FileNotFoundError(
            f"Summary not found at {settings.summary_path}. Run `dashboard-summarize` first."
        )

    summary = json.loads(settings.summary_path.read_text(encoding="utf-8"))

    # Enrich issue data from SQLite (title, body preview, url)
    issue_details: dict[int, dict] = {}
    total_issues = 0
    total_classified = 0
    if settings.sqlite_path.exists():
        conn = sqlite3.connect(settings.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            total_issues = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            total_classified = conn.execute(
                "SELECT COUNT(*) FROM issues WHERE sig_group IS NOT NULL"
            ).fetchone()[0]
            # Collect all issue numbers referenced in the summary
            all_nums = set()
            for sig in summary.get("sig_summaries", []):
                for cluster in sig.get("clusters", []):
                    for iss in cluster.get("issues", []):
                        all_nums.add(iss.get("number"))
            if all_nums:
                placeholders = ",".join("?" for _ in all_nums)
                rows = conn.execute(
                    f"SELECT issue_number, title, body, url, created_at FROM issues WHERE issue_number IN ({placeholders})",
                    list(all_nums),
                ).fetchall()
                for row in rows:
                    body = (row["body"] or "")[:500].replace("\n", " ").strip()
                    if len(row["body"] or "") > 500:
                        body += "..."
                    issue_details[row["issue_number"]] = {
                        "title": row["title"] or "",
                        "body_preview": body,
                        "url": row["url"] or f"{GITHUB_BASE}{row['issue_number']}",
                        "created_at": (row["created_at"] or "")[:10],
                    }
        finally:
            conn.close()

    # Load issue enrichments (problem/fix summaries) if available
    enrichments: dict[int, dict] = {}
    enrichments_path = settings.build_dir / "issue_enrichments.json"
    if enrichments_path.exists():
        raw = json.loads(enrichments_path.read_text(encoding="utf-8"))
        enrichments = {e["issue_number"]: e for e in raw.get("enrichments", [])}

    html_text = render_roadmap_html(summary, total_issues, total_classified, issue_details, enrichments)
    settings.roadmap_path.write_text(html_text, encoding="utf-8")
    return settings.roadmap_path


def render_roadmap_html(
    summary: dict, total_issues: int = 0, total_classified: int = 0,
    issue_details: dict[int, dict] | None = None,
    enrichments: dict[int, dict] | None = None,
) -> str:
    if issue_details is None:
        issue_details = {}
    if enrichments is None:
        enrichments = {}
    sig_summaries = summary.get("sig_summaries", [])
    total_clusters = sum(len(s.get("clusters", [])) for s in sig_summaries)

    import re as _re

    def _linkify_issues(text: str) -> str:
        """Convert #12345 references to GitHub links."""
        escaped = html.escape(text)
        return _re.sub(
            r'#(\d{4,})',
            rf'<a href="{GITHUB_BASE}\1" target="_blank">#\1</a>',
            escaped,
        )

    executive_summary = summary.get("executive_summary", [])
    summary_html = ""
    if executive_summary:
        bullet_items = []
        for b in executive_summary:
            if isinstance(b, dict) and "topic" in b:
                topic = html.escape(b["topic"])
                detail = _linkify_issues(b["detail"])
                bullet_items.append(f"<li><strong>{topic}:</strong> {detail}</li>")
            else:
                bullet_items.append(f"<li>{_linkify_issues(str(b))}</li>")
        bullets = "".join(bullet_items)
        summary_html = f"""
    <section class="hero" style="border-left: 4px solid var(--primary);">
      <h2 style="margin-bottom: 8px;">Executive Summary</h2>
      <ul style="margin:0; padding-left:20px; font-size:13px; line-height:1.7;">
        {bullets}
      </ul>
    </section>
    """

    sig_sections = "".join(
        _render_roadmap_sig(sig_data, idx, issue_details, enrichments)
        for idx, sig_data in enumerate(sig_summaries)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>vLLM Issue Roadmap</title>
  <style>
    :root {{
      --bg: #f8f9fa;
      --surface: #ffffff;
      --text: #212529;
      --text-secondary: #6c757d;
      --primary: #0d6efd;
      --primary-light: #e7f1ff;
      --border: #dee2e6;
      --radius: 6px;
      --shadow: 0 1px 3px rgba(0,0,0,0.08);
      --row-hover: #f8f9ff;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      font-size: 14px;
    }}
    .page {{ max-width: 1400px; margin: 0 auto; padding: 16px 20px 40px; }}
    .hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 20px 24px;
      margin-bottom: 16px;
    }}
    .sig-panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 16px 20px;
      margin-bottom: 12px;
    }}
    h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 6px; }}
    h1 span {{ color: var(--primary); }}
    h2 {{ font-size: 15px; font-weight: 700; margin: 0; }}
    .meta {{ color: var(--text-secondary); font-size: 13px; }}
    .tag {{
      display: inline-block;
      padding: 1px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      margin: 1px 2px 1px 0;
    }}
    .tag-type {{ background: #e7f1ff; color: #084298; }}
    .tag-model {{ background: #e2d9f3; color: #432874; }}
    .tag-hw {{ background: #fff3cd; color: #664d03; }}
    .tag-sig {{ background: #d4edda; color: #155724; }}
    .pill {{
      display: inline-block;
      background: var(--primary-light);
      border-radius: 12px;
      padding: 2px 10px;
      margin: 2px 4px 2px 0;
      color: var(--primary);
      font-size: 12px;
      font-weight: 500;
    }}
    .sig-header {{
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
      cursor: pointer; user-select: none;
    }}
    .sig-header h2 {{ flex-shrink: 0; }}
    .sig-chevron {{
      font-size: 11px; color: var(--text-secondary); transition: transform 0.2s;
    }}
    .sig-panel.collapsed .sig-chevron {{ transform: rotate(-90deg); }}
    .sig-panel.collapsed .sig-body {{ display: none; }}
    .cluster-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 13px;
      table-layout: fixed;
    }}
    .cluster-table th {{
      text-align: left;
      padding: 6px 10px;
      border-bottom: 2px solid var(--border);
      color: var(--text-secondary);
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }}
    .cluster-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid #f0f0f0;
      vertical-align: top;
    }}
    .cluster-table tr:hover td {{ background: var(--row-hover); }}
    .cluster-table .col-rank {{ width: 36px; text-align: center; color: var(--text-secondary); }}
    .cluster-table .col-main {{ width: 33%; }}
    .cluster-table .col-issues {{ width: 42%; }}
    .cluster-table .col-tags {{ width: 20%; }}
    .main-fix {{ font-weight: 600; font-size: 13px; }}
    .issue-link {{
      display: block;
      margin: 2px 0;
      font-size: 12px;
      line-height: 1.4;
    }}
    .issue-link {{
      display: block;
      margin: 3px 0;
      font-size: 12px;
      line-height: 1.4;
      padding: 4px 6px;
      border-radius: 4px;
      cursor: pointer;
      transition: background 0.1s;
    }}
    .issue-link:hover {{ background: var(--primary-light); }}
    .issue-link a {{ color: var(--primary); text-decoration: none; font-weight: 500; }}
    .issue-link a:hover {{ text-decoration: underline; }}
    .issue-summary {{ color: var(--text-secondary); }}
    .issue-detail {{
      display: none;
      margin: 4px 0 8px;
      padding: 10px 12px;
      background: #f8f9fa;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      font-size: 12px;
      line-height: 1.5;
    }}
    .issue-detail.open {{ display: block; }}
    .issue-detail-title {{ font-weight: 600; margin-bottom: 6px; }}
    .issue-detail-body {{ color: var(--text-secondary); margin-bottom: 6px; white-space: pre-wrap; word-break: break-word; }}
    .issue-detail-meta {{ font-size: 11px; color: var(--text-secondary); }}
    .issue-enrichment {{ margin: 8px 0; padding: 8px 12px; background: #f8f9fa; border-radius: 4px; border-left: 3px solid var(--primary); }}
    .issue-enrichment-item {{ font-size: 12px; line-height: 1.6; color: var(--text); margin-bottom: 4px; }}
    .issue-enrichment-item:last-child {{ margin-bottom: 0; }}
    .issue-enrichment-item strong {{ color: var(--text); }}
    .issues-more {{
      display: none;
    }}
    .issues-more.open {{ display: block; }}
    .more-issues-btn, .show-more-btn {{
      background: none;
      border: none;
      border-radius: var(--radius);
      padding: 4px 8px;
      font-size: 12px;
      color: var(--text-secondary);
      cursor: pointer;
      font-family: inherit;
      margin-top: 6px;
      transition: background 0.15s;
    }}
    .more-issues-btn:hover, .show-more-btn:hover {{ background: rgba(0,0,0,0.05); }}
    .hidden-rows {{ display: none; }}
    .hidden-rows.open {{ display: table-row-group; }}
    a {{ color: var(--primary); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .footer {{ color: var(--text-secondary); font-size: 12px; margin-top: 20px; text-align: center; }}
    @media (max-width: 900px) {{
      .cluster-table .col-tags {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="page" data-generated="{(datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")}" data-issues="{total_issues}">

    {summary_html}

    {sig_sections}

    <p class="footer">
      Generated by vllm-issue-tracker &middot; Classifications by LLM &middot; Summaries by LLM
    </p>
  </div>

  <script>
    // SIG panel collapse/expand
    document.querySelectorAll('.sig-header').forEach(header => {{
      header.addEventListener('click', () => {{
        header.closest('.sig-panel').classList.toggle('collapsed');
      }});
    }});
    // Show more clusters per SIG
    document.querySelectorAll('.show-more-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const target = document.getElementById(btn.dataset.target);
        if (target) {{
          target.classList.toggle('open');
          btn.textContent = target.classList.contains('open') ? 'Show less' : btn.dataset.label;
        }}
      }});
    }});
    // Show more issues per cluster
    document.querySelectorAll('.more-issues-btn').forEach(btn => {{
      btn.addEventListener('click', (e) => {{
        e.stopPropagation();
        const target = document.getElementById(btn.dataset.target);
        if (target) {{
          target.classList.toggle('open');
          btn.textContent = target.classList.contains('open') ? 'Show less' : btn.dataset.label;
        }}
      }});
    }});
    // Issue detail expand/collapse
    document.querySelectorAll('.issue-link[data-detail]').forEach(link => {{
      link.addEventListener('click', (e) => {{
        if (e.target.closest('a')) return; // let actual links work
        const detail = document.getElementById(link.dataset.detail);
        if (detail) detail.classList.toggle('open');
      }});
    }});
  </script>
</body>
</html>
"""


_cluster_id_counter = 0

def _render_roadmap_sig(sig_data: dict, idx: int, issue_details: dict[int, dict], enrichments: dict[int, dict] | None = None) -> str:
    sig_group = sig_data.get("sig_group", "Unknown")
    clusters = sig_data.get("clusters", [])
    if not clusters:
        return ""

    total_issues = sum(len(c.get("issues", [])) for c in clusters)
    rank = sig_data.get("rank", idx + 1)
    rationale = sig_data.get("rank_rationale", "")
    visible_limit = 5

    visible_clusters = clusters[:visible_limit]
    hidden_clusters = clusters[visible_limit:]

    visible_rows = "".join(
        _render_roadmap_cluster_row(cluster, rank, issue_details, enrichments)
        for rank, cluster in enumerate(visible_clusters, 1)
    )

    hidden_html = ""
    if hidden_clusters:
        hidden_id = f"sig-{idx}-more"
        hidden_rows = "".join(
            _render_roadmap_cluster_row(cluster, rank, issue_details, enrichments)
            for rank, cluster in enumerate(hidden_clusters, visible_limit + 1)
        )
        hidden_html = f"""
          <tbody id="{hidden_id}" class="hidden-rows">
            {hidden_rows}
          </tbody>
        """
        show_label = f"Show {len(hidden_clusters)} more clusters"
        hidden_html += f"""
        <tfoot><tr><td colspan="4" style="text-align:center;padding:8px">
          <button class="show-more-btn" data-target="{hidden_id}" data-label="{show_label}">{show_label}</button>
        </td></tr></tfoot>
        """

    return f"""
    <section class="sig-panel" id="sig-{idx}">
      <div class="sig-header">
        <span class="sig-chevron">&#9662;</span>
        <h2 style="font-weight:800;">#{rank} {html.escape(sig_group)}</h2>
        <span class="pill">{total_issues} issues</span>
        <span class="pill">{len(clusters)} clusters</span>
      </div>
      {'<p class="meta" style="margin:4px 0 0 24px;">' + html.escape(rationale) + '</p>' if rationale else ''}
      <div class="sig-body">
        <table class="cluster-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th class="col-main">Main Issue / Fix</th>
              <th class="col-issues">Issues &amp; Summaries</th>
              <th class="col-tags">Categories</th>
            </tr>
          </thead>
          <tbody>
            {visible_rows}
          </tbody>
          {hidden_html}
        </table>
      </div>
    </section>
    """


def _render_roadmap_cluster_row(cluster: dict, rank: int, issue_details: dict[int, dict], enrichments: dict[int, dict] | None = None) -> str:
    global _cluster_id_counter
    _cluster_id_counter += 1
    cid = f"cl-{_cluster_id_counter}"
    if enrichments is None:
        enrichments = {}

    main_fix = html.escape(cluster.get("main_fix", ""))
    issues = cluster.get("issues", [])
    categories = cluster.get("categories", {})
    visible_issue_limit = 2

    visible_issues = issues[:visible_issue_limit]
    hidden_issues = issues[visible_issue_limit:]

    def _render_issue_item(iss: dict, detail_id: str) -> str:
        num = iss.get("number", 0)
        summary = html.escape(iss.get("summary", ""))
        details = issue_details.get(num, {})
        enrichment = enrichments.get(num, {})
        url = details.get("url", f"{GITHUB_BASE}{num}")
        title = html.escape(details.get("title", ""))
        body_preview = html.escape(details.get("body_preview", ""))
        created = details.get("created_at", "")
        problem = html.escape(enrichment.get("problem", ""))
        suggested_fix = html.escape(enrichment.get("suggested_fix", ""))

        detail_html = ""
        if title or body_preview or problem:
            enrichment_html = ""
            if problem:
                enrichment_html = f"""
                <div class="issue-enrichment">
                  <div class="issue-enrichment-item"><strong>Problem:</strong> {problem}</div>
                  <div class="issue-enrichment-item"><strong>Suggested Fix:</strong> {suggested_fix}</div>
                </div>"""

            detail_html = f"""
            <div class="issue-detail" id="{detail_id}">
              <div class="issue-detail-title">{title}</div>
              {enrichment_html}
              {'<div class="issue-detail-body">' + body_preview + '</div>' if body_preview and not problem else ''}
              <div class="issue-detail-meta">
                {'Created: ' + created + ' &middot; ' if created else ''}
                <a href="{html.escape(url)}" target="_blank">View on GitHub &rarr;</a>
              </div>
            </div>"""

        return f"""
        <span class="issue-link" data-detail="{detail_id}">
          <a href="{html.escape(url)}" target="_blank" onclick="event.stopPropagation()">#{num}</a>
          <span class="issue-summary">{summary}</span>
        </span>
        {detail_html}"""

    visible_html = "\n".join(
        _render_issue_item(iss, f"{cid}-d{i}")
        for i, iss in enumerate(visible_issues)
    )

    hidden_html = ""
    if hidden_issues:
        more_id = f"{cid}-more"
        hidden_items = "\n".join(
            _render_issue_item(iss, f"{cid}-d{i + visible_issue_limit}")
            for i, iss in enumerate(hidden_issues)
        )
        show_label = f"+{len(hidden_issues)} more"
        hidden_html = f"""
        <div id="{more_id}" class="issues-more">{hidden_items}</div>
        <button class="more-issues-btn" data-target="{more_id}" data-label="{show_label}">{show_label}</button>
        """

    issues_cell = f"{visible_html}{hidden_html}"

    # Render category tags
    pills = []
    itype = categories.get("type", "")
    if itype:
        pills.append(f'<span class="tag tag-type">{html.escape(itype)}</span>')
    for m in categories.get("models", [])[:3]:
        if m and m != "General":
            pills.append(f'<span class="tag tag-model">{html.escape(m)}</span>')
    for h in categories.get("hardware", [])[:3]:
        if h and h != "General":
            pills.append(f'<span class="tag tag-hw">{html.escape(h)}</span>')
    tags_html = "\n".join(pills)

    return f"""
    <tr>
      <td class="col-rank">{rank}</td>
      <td class="col-main"><span class="main-fix">{main_fix}</span></td>
      <td class="col-issues">{issues_cell}</td>
      <td class="col-tags">{tags_html}</td>
    </tr>
    """
