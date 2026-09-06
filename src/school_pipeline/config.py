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
    # 抽出済みの予定(JSON)を置くフォルダ。対話セッションで読み取った結果を受け取る。
    extracted_events_directory: str | None = None
    # 教科ごとの授業予定表(Excel)を置くフォルダ。LLMを通さず列を決め打ちで読む。
    xlsx_directory: str | None = None
    # 読み取り対象のシート名(部分一致)。クラス別にシートが分かれている場合に使う。
    xlsx_sheets_include: list[str] = field(default_factory=list)
    # Excelの予定に付ける科目名。省略するとファイル名から推測する。
    xlsx_subject: str | None = None
    google_credentials_path: str | None = None
    google_calendar_token_path: str = "calendar_token.json"
    calendar_id: str = "primary"
    # 時刻指定のある予定をGoogleカレンダーへ送る際のタイムゾーン(IANA名)。
    timezone: str = "Asia/Tokyo"
    confidence_threshold: float = 0.5
    # カレンダーに載せたくない予定を指定するルールファイル。
    exclusions_path: str = "exclusions.yaml"
    # 終日イベントがこの日数を超えて続く場合、日付が特定できていない疑いがあるとみなす。
    max_span_days: int = 14
    # 自分(息子)のクラス。クラス別に内容の違う資料を取り違えないための確認に使う。
    my_class: str | None = None
    # クラスの別名。コースが1クラスしかない場合など、配布物が別の呼び方で
    # 対象を書いてくることがあるため。
    my_class_aliases: list[str] = field(default_factory=list)
    # 資料には書かれていないが抽出に必要な前提(時間割など)。プロンプトに追記される。
    extraction_notes: list[str] = field(default_factory=list)
    cache_path: str = ".school_pipeline_cache.json"
    # API使用量と残高の目安を記録するファイル。
    usage_ledger_path: str = ".school_pipeline_usage.json"
    # 費用を円換算して表示するときのレート。
    jpy_per_usd: float = 155.0


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
        extracted_events_directory=data.get("extracted_events_directory"),
        xlsx_directory=data.get("xlsx_directory"),
        xlsx_sheets_include=list(data.get("xlsx_sheets_include") or []),
        xlsx_subject=data.get("xlsx_subject"),
        google_credentials_path=data.get("google_credentials_path"),
        google_calendar_token_path=data.get("google_calendar_token_path", "calendar_token.json"),
        calendar_id=data.get("calendar_id", "primary"),
        timezone=data.get("timezone", "Asia/Tokyo"),
        confidence_threshold=float(data.get("confidence_threshold", 0.5)),
        exclusions_path=data.get("exclusions_path", "exclusions.yaml"),
        max_span_days=int(data.get("max_span_days", 14)),
        my_class=data.get("my_class"),
        my_class_aliases=list(data.get("my_class_aliases") or []),
        extraction_notes=list(data.get("extraction_notes") or []),
        cache_path=data.get("cache_path", ".school_pipeline_cache.json"),
        usage_ledger_path=data.get("usage_ledger_path", ".school_pipeline_usage.json"),
        jpy_per_usd=float(data.get("jpy_per_usd", 155.0)),
    )
