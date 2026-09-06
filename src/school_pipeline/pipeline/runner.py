from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..models import SchoolEvent, SourceRef, audience_excludes, audience_lists_my_class, audience_matches, normalize_identity
from ..sources.base import Source
from .cache import ExtractionCache
from .dedup import EventConflict, dedup_events, find_possible_duplicates
from .exclusions import ExclusionRule, ExclusionRules

logger = logging.getLogger(__name__)

# 終日イベントがこの日数を超えて続く場合、日付を特定できていない疑いが強い。
# 「9月中のどこか1日、抜き打ちで実施」が9月まるごとの予定になり、カレンダー上で
# 毎日表示され続けた実例があるため、既定値はひと学期より十分短くしてある。
DEFAULT_MAX_SPAN_DAYS = 14


class Extractor(Protocol):
    def extract(
        self, text: str, *, reference_date: date, source: SourceRef | None = None
    ) -> list[SchoolEvent]: ...


@dataclass
class PipelineResult:
    events: list[SchoolEvent]
    possible_duplicates: list[tuple[SchoolEvent, SchoolEvent]]
    low_confidence: list[SchoolEvent]
    # 同じ予定について資料ごとに日付や対象クラスが食い違っていたもの。
    conflicts: list[EventConflict] = field(default_factory=list)
    # 除外ルールで登録対象から外したもの。
    excluded: list[tuple[SchoolEvent, ExclusionRule]] = field(default_factory=list)
    # 取り込んだ資料そのものへの注意喚起(対象が判定できないなど)。
    warnings: list[str] = field(default_factory=list)
    # 対象が他クラスと明記されていたため取り込まなかったもの。
    other_class: list[SchoolEvent] = field(default_factory=list)


def run_pipeline(
    sources: list[Source],
    extractor: Extractor,
    *,
    confidence_threshold: float = 0.5,
    cache: ExtractionCache | None = None,
    force: bool = False,
    exclusions: ExclusionRules | None = None,
    max_span_days: int = DEFAULT_MAX_SPAN_DAYS,
    my_class: str | None = None,
    my_class_aliases: list | None = None,
) -> PipelineResult:
    # 抽出条件(モデル・プロンプト・ツール定義)の指紋。これが変わったキャッシュは使わない。
    fingerprint = getattr(extractor, "fingerprint", "")

    all_events: list[SchoolEvent] = []
    for source in sources:
        if hasattr(source, "fetch_events"):
            # 抽出を通さず予定を直接返す取得元(表形式の資料など)。API費用はかからない。
            structured = source.fetch_events()
            logger.info("Read %d events directly from %s", len(structured), type(source).__name__)
            all_events.extend(structured)
            continue
        for doc in source.fetch():
            source_key = f"{doc.source.source_type}:{doc.source.source_id}"
            content_hash = ExtractionCache.content_hash(doc.text)

            if cache is not None and not force:
                cached_events = cache.get(source_key, content_hash, fingerprint)
                if cached_events is not None:
                    logger.info("Using cached extraction for %s (unchanged since last run)", doc.source.label)
                    for ev in cached_events:
                        ev.source = doc.source
                    all_events.extend(cached_events)
                    continue

            try:
                events = extractor.extract(doc.text, reference_date=doc.reference_date, source=doc.source)
            except Exception:
                logger.exception("Extraction failed for %s", doc.source.label)
                continue

            if cache is not None:
                cache.put(source_key, content_hash, events, fingerprint)
            all_events.extend(events)

    aliases = my_class_aliases or []
    all_events, other_class = _split_off_other_classes(all_events, my_class, aliases)
    _flag_events_outside_the_school_year(all_events)
    warnings = _audience_warnings(all_events, my_class, aliases)
    _flag_implausibly_long_events(all_events, max_span_days)

    deduped, conflicts = dedup_events(all_events)
    kept, excluded = (exclusions or ExclusionRules()).split(deduped)
    low_confidence = [e for e in kept if e.confidence < confidence_threshold]
    kept = [e for e in kept if e.confidence >= confidence_threshold]
    duplicates = find_possible_duplicates(kept)
    return PipelineResult(
        events=kept,
        possible_duplicates=duplicates,
        low_confidence=low_confidence,
        conflicts=conflicts,
        excluded=excluded,
        warnings=warnings,
        other_class=other_class,
    )


def _flag_implausibly_long_events(events: list[SchoolEvent], max_span_days: int) -> None:
    """長すぎる終日イベントの確信度を落とし、理由を本文に残す。

    「9月中のどこか1日」のように日付が定まらない連絡を、期間全体にまたがる
    1件の予定として抽出してしまうことがある。カレンダー上では毎日表示され
    続けるので実害が大きい。プロンプトでも禁じているが、ここでも止める。
    """
    for event in events:
        if not event.all_day or event.end_date is None:
            continue
        span = (event.end_date - event.date).days
        if span <= max_span_days:
            continue
        note = f"[要確認: {span + 1}日間にわたる予定として抽出されました。実施日が特定できていない可能性があります]"
        event.confidence = min(event.confidence, 0.3)
        if note not in event.description:
            event.description = f"{event.description}\n{note}".strip()
        logger.info(
            "確信度を下げました: %s %s (%d日間)", event.date, event.title, span + 1
        )


def _flag_events_outside_the_school_year(events: list[SchoolEvent]) -> None:
    """資料が配られた年度から外れた予定の確信度を落とす。

    年が本文に書かれていない日付(「10月4日」など)の年をLLMが取り違えることがある。
    実例として、ファイル名の「250925」という数字列を根拠に2025年と判断し、2026年度の
    行事が1年ずれて登録されかけた。曜日も日付も合っているぶん、目視では見落としやすい。

    学校の年度は4月始まりなので、資料の配布日から年度を決めて突き合わせる。
    確信度を下げるだけで捨てはしない(年度をまたぐ連絡が本当にありうるため)。
    """
    for event in events:
        captured = event.source.captured_at if event.source else None
        if not captured:
            continue
        try:
            reference = date.fromisoformat(captured)
        except ValueError:
            continue
        if _school_year(event.date) == _school_year(reference):
            continue
        note = (
            f"[要確認: 資料の配布日({reference.isoformat()})とは違う年度の予定として"
            f"抽出されました。年の取り違えの可能性があります]"
        )
        event.confidence = min(event.confidence, 0.4)
        if note not in event.description:
            event.description = f"{event.description}\n{note}".strip()
        logger.info("確信度を下げました: %s %s (年度が資料と一致しません)", event.date, event.title)


def _school_year(value: date) -> int:
    """学校の年度(4月始まり)。1〜3月は前年の年度に属する。"""
    return value.year if value.month >= 4 else value.year - 1


def _split_off_other_classes(
    events: list[SchoolEvent], my_class: str | None, aliases: list
) -> tuple[list[SchoolEvent], list[SchoolEvent]]:
    """対象が他クラスだと明記されている予定を取り除く。

    1つの連絡の中でクラスごとに違う日程が示されることがある(「3・4・5・A組は
    9月7日、1・2組は9月8日」など)。どちらも同じ試験なので識別子は同じになり、
    放っておくと片方が勝手に採用されて、他クラスの日程が登録されてしまう。
    """
    if not my_class:
        return events, []
    mine: list[SchoolEvent] = []
    others: list[SchoolEvent] = []
    for event in events:
        (others if audience_excludes(event.audience, my_class, aliases) else mine).append(event)
    return mine, others


def _audience_warnings(events: list[SchoolEvent], my_class: str | None, aliases: list) -> list[str]:
    """対象が明記されているが、自分のクラスが含まれるか判定できない資料を知らせる。

    「B先生担当クラス」のようにクラス番号で書かれておらず、別名にも当てはまらない
    対象は機械的に判定できない。勝手に捨てず、人が確認できるよう知らせるだけにする。
    """
    if not my_class:
        return []

    seen: set[tuple[str, str]] = set()
    warnings: list[str] = []
    for event in events:
        if not event.audience or not event.source:
            continue
        key = (event.source.label, event.audience)
        if key in seen:
            continue
        seen.add(key)
        if audience_matches(event.audience, my_class, aliases):
            continue
        if audience_lists_my_class(event.audience, my_class):
            continue
        if not _is_class_specific(event.audience):
            # 「中学1〜3年生」「2年生」のような学年・全体向けの資料。クラスを
            # 絞っていないので警告する必要がない。毎回出すと警告が読み流される。
            continue
        warnings.append(
            f"「{event.source.label}」は「{event.audience}」向けと書かれています"
            f"({my_class} が対象か確認してください)"
        )
    return warnings


def _is_class_specific(audience: str) -> bool:
    """その対象の書き方が、クラス単位で相手を絞っているか。

    絞っていない資料(学年全体・保護者全体)まで警告すると、毎回同じ警告が並んで
    肝心のクラス違いを読み飛ばすことになる。
    """
    stated = normalize_identity(audience)
    return any(word in stated for word in ("組", "クラス", "コース"))
