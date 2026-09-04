from __future__ import annotations

import logging
import os
import sys

import click

from .calendar_sync.google_calendar import GoogleCalendarSync
from .config import load_settings
from .extraction.llm_extractor import AnthropicExtractor, AnthropicMessagesClient
from .pipeline.runner import PipelineResult, run_pipeline
from .sources.gmail_source import GmailAccountConfig, GmailSource
from .sources.image_source import ImageSource
from .sources.manual_source import ManualTextSource
from .sources.pdf_source import PdfSource

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


@click.group()
def main() -> None:
    """息子の学校予定をメール・プリント等から抽出しGoogleカレンダーへ登録するパイプライン。"""


@main.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--push/--no-push",
    default=False,
    help="実際にGoogleカレンダーへ書き込む(指定しない場合は差分の確認のみ)",
)
def run(config_path: str, push: bool) -> None:
    settings = load_settings(config_path)

    sources = _build_sources(settings)
    if not sources:
        click.echo(
            "設定ファイルに有効な取得元がありません"
            "(gmail_accounts / pdf_directory / image_directory / manual_text_directory)。",
            err=True,
        )
        sys.exit(1)

    api_key = os.environ.get(settings.anthropic_api_key_env)
    if not api_key:
        click.echo(f"環境変数 {settings.anthropic_api_key_env} が設定されていません。", err=True)
        sys.exit(1)
    extractor = AnthropicExtractor(AnthropicMessagesClient(api_key=api_key, model=settings.anthropic_model))

    result = run_pipeline(sources, extractor, confidence_threshold=settings.confidence_threshold)
    _print_report(result)

    if not result.events:
        return

    service = _build_calendar_service(settings)
    syncer = GoogleCalendarSync(service, settings.calendar_id)
    stats = syncer.sync(result.events, dry_run=not push)

    if push:
        click.echo(f"\n登録完了: 新規{stats.created}件 / 更新{stats.updated}件 / 変更なし{stats.unchanged}件")
    else:
        click.echo(
            f"\n[ドライラン] 新規{stats.created}件 / 更新{stats.updated}件 / 変更なし{stats.unchanged}件"
            " (--push を付けると実際に登録します)"
        )


@main.command("auth-gmail")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--account", required=True, help="config.yaml の gmail_accounts[].name")
def auth_gmail(config_path: str, account: str) -> None:
    """指定したGmailアカウントのOAuth認証を行いトークンを保存する。"""
    settings = load_settings(config_path)
    acc = next((a for a in settings.gmail_accounts if a.name == account), None)
    if acc is None:
        click.echo(f"アカウント '{account}' が config.yaml に見つかりません。", err=True)
        sys.exit(1)

    from .auth.google_auth import get_credentials

    get_credentials(acc.credentials_path, acc.token_path, ["https://www.googleapis.com/auth/gmail.readonly"])
    click.echo(f"{account} の認証が完了し、{acc.token_path} に保存しました。")


@main.command("auth-calendar")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def auth_calendar(config_path: str) -> None:
    """Googleカレンダーへのアクセス認証を行いトークンを保存する。"""
    settings = load_settings(config_path)
    from .auth.google_auth import get_credentials

    get_credentials(settings.google_credentials_path, settings.google_calendar_token_path, _CALENDAR_SCOPES)
    click.echo(f"カレンダー認証が完了し、{settings.google_calendar_token_path} に保存しました。")


def _build_sources(settings) -> list:
    sources: list = []
    for acc in settings.gmail_accounts:
        sources.append(
            GmailSource(
                GmailAccountConfig(
                    name=acc.name,
                    credentials_path=acc.credentials_path,
                    token_path=acc.token_path,
                    query=acc.query,
                    max_results=acc.max_results,
                )
            )
        )
    if settings.pdf_directory:
        sources.append(PdfSource(settings.pdf_directory))
    if settings.image_directory:
        sources.append(ImageSource(settings.image_directory))
    if settings.manual_text_directory:
        sources.append(ManualTextSource(settings.manual_text_directory))
    return sources


def _build_calendar_service(settings):
    from googleapiclient.discovery import build

    from .auth.google_auth import get_credentials

    creds = get_credentials(settings.google_credentials_path, settings.google_calendar_token_path, _CALENDAR_SCOPES)
    return build("calendar", "v3", credentials=creds)


def _print_report(result: PipelineResult) -> None:
    click.echo(f"抽出された予定: {len(result.events)}件")
    for ev in result.events:
        flag = " ★要確認" if ev.needs_review else ""
        click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title}{flag} (確信度{ev.confidence:.2f})")

    if result.low_confidence:
        click.echo(f"\n確信度が低く登録対象から除外された予定: {len(result.low_confidence)}件")
        for ev in result.low_confidence:
            click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title} (確信度{ev.confidence:.2f})")

    if result.possible_duplicates:
        click.echo(f"\n重複の可能性がある予定: {len(result.possible_duplicates)}組(自動統合はしていません)")
        for a, b in result.possible_duplicates:
            label_a = a.source.label if a.source else "?"
            label_b = b.source.label if b.source else "?"
            click.echo(f"  - 「{a.title}」({label_a}) と 「{b.title}」({label_b})")


if __name__ == "__main__":
    main()
