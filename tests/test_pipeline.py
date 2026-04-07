from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from vllm_issue_tracker.classify import classify_failure_mode, classify_roadmap_tag, extract_tag
from vllm_issue_tracker.config import get_settings
from vllm_issue_tracker.ingest import (
    parse_issue_type_from_labels,
    refresh_database,
)
from vllm_issue_tracker.report import build_roadmap_report, render_roadmap_html


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data_dir = root / "data"
        preview_dir = data_dir / "preview"
        data_dir.mkdir(parents=True)
        preview_dir.mkdir(parents=True)

        source_root = Path(__file__).resolve().parents[1]
        (data_dir / "github_issues.csv").write_text(
            (source_root / "data" / "preview" / "github_issues_preview.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (data_dir / "users.csv").write_text(
            (source_root / "data" / "preview" / "users_preview.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (data_dir / "issue_comments.csv").write_text(
            (source_root / "data" / "preview" / "issue_comments_preview.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.settings = get_settings(root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_refresh_database_loads_issues(self) -> None:
        stats = refresh_database(self.settings)
        self.assertGreater(stats.inserted_rows, 0)
        conn = sqlite3.connect(self.settings.sqlite_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            self.assertEqual(count, stats.inserted_rows)
            pr_count = conn.execute("SELECT COUNT(*) FROM issues WHERE issue_number IS NULL").fetchone()[0]
            self.assertEqual(pr_count, 0)
        finally:
            conn.close()

    def test_classification_helpers(self) -> None:
        text = "Qwen3 on GB200 hits torch.compile performance regression"
        self.assertEqual(extract_tag(text.lower(), "model"), "Qwen")
        self.assertEqual(extract_tag(text.lower(), "hardware"), "GB200")
        self.assertEqual(classify_failure_mode(text.lower())[0], "compile_kernel_backend")
        self.assertEqual(classify_roadmap_tag(text.lower()), "torch_compile")

    def test_parse_issue_type_from_labels(self) -> None:
        self.assertEqual(parse_issue_type_from_labels("bug"), "Bug")
        self.assertEqual(parse_issue_type_from_labels("feature request"), "Feature Request")
        self.assertEqual(parse_issue_type_from_labels("documentation"), "Usage/Question")
        self.assertEqual(parse_issue_type_from_labels("usage"), "Usage/Question")
        self.assertEqual(parse_issue_type_from_labels("new-model"), "Model Request")
        self.assertEqual(parse_issue_type_from_labels("rfc"), "RFC/Discussion")
        self.assertEqual(parse_issue_type_from_labels("feature request, unstale"), "Feature Request")
        self.assertIsNone(parse_issue_type_from_labels("stale, good first issue"))
        self.assertIsNone(parse_issue_type_from_labels(""))

    def test_schema_has_new_columns(self) -> None:
        refresh_database(self.settings)
        conn = sqlite3.connect(self.settings.sqlite_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
            for col in ("issue_type", "sig_group", "model_tags", "hardware_tags"):
                self.assertIn(col, columns, f"Missing column: {col}")
        finally:
            conn.close()

    def test_issue_type_populated_from_labels(self) -> None:
        refresh_database(self.settings)
        conn = sqlite3.connect(self.settings.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            # Check that at least some issues got issue_type from labels
            typed_count = conn.execute(
                "SELECT COUNT(*) FROM issues WHERE issue_type IS NOT NULL"
            ).fetchone()[0]
            self.assertGreater(typed_count, 0, "No issues got issue_type from labels")

            # Check a known label mapping — issues with "bug" label should be "Bug"
            bug_rows = conn.execute(
                "SELECT issue_type FROM issues WHERE labels LIKE '%bug%' AND issue_type IS NOT NULL"
            ).fetchall()
            for row in bug_rows:
                self.assertIn(row["issue_type"], ("Bug",))
        finally:
            conn.close()

    def test_format_dashboard_issues_block(self) -> None:
        from vllm_issue_tracker.prompts import format_dashboard_issues_block

        issues = [
            {"issue_number": 100, "title": "A bug", "body": "details", "issue_type": "Bug"},
            {"issue_number": 200, "title": "Unknown", "body": "no label", "issue_type": None},
        ]
        result = format_dashboard_issues_block(issues)
        self.assertIn("[known_type: Bug]", result)
        self.assertIn("[known_type: NONE]", result)
        self.assertIn("ISSUE 100", result)
        self.assertIn("ISSUE 200", result)


    def test_render_roadmap_html(self) -> None:
        from vllm_issue_tracker.report import render_roadmap_html

        mock_summary = {
            "sig_summaries": [
                {
                    "sig_group": "Core Engine",
                    "clusters": [
                        {
                            "main_fix": "FP8 scale inconsistency",
                            "priority": 1,
                            "issues": [
                                {"number": 37054, "summary": "Fix scale propagation"},
                                {"number": 37725, "summary": "NaN on Blackwell"},
                            ],
                            "categories": {
                                "type": "Bug",
                                "models": ["DeepSeek"],
                                "hardware": ["H100"],
                            },
                        },
                    ],
                },
            ],
        }
        html_out = render_roadmap_html(mock_summary, total_issues=100, total_classified=90)
        self.assertIn("vLLM Issue Roadmap", html_out)
        self.assertIn("Core Engine", html_out)
        self.assertIn("FP8 scale inconsistency", html_out)
        self.assertIn("#37054", html_out)
        self.assertIn("Fix scale propagation", html_out)
        self.assertIn("DeepSeek", html_out)
        self.assertIn("H100", html_out)

    def test_build_roadmap_report(self) -> None:
        import json
        from vllm_issue_tracker.report import build_roadmap_report

        refresh_database(self.settings)
        # Write mock summary
        mock_summary = {
            "sig_summaries": [
                {
                    "sig_group": "Frontend / API",
                    "clusters": [
                        {
                            "main_fix": "Tool calling errors",
                            "priority": 1,
                            "issues": [{"number": 1, "summary": "Parser crash"}],
                            "categories": {"type": "Bug", "models": [], "hardware": []},
                        },
                    ],
                },
            ],
        }
        self.settings.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.summary_path.write_text(
            json.dumps(mock_summary), encoding="utf-8"
        )
        path = build_roadmap_report(self.settings)
        self.assertTrue(path.exists())
        contents = path.read_text(encoding="utf-8")
        self.assertIn("Frontend / API", contents)
        self.assertIn("Tool calling errors", contents)


if __name__ == "__main__":
    unittest.main()
