from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "anthropic"  # "anthropic" or "openai"
    model: str = ""  # empty = use provider default
    batch_size: int = 50  # issues per LLM classification call
    max_concurrent: int = 5  # max parallel LLM calls
    thinking_budget: int = 10000  # tokens for extended thinking (0 = disabled)

    @property
    def resolved_model(self) -> str:
        if self.model:
            return self.model
        if self.provider == "anthropic":
            return "claude-opus-4-20250514"
        return "gpt-4o"

    @property
    def sonnet_model(self) -> str:
        """Return the Sonnet model ID for lightweight tasks."""
        return "claude-sonnet-4-20250514"


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    build_dir: Path
    output_dir: Path
    issues_csv: Path
    users_csv: Path
    comments_csv: Path
    sqlite_path: Path
    report_path: Path
    roadmap_path: Path
    summary_path: Path
    report_window_days: int = 30
    top_n: int = 5
    cited_issue_count: int = 12
    power_user_count: int = 5
    llm: LLMSettings = field(default_factory=LLMSettings)


def get_settings(root_dir: Path | None = None) -> Settings:
    root = root_dir or Path(__file__).resolve().parents[2]
    data_dir = root / "data"
    build_dir = root / "build"
    output_dir = root / "outputs"

    llm = LLMSettings(
        provider=os.environ.get("LLM_PROVIDER", "anthropic"),
        model=os.environ.get("LLM_MODEL", ""),
        batch_size=int(os.environ.get("LLM_BATCH_SIZE", "50")),
        max_concurrent=int(os.environ.get("LLM_MAX_CONCURRENT", "5")),
        thinking_budget=int(os.environ.get("LLM_THINKING_BUDGET", "10000")),
    )

    return Settings(
        root_dir=root,
        data_dir=data_dir,
        build_dir=build_dir,
        output_dir=output_dir,
        issues_csv=data_dir / "github_issues.csv",
        users_csv=data_dir / "users.csv",
        comments_csv=data_dir / "issue_comments.csv",
        sqlite_path=build_dir / "vllm_issue_snapshot.sqlite3",
        report_path=output_dir / "report.html",
        roadmap_path=output_dir / "roadmap.html",
        summary_path=build_dir / "dashboard_summary.json",
        llm=llm,
    )

