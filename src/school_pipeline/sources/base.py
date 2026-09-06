from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterator, Protocol

from ..models import SchoolEvent, SourceRef


@dataclass
class RawDocument:
    """取得元から得られた、抽出前の生テキスト1件分。"""

    source: SourceRef
    text: str
    reference_date: date  # 「来週」等の相対表現を解決する基準日 (受信日/配布日)


class Source(Protocol):
    def fetch(self) -> Iterator[RawDocument]: ...


class StructuredSource(Protocol):
    """本文を抽出にかけず、予定を直接組み立てられる取得元。

    表形式の資料のように、行と列で意味が決まっていて日付も確定しているものは、
    LLMに読ませる必要がない。決め打ちで読めば費用がかからず、解釈もぶれない。
    """

    def fetch_events(self) -> list[SchoolEvent]: ...
