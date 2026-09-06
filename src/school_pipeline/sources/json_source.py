from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from pathlib import Path

from ..models import EventType, SchoolEvent, SourceRef

logger = logging.getLogger(__name__)


class ExtractedEventsError(ValueError):
    """JSONの書き方が想定と違うときに送出する。

    このファイルは人か対話中のアシスタントが書くので、どこがどう悪いのかを
    そのまま読んで直せるメッセージにする。
    """


class JsonEventsSource:
    """抽出済みの予定をJSONファイルから読み込む。

    配布物の読み取りを Claude Code などの対話セッションで済ませ、その結果を
    JSONで受け取るための取得元。Anthropic API を呼ばないので費用がかからず、
    APIキーの管理も要らない。学校側の制限で自動巡回ができず、どのみち人が
    「新しい資料が来た」と持ち込む運用になっているため、この形が実態に合う。

    読み込んだ後の扱い(同一性による統合、日付の食い違いの検出、除外ルール、
    カレンダーへの冪等な同期)は、他の取得元とまったく同じである。
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def fetch_events(self) -> list[SchoolEvent]:
        if not self._directory.exists():
            return []
        events: list[SchoolEvent] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                events.extend(load_extracted_events(path))
            except (ExtractedEventsError, json.JSONDecodeError, OSError) as exc:
                # 1ファイル壊れていても他は取り込む。黙って減ると気づけないので必ず出す。
                logger.error("%s を読み込めませんでした: %s", path, exc)
        return events


def load_extracted_events(path: str | Path) -> list[SchoolEvent]:
    """1ファイル分の抽出結果を読み込む。書式は docs/extraction-format.md を参照。"""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ExtractedEventsError("最上位はオブジェクトにしてください(events を持つ形)")

    source = _to_source(data.get("source"), fallback_label=p.stem, fallback_id=str(p))
    document_audience = data.get("audience") or None

    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raise ExtractedEventsError("'events' は配列にしてください")

    events: list[SchoolEvent] = []
    for index, raw in enumerate(raw_events):
        try:
            event = _to_event(raw, source=source, document_audience=document_audience)
        except ExtractedEventsError as exc:
            raise ExtractedEventsError(f"events[{index}]: {exc}") from exc
        events.append(event)
    return events


def _to_source(raw, *, fallback_label: str, fallback_id: str) -> SourceRef:
    """出典。省略されたらファイル名で補う。

    captured_at(配布日/受信日)は、資料どうしで日付が食い違ったときにどちらが
    新しいかの判断に使う。省略すると古い資料が新しい資料を上書きしうるので、
    分かる範囲で書いてもらいたい。
    """
    raw = raw if isinstance(raw, dict) else {}
    captured_at = raw.get("captured_at")
    if captured_at:
        _parse_date(captured_at, field="source.captured_at")
    return SourceRef(
        source_type=raw.get("source_type") or "extracted",
        source_id=raw.get("source_id") or fallback_id,
        label=raw.get("label") or fallback_label,
        account=raw.get("account"),
        captured_at=captured_at,
    )


def _to_event(raw, *, source: SourceRef, document_audience: str | None) -> SchoolEvent:
    if not isinstance(raw, dict):
        raise ExtractedEventsError(f"オブジェクトにしてください(いまは {type(raw).__name__})")

    event_type = raw.get("type")
    try:
        parsed_type = EventType(event_type)
    except ValueError:
        allowed = " / ".join(t.value for t in EventType)
        raise ExtractedEventsError(f"'type' が不正です: {event_type!r} (使えるのは {allowed})")

    title = (raw.get("title") or "").strip()
    if not title:
        raise ExtractedEventsError("'title' は必須です")

    start_time = _parse_time(raw.get("start_time"), field="start_time")
    return SchoolEvent(
        type=parsed_type,
        title=title,
        date=_parse_date(raw.get("date"), field="date"),
        end_date=_parse_date(raw.get("end_date"), field="end_date", allow_none=True),
        start_time=start_time,
        end_time=_parse_time(raw.get("end_time"), field="end_time"),
        all_day=start_time is None,
        subject=(raw.get("subject") or None),
        location=(raw.get("location") or None),
        description=(raw.get("description") or "").strip(),
        confidence=_parse_confidence(raw.get("confidence")),
        identity_key=(raw.get("identity_key") or None),
        audience=(raw.get("audience") or document_audience),
        source=source,
    )


def _parse_date(value, *, field: str, allow_none: bool = False):
    if value in (None, ""):
        if allow_none:
            return None
        raise ExtractedEventsError(f"'{field}' は必須です(YYYY-MM-DD)")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ExtractedEventsError(f"'{field}' はYYYY-MM-DD形式にしてください: {value!r}")


def _parse_time(value, *, field: str):
    if value in (None, ""):
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise ExtractedEventsError(f"'{field}' はHH:MM形式にしてください: {value!r}")


def _parse_confidence(value) -> float:
    if value is None:
        return 0.9
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        raise ExtractedEventsError(f"'confidence' は数値にしてください: {value!r}")
    if not 0.0 <= confidence <= 1.0:
        raise ExtractedEventsError(f"'confidence' は 0.0〜1.0 にしてください: {confidence}")
    return confidence
