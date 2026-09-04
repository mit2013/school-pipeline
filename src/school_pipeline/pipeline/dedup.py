from __future__ import annotations

from dataclasses import replace
from datetime import time
from difflib import SequenceMatcher

from ..models import SchoolEvent


def dedup_events(events: list[SchoolEvent]) -> list[SchoolEvent]:
    """stable_id が一致する予定(同種別・同科目・同日時)を1件にまとめる。"""
    merged: dict[str, SchoolEvent] = {}
    for event in events:
        existing = merged.get(event.stable_id)
        merged[event.stable_id] = event if existing is None else _merge(existing, event)
    return sorted(merged.values(), key=lambda e: (e.date, e.start_time or time.min, e.title))


def _merge(a: SchoolEvent, b: SchoolEvent) -> SchoolEvent:
    keep, other = (a, b) if len(a.description) >= len(b.description) else (b, a)
    merged = replace(keep, confidence=max(a.confidence, b.confidence))
    if (
        other.source
        and keep.source
        and other.source.source_id != keep.source.source_id
        and other.source.label not in merged.description
    ):
        merged.description = f"{merged.description}\n[出典: {other.source.label}]".strip()
    return merged


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
            similarity = SequenceMatcher(None, a.title, b.title).ratio()
            if similarity >= similarity_threshold:
                pairs.append((a, b))
    return pairs
