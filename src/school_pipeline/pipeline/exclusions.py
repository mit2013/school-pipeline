from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..models import EventType, SchoolEvent, normalize_identity

logger = logging.getLogger(__name__)


@dataclass
class ExclusionRule:
    """カレンダーに載せたくない予定を指定する1件のルール。

    抽出そのものは止めない(止めても資料に書いてある事実は変わらないし、
    再抽出の課金も発生しない)。抽出後に、登録対象から外すだけである。
    """

    title_contains: str | None = None
    identity: str | None = None
    subject: str | None = None
    source_label_contains: str | None = None
    type: EventType | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not any([self.title_contains, self.identity, self.subject, self.source_label_contains, self.type]):
            raise ValueError(
                "除外ルールには少なくとも1つの条件が必要です"
                "(title_contains / identity / subject / source_label_contains / type)"
            )

    def matches(self, event: SchoolEvent) -> bool:
        """指定された条件を「すべて」満たすときだけ除外する。

        条件はANDで組み合わさる。うっかり広く消しすぎないよう、
        条件を足すほど対象が狭くなる方向にしてある。
        """
        if self.type is not None and event.type != self.type:
            return False
        if self.title_contains and normalize_identity(self.title_contains) not in normalize_identity(event.title):
            return False
        if self.subject and normalize_identity(self.subject) != normalize_identity(event.subject):
            return False
        if self.identity and normalize_identity(self.identity) not in (
            normalize_identity(event.identity),
            normalize_identity(event.identity_key),
        ):
            return False
        if self.source_label_contains:
            label = event.source.label if event.source else ""
            if normalize_identity(self.source_label_contains) not in normalize_identity(label):
                return False
        return True


@dataclass
class ExclusionRules:
    rules: list[ExclusionRule] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None) -> ExclusionRules:
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("除外ルール %s を読み込めませんでした: %s", p, exc)
            return cls()

        rules: list[ExclusionRule] = []
        for raw in data.get("exclusions") or []:
            try:
                rules.append(_to_rule(raw))
            except (ValueError, TypeError) as exc:
                # 1つ壊れていても残りのルールは活かす。黙って全部無効になると、
                # 消したはずの予定が復活して原因が分かりにくい。
                logger.warning("除外ルールを読み飛ばしました: %s (%r)", exc, raw)
        return cls(rules=rules)

    def split(self, events: list[SchoolEvent]) -> tuple[list[SchoolEvent], list[tuple[SchoolEvent, ExclusionRule]]]:
        """(残す予定, [(除外した予定, 該当ルール)]) に振り分ける。"""
        kept: list[SchoolEvent] = []
        excluded: list[tuple[SchoolEvent, ExclusionRule]] = []
        for event in events:
            rule = next((r for r in self.rules if r.matches(event)), None)
            if rule is None:
                kept.append(event)
            else:
                excluded.append((event, rule))
        return kept, excluded


def _to_rule(raw: dict) -> ExclusionRule:
    if not isinstance(raw, dict):
        raise TypeError(f"除外ルールはマッピングで書いてください (got {type(raw).__name__})")
    event_type = raw.get("type")
    return ExclusionRule(
        title_contains=raw.get("title_contains"),
        identity=raw.get("identity"),
        subject=raw.get("subject"),
        source_label_contains=raw.get("source_label_contains"),
        type=EventType(event_type) if event_type else None,
        reason=raw.get("reason", ""),
    )
