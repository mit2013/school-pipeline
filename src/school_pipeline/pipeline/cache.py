from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, time
from pathlib import Path

from ..models import EventType, SchoolEvent

logger = logging.getLogger(__name__)


class ExtractionCache:
    """内容が変わっていない文書は再度LLMを呼ばずに済ませるためのキャッシュ。

    同じPDF/メールを何度も処理し直す(試運転を繰り返す、毎日cronで回すなど)
    際に、変化のないファイルへの重複した課金を避けるために使う。
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> ExtractionCache:
        cache = cls()
        p = Path(path)
        if p.exists():
            try:
                cache._entries = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load extraction cache at %s: %s", p, exc)
        return cache

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, source_key: str, content_hash: str, fingerprint: str = "") -> list[SchoolEvent] | None:
        entry = self._entries.get(source_key)
        if entry is None or entry.get("hash") != content_hash:
            return None
        # 本文が同じでも、モデルやプロンプトが変わっていれば抽出結果は変わりうる。
        # 指紋を持たない古いエントリも、どの条件で作られたか分からないので作り直す。
        if entry.get("fingerprint") != fingerprint:
            return None
        return [_deserialize_event(raw) for raw in entry["events"]]

    def put(
        self, source_key: str, content_hash: str, events: list[SchoolEvent], fingerprint: str = ""
    ) -> None:
        self._entries[source_key] = {
            "hash": content_hash,
            "fingerprint": fingerprint,
            "events": [_serialize_event(ev) for ev in events],
        }


def _serialize_event(event: SchoolEvent) -> dict:
    return {
        "type": event.type.value,
        "title": event.title,
        "date": event.date.isoformat(),
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "start_time": event.start_time.isoformat() if event.start_time else None,
        "end_time": event.end_time.isoformat() if event.end_time else None,
        "all_day": event.all_day,
        "subject": event.subject,
        "location": event.location,
        "description": event.description,
        "confidence": event.confidence,
    }


def _deserialize_event(data: dict) -> SchoolEvent:
    return SchoolEvent(
        type=EventType(data["type"]),
        title=data["title"],
        date=date.fromisoformat(data["date"]),
        end_date=date.fromisoformat(data["end_date"]) if data.get("end_date") else None,
        start_time=time.fromisoformat(data["start_time"]) if data.get("start_time") else None,
        end_time=time.fromisoformat(data["end_time"]) if data.get("end_time") else None,
        all_day=data.get("all_day", True),
        subject=data.get("subject"),
        location=data.get("location"),
        description=data.get("description", ""),
        confidence=data.get("confidence", 1.0),
    )
