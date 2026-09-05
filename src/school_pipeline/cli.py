from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from .calendar_sync.google_calendar import GoogleCalendarSync
from .config import load_settings
from .extraction.llm_extractor import AnthropicExtractor, AnthropicMessagesClient
from .models import EventType
from .pipeline.cache import ExtractionCache
from .pipeline.exclusions import ExclusionRules
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
@click.option(
    "--overwrite-manual",
    is_flag=True,
    default=False,
    help="手動で編集された予定も抽出結果で上書きする(通常は保護して残します)",
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help="抽出結果に無くなった予定をカレンダーから削除する(指定しない場合は一覧表示のみ)",
)
def run(config_path: str, push: bool, force: bool, overwrite_manual: bool, prune: bool) -> None:
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
        exclusions=ExclusionRules.load(settings.exclusions_path),
        max_span_days=settings.max_span_days,
        my_class=settings.my_class,
        my_class_aliases=settings.my_class_aliases,
    )
    cache.save(settings.cache_path)
    _report_usage(extractor.usage, settings)
    _print_report(result)

    if not result.events:
        click.echo("\n登録対象の予定がありません(カレンダーには触れません)。")
        return

    service = _build_calendar_service(settings)
    syncer = GoogleCalendarSync(
        service,
        settings.calendar_id,
        settings.timezone,
        overwrite_manual_edits=overwrite_manual,
    )
    stats = syncer.sync(result.events, dry_run=not push, prune=prune, keep_ids=_protected_ids(result))

    summary = f"新規{stats.created}件 / 更新{stats.updated}件 / 変更なし{stats.unchanged}件"
    if prune:
        summary += f" / 削除{stats.deleted}件"
    if push:
        click.echo(f"\n登録完了: {summary}")
    else:
        click.echo(f"\n[ドライラン] {summary} (--push を付けると実際に登録します)")

    if stats.skipped_manual:
        click.echo(
            f"手動で編集されていたため、そのまま残した予定: {stats.skipped_manual}件"
            " (抽出結果で戻したい場合は --overwrite-manual)"
        )

    if stats.orphans and not prune:
        click.echo(
            f"\nカレンダーに残っているが、今回の抽出結果には無い予定: {len(stats.orphans)}件"
        )
        for item in stats.orphans:
            start = item.get("start", {})
            when = start.get("date") or start.get("dateTime", "")[:10]
            click.echo(f"  - {when} {item.get('summary', '')}")
        click.echo("  --prune を付けると、これらを削除します。")


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
    for warning in result.warnings:
        click.echo(f"⚠ {warning}")
    if result.warnings:
        click.echo("")

    _print_conflicts(result)

    click.echo(f"抽出された予定: {len(result.events)}件")
    for ev in result.events:
        flag = " ★要確認" if ev.needs_review else ""
        click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title}{flag} (確信度{ev.confidence:.2f})")

    if result.low_confidence:
        click.echo(f"\n確信度が低く登録対象から除外された予定: {len(result.low_confidence)}件")
        for ev in result.low_confidence:
            click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title} (確信度{ev.confidence:.2f})")

    if result.other_class:
        click.echo(f"\n他クラス向けと明記されていたため取り込まなかった予定: {len(result.other_class)}件")
        for ev in result.other_class:
            click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title} (対象: {ev.audience})")

    if result.excluded:
        click.echo(f"\n除外ルールで登録対象から外した予定: {len(result.excluded)}件")
        for ev, rule in result.excluded:
            reason = f" — {rule.reason}" if rule.reason else ""
            click.echo(f"  - {ev.date} [{ev.type.label_ja}] {ev.title}{reason}")

    if result.possible_duplicates:
        click.echo(f"\n重複の可能性がある予定: {len(result.possible_duplicates)}組(自動統合はしていません)")
        for a, b in result.possible_duplicates:
            label_a = a.source.label if a.source else "?"
            label_b = b.source.label if b.source else "?"
            click.echo(f"  - 「{a.title}」({label_a}) と 「{b.title}」({label_b})")


def _protected_ids(result: PipelineResult) -> set:
    """登録はしないが、削除もしてはいけない予定の識別子。

    - 確信度がしきい値を下回った予定: 資料には依然として載っている。抽出のたびに
      確信度は多少ぶれるので、消してしまうと登録と削除を交互に繰り返すことになる。
    - 対象クラスが食い違って判断を保留した予定: どちらが正しいか決めていないのに
      片方を消すのは筋が通らない。

    逆に、除外ルールで外した予定と他クラス向けと分かった予定は保護しない。
    それらは「消したい」という意思表示なので、--prune の対象になる。
    """
    protected: set = set()
    _protect(protected, result.low_confidence)
    for conflict in result.conflicts:
        if conflict.kind == "audience":
            _protect(protected, conflict.candidates)
    return protected


def _protect(protected: set, events) -> None:
    # 旧形式の識別子も入れる。カレンダー上の予定がまだ移行前だと、新しい識別子だけでは
    # 照合できず、保護したはずの予定が削除候補として残ってしまう。
    for event in events:
        protected.add(event.stable_id)
        protected.add(event.legacy_stable_id)


def _print_conflicts(result: PipelineResult) -> None:
    """資料どうしで言っていることが食い違う予定を、判断できる形で見せる。

    黙って新しいほうを採用すると、担当者違い・クラス違いの資料を「変更」と
    誤認したときに気づけない。必ず両方の日付と出典を並べて出す。
    """
    if not result.conflicts:
        return

    date_conflicts = [c for c in result.conflicts if c.kind == "date"]
    audience_conflicts = [c for c in result.conflicts if c.kind == "audience"]

    if date_conflicts:
        click.echo(f"⚠ 日付が食い違う予定: {len(date_conflicts)}件")
        for conflict in date_conflicts:
            click.echo(f"  - {conflict.label}")
            for candidate in sorted(conflict.candidates, key=lambda e: e.date):
                click.echo(f"      {_candidate_line(candidate, candidate is conflict.chosen)}")
        click.echo("")

    if audience_conflicts:
        click.echo(f"⚠ 対象クラスが食い違うため登録を見送った予定: {len(audience_conflicts)}件")
        for conflict in audience_conflicts:
            click.echo(f"  - {conflict.label}")
            for candidate in sorted(conflict.candidates, key=lambda e: e.date):
                click.echo(f"      {_candidate_line(candidate, False)}")
        click.echo("    担当者違い・クラス違いの資料が混ざっている可能性があります。")
        click.echo("    自分のクラス向けでないほうを inbox/pdfs/_excluded/ へ移してください。")
        click.echo("")


def _candidate_line(event, chosen: bool) -> str:
    label = event.source.label if event.source else "?"
    captured = f", {event.source.captured_at}" if event.source and event.source.captured_at else ""
    audience = f", 対象: {event.audience}" if event.audience else ""
    mark = "  ★採用" if chosen else ""
    return f"{event.date} ← {label}{captured}{audience}{mark}"


@main.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--title-contains", default=None, help="タイトルに含まれる文字列で除外する")
@click.option("--identity", default=None, help="課題番号などの識別子で除外する(例: B-1)")
@click.option("--subject", default=None, help="科目で絞る(例: 英語)")
@click.option("--source-label-contains", default=None, help="出典のファイル名・件名で絞る")
@click.option(
    "--type",
    "event_type",
    type=click.Choice([t.value for t in EventType]),
    default=None,
    help="種別で絞る",
)
@click.option("--reason", default="", help="なぜ除外するかのメモ")
@click.option("--list", "list_rules", is_flag=True, default=False, help="登録済みの除外ルールを一覧表示する")
def exclude(
    config_path: str,
    title_contains: str | None,
    identity: str | None,
    subject: str | None,
    source_label_contains: str | None,
    event_type: str | None,
    reason: str,
    list_rules: bool,
) -> None:
    """カレンダーに載せたくない予定のルールを追加・確認する。

    すでにカレンダーに登録済みの予定は、このルールを足したうえで
    `school-pipeline run --push --prune` を実行すると削除される。
    """
    settings = load_settings(config_path)
    path = Path(settings.exclusions_path)

    if list_rules:
        rules = ExclusionRules.load(path).rules
        if not rules:
            click.echo(f"除外ルールはまだありません ({path})。")
            return
        click.echo(f"除外ルール: {len(rules)}件 ({path})")
        for rule in rules:
            conditions = ", ".join(
                f"{name}={value}"
                for name, value in [
                    ("title_contains", rule.title_contains),
                    ("identity", rule.identity),
                    ("subject", rule.subject),
                    ("source_label_contains", rule.source_label_contains),
                    ("type", rule.type.value if rule.type else None),
                ]
                if value
            )
            suffix = f" — {rule.reason}" if rule.reason else ""
            click.echo(f"  - {conditions}{suffix}")
        return

    conditions = {
        "title_contains": title_contains,
        "identity": identity,
        "subject": subject,
        "source_label_contains": source_label_contains,
        "type": event_type,
    }
    if not any(conditions.values()):
        click.echo(
            "条件を1つ以上指定してください"
            "(--title-contains / --identity / --subject / --source-label-contains / --type)。",
            err=True,
        )
        sys.exit(1)

    _append_exclusion(path, conditions, reason)
    click.echo(f"除外ルールを {path} に追加しました。")
    click.echo("  school-pipeline run          で、対象から外れたことを確認できます")
    click.echo("  school-pipeline run --push --prune  で、登録済みの予定を削除します")


def _append_exclusion(path: Path, conditions: dict, reason: str) -> None:
    """既存のコメントを壊さないよう、YAMLを読み書きせず末尾に追記する。

    このファイルは人が手で書くことも想定していて、書き方の説明がコメントとして
    入っている。yaml.safe_dump で書き戻すとそれが消えてしまう。
    """
    lines = []
    if not path.exists():
        lines.append("exclusions:")
    else:
        existing = path.read_text(encoding="utf-8")
        if "\nexclusions:" not in f"\n{existing}":
            lines.append("exclusions:")
        elif not existing.endswith("\n"):
            lines.append("")

    first = True
    for name, value in conditions.items():
        if not value:
            continue
        prefix = "  - " if first else "    "
        lines.append(f"{prefix}{name}: {_yaml_scalar(value)}")
        first = False
    if reason:
        lines.append(f"    reason: {_yaml_scalar(reason)}")

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _yaml_scalar(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
