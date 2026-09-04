from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GmailAccountSettings:
    name: str
    credentials_path: str
    token_path: str
    query: str = "newer_than:14d"
    max_results: int = 50


@dataclass
class Settings:
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_model: str = "claude-sonnet-5"
    gmail_accounts: list[GmailAccountSettings] = field(default_factory=list)
    pdf_directory: str | None = None
    image_directory: str | None = None
    manual_text_directory: str | None = None
    google_credentials_path: str | None = None
    google_calendar_token_path: str = "calendar_token.json"
    calendar_id: str = "primary"
    confidence_threshold: float = 0.5
    cache_path: str = ".school_pipeline_cache.json"


def load_settings(path: str | Path) -> Settings:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # "gmail_accounts:" left with no entries parses as None, not an empty list.
    accounts = [GmailAccountSettings(**acc) for acc in (data.get("gmail_accounts") or [])]
    return Settings(
        anthropic_api_key_env=data.get("anthropic_api_key_env", "ANTHROPIC_API_KEY"),
        anthropic_model=data.get("anthropic_model", "claude-sonnet-5"),
        gmail_accounts=accounts,
        pdf_directory=data.get("pdf_directory"),
        image_directory=data.get("image_directory"),
        manual_text_directory=data.get("manual_text_directory"),
        google_credentials_path=data.get("google_credentials_path"),
        google_calendar_token_path=data.get("google_calendar_token_path", "calendar_token.json"),
        calendar_id=data.get("calendar_id", "primary"),
        confidence_threshold=float(data.get("confidence_threshold", 0.5)),
        cache_path=data.get("cache_path", ".school_pipeline_cache.json"),
    )
