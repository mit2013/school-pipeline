from __future__ import annotations

import logging
import os
import sys

import click
from dotenv import load_dotenv

from .calendar_sync.google_calendar import GoogleCalendarSync
from .config import load_settings
from .extraction.llm_extractor import AnthropicExtractor, AnthropicMessagesClient
from .pipeline.cache import ExtractionCache
from .pipeline.runner import PipelineResult, run_pipeline
from .pipeline.usage import CreditLedger, UsageTotals, format_money
from .sources.gmail_source import GmailAccountConfig, GmailSource
from .sources.image_source import ImageSource
from .sources.manual_source import ManualTextSource
from .sources.pdf_source import PdfSource

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# カレントディレクトリの .env から環境変数を読み込む(既に設定済みの値は上書きしない)。
# これにより、school-pipeline実行用のシェルで毎回 export しなくて済む。
load_dotenv()

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
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="キャッシュを無視し、内容が変わっていないファイルも含めてすべて再抽出する",
)
def run(config_path: str, push: bool, force: bool) -> None:
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

    cache = ExtractionCache.load(settings.cache_path)
    result = run_pipeline(
        sources,
        extractor,
        confidence_threshold=settings.confidence_threshold,
        cache=cache,
        force=force,
    )
    cache.save(settings.cache_path)
    _report_usage(extractor.usage, settings)
    _print_report(result)

    if not result.events:
        return

    service = _build_calendar_service(settings)
    syncer = GoogleCalendarSync(service, settings.calendar_id, settings.timezone)
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


def _report_usage(usage: UsageTotals, settings) -> None:
    """この実行でかかったAPI費用と、残高の目安を表示する。"""
    if usage.calls == 0:
        click.echo("\nAPI呼び出し: 0回(すべてキャッシュから取得したため課金はありません)")
        return

    cost = usage.cost_usd(settings.anthropic_model)
    click.echo(
        f"\nAPI呼び出し: {usage.calls}回 "
        f"(入力{usage.input_tokens:,} / 出力{usage.output_tokens:,} トークン)"
    )
    if cost is None:
        click.echo(f"今回の費用: 不明({settings.anthropic_model} の単価が未登録です)")
        return

    click.echo(f"今回の費用: {format_money(cost, settings.jpy_per_usd)}")

    ledger = CreditLedger.load(settings.usage_ledger_path)
    ledger.record(cost, usage, settings.anthropic_model)
    ledger.save(settings.usage_ledger_path)

    remaining = ledger.estimated_remaining_usd
    if remaining is None:
        click.echo(
            "残高の目安: 未設定 "
            "(`school-pipeline balance --set <Consoleの残高>` で基準を登録してください)"
        )
    else:
        click.echo(
            f"残高の目安: {format_money(remaining, settings.jpy_per_usd)} "
            f"※{ledger.anchor_date}に${ledger.anchor_usd:.2f}で同期"
        )


@main.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--set",
    "set_balance",
    type=float,
    default=None,
    help="Consoleで確認した残高(USD)で基準を貼り直す",
)
def balance(config_path: str, set_balance: float | None) -> None:
    """APIクレジット残高の目安を表示する(--set で実際の残高に同期)。

    Anthropicは残高を返すAPIを公開していないため、正確な残高は Console
    (https://platform.claude.com/settings/billing) でしか確認できない。
    ここでは最後に同期した残高からの支出を差し引いた目安を表示する。
    """
    settings = load_settings(config_path)
    ledger = CreditLedger.load(settings.usage_ledger_path)

    if set_balance is not None:
        ledger.set_anchor(set_balance)
        ledger.save(settings.usage_ledger_path)
        click.echo(
            f"残高を {format_money(set_balance, settings.jpy_per_usd)} に同期しました"
            f"({ledger.anchor_date}時点)。"
        )
        return

    remaining = ledger.estimated_remaining_usd
    if remaining is None:
        click.echo("残高の基準が未設定です。")
        click.echo("  https://platform.claude.com/settings/billing で残高を確認し、")
        click.echo("  school-pipeline balance --set 3.49 のように登録してください。")
        return

    click.echo(f"残高の目安: {format_money(remaining, settings.jpy_per_usd)}")
    click.echo(
        f"  基準: {ledger.anchor_date} 時点で ${ledger.anchor_usd:.2f}"
        f" / それ以降の使用: {format_money(ledger.spent_since_anchor_usd, settings.jpy_per_usd)}"
    )
    if ledger.history:
        click.echo("  直近の実行:")
        for h in ledger.history[-5:]:
            click.echo(
                f"    {h['date']}  {h['calls']}回  "
                f"${h['cost_usd']:.4f}  ({h['model']})"
            )
    click.echo("\n※あくまで目安です。ズレてきたら Console の値で --set し直してください。")
