from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, time
from enum import Enum


class EventType(str, Enum):
    EVENT = "school_event"
    QUIZ = "quiz"
    EXAM = "exam"
    ASSIGNMENT = "assignment"
    OTHER = "other"

    @property
    def label_ja(self) -> str:
        return {
            EventType.EVENT: "学校行事",
            EventType.QUIZ: "小テスト",
            EventType.EXAM: "定期試験",
            EventType.ASSIGNMENT: "課題",
            EventType.OTHER: "その他",
        }[self]


@dataclass
class SourceRef:
    """予定の出典(取得元)情報。"""

    source_type: str  # "gmail" | "pdf" | "image" | "manual"
    source_id: str  # メッセージID / ファイルパスなど、出典内で一意な識別子
    label: str  # 件名やファイル名など、人が読める表示名
    account: str | None = None  # gmail の場合のアカウント名
    captured_at: str | None = None  # 受信日/配布日 (ISO date)


@dataclass
class SchoolEvent:
    type: EventType
    title: str
    date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    all_day: bool = True
    subject: str | None = None  # 科目名。学校全体の行事などは None
    location: str | None = None
    description: str = ""
    confidence: float = 1.0
    source: SourceRef | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.7

    @property
    def stable_id(self) -> str:
        # タイトルは意図的に含めない: LLMが同じ予定を毎回微妙に異なる文言で
        # 出力しても、type/subject/date が同じなら同一予定とみなして
        # カレンダー上で重複登録せず更新扱いにするため。
        key = "|".join(
            [
                self.type.value,
                (self.subject or "").strip(),
                self.date.isoformat(),
                self.end_date.isoformat() if self.end_date else "",
                self.start_time.isoformat() if self.start_time else "",
            ]
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
