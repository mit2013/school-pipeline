from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterator, Protocol

from ..models import SourceRef


@dataclass
class RawDocument:
    """取得元から得られた、抽出前の生テキスト1件分。"""

    source: SourceRef
    text: str
    reference_date: date  # 「来週」等の相対表現を解決する基準日 (受信日/配布日)


class Source(Protocol):
    def fetch(self) -> Iterator[RawDocument]: ...
