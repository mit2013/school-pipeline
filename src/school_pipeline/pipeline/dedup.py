from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from difflib import SequenceMatcher

from ..models import SchoolEvent, normalize_identity


@dataclass
class EventConflict:
    """同じ予定を指しているのに、資料ごとに言っていることが違うケース。

    黙って片方を選ぶと事故になる(前学期に、担当者違いのクラス別プリントを
    取り違えかけた実例がある)ので、必ず人が読める形で報告する。
    """

    kind: str  # "date" = 日付が食い違う / "audience" = 対象クラスが食い違う
    candidates: list[SchoolEvent]
    # 採用した予定。audience が食い違う場合は判断できないので None。
    chosen: SchoolEvent | None

    @property
    def label(self) -> str:
        head = self.candidates[0]
        subject = f"{head.subject} " if head.subject else ""
        return f"{subject}{head.identity_key or head.title}"


def dedup_events(events: list[SchoolEvent]) -> tuple[list[SchoolEvent], list[EventConflict]]:
    """同じ予定を指すものを1件にまとめ、まとめきれなかった食い違いを併せて返す。

    identity_key を持つ予定は日付を含まない stable_id になるため、提出日が
    後から変更されても同じグループに入る。そのとき採用するのは、最も新しい
    資料から取れたものである。
    """
    groups: dict[str, list[SchoolEvent]] = {}
    for event in events:
        groups.setdefault(event.stable_id, []).append(event)

    merged: list[SchoolEvent] = []
    conflicts: list[EventConflict] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue

        if _audiences_disagree(group):
            # 別クラス・別担当の資料が混ざっている。どちらが息子のものか
            # 機械的には決められないので、どちらも登録せず人に判断を委ねる。
            conflicts.append(EventConflict(kind="audience", candidates=list(group), chosen=None))
            continue

        winner = _most_recent(group)
        others = [e for e in group if e is not winner]
        if _dates_disagree(group):
            conflicts.append(EventConflict(kind="date", candidates=list(group), chosen=winner))
            merged.append(_merge_into(winner, others, note_date_change_from=others))
        else:
            merged.append(_merge_into(winner, others))

    return (
        sorted(merged, key=lambda e: (e.date, e.start_time or time.min, e.title)),
        conflicts,
    )


def _audiences_disagree(group: list[SchoolEvent]) -> bool:
    """どちらの資料も対象を明記していて、かつそれが違うか。

    片方でも未記載なら判断材料が足りないので「食い違い」とは見なさない。
    誤検出でいちいち登録が止まるほうが実用上つらいため。
    """
    stated = {normalize_identity(e.audience) for e in group if e.audience}
    return len(stated) > 1


def _dates_disagree(group: list[SchoolEvent]) -> bool:
    return len({(e.date, e.end_date, e.start_time) for e in group}) > 1


def _most_recent(group: list[SchoolEvent]) -> SchoolEvent:
    """最も新しい資料から取れた予定を選ぶ。

    同着なら確信度の高いほう、次に後ろの日付のほう(提出日は前倒しより
    後ろ倒しのほうが多いという経験則)、最後に説明の詳しいほうを採る。
    日付も資料の新しさも同じなら、情報量の多い記述を残したい。
    """
    return max(
        group,
        key=lambda e: (
            (e.source.captured_at or "") if e.source else "",
            e.confidence,
            e.date,
            len(e.description),
        ),
    )


def _merge_into(
    winner: SchoolEvent,
    others: list[SchoolEvent],
    *,
    note_date_change_from: list[SchoolEvent] | None = None,
) -> SchoolEvent:
    merged = replace(winner, confidence=max([winner.confidence] + [e.confidence for e in others]))
    for other in others:
        if (
            other.source
            and merged.source
            and other.source.source_id != merged.source.source_id
            and other.source.label not in merged.description
        ):
            merged.description = f"{merged.description}\n[出典: {other.source.label}]".strip()

    for old in note_date_change_from or []:
        if old.date == merged.date:
            continue
        origin = f"({old.source.label})" if old.source else ""
        note = f"[変更: {_md(old.date)} → {_md(merged.date)} {origin}]".strip()
        if note not in merged.description:
            merged.description = f"{merged.description}\n{note}".strip()
    return merged


def _md(value) -> str:
    return f"{value.month}/{value.day}"


def find_possible_duplicates(
    events: list[SchoolEvent], *, similarity_threshold: float = 0.6
) -> list[tuple[SchoolEvent, SchoolEvent]]:
    """stable_id は異なるが、同じ種別・同じ日でタイトルが似ている予定を検出する。

    自動統合はせず、人が確認できるよう候補として返す(誤って別々の予定を
    1つに統合してしまうリスクを避けるため)。
    """
    pairs: list[tuple[SchoolEvent, SchoolEvent]] = []
    for i, a in enumerate(events):
        for b in events[i + 1 :]:
            if a.stable_id == b.stable_id:
                continue
            if a.type != b.type or a.date != b.date:
                continue
            if a.identity_key and b.identity_key and a.identity != b.identity:
                # 別物だと明示されている(同じ日に締め切られるB-3とB-4など)。
                # タイトルが似ていて当然なので、重複候補として出しても雑音になる。
                continue
            similarity = SequenceMatcher(None, a.title, b.title).ratio()
            if similarity >= similarity_threshold:
                pairs.append((a, b))
    return pairs
