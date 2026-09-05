from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..models import EventType, SchoolEvent

logger = logging.getLogger(__name__)

_EXT_PROPERTY_KEY = "school_pipeline_id"

# Googleカレンダーの colorId (1-11)。種別が一目でわかるよう色分けする。
_COLOR_BY_TYPE = {
    EventType.EXAM: "11",  # 赤系: 定期試験
    EventType.QUIZ: "5",  # 黄系: 小テスト
    EventType.EVENT: "9",  # 青系: 学校行事
    EventType.ASSIGNMENT: "10",  # 緑系: 課題
    EventType.OTHER: "8",  # 灰色: その他
}


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0


class GoogleCalendarSync:
    """SchoolEvent のリストをGoogleカレンダーへ冪等に同期する。

    各イベントの extendedProperties に stable_id を保存しておくことで、
    再実行しても重複登録せず、内容が変わった予定だけ更新する。
    """

    def __init__(self, service, calendar_id: str, timezone: str = "Asia/Tokyo") -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._timezone = timezone

    def sync(self, events: list[SchoolEvent], *, dry_run: bool = True) -> SyncStats:
        existing_by_stable_id = self._load_existing(events)
        stats = SyncStats()
        for event in events:
            body = _to_calendar_body(event, self._timezone)
            existing = existing_by_stable_id.get(event.stable_id)
            if existing is None:
                stats.created += 1
                logger.info("[CREATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
            elif _needs_update(existing, body, self._timezone):
                stats.updated += 1
                logger.info("[UPDATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().update(
                        calendarId=self._calendar_id, eventId=existing["id"], body=body
                    ).execute()
            else:
                stats.unchanged += 1
        return stats

    def _load_existing(self, events: list[SchoolEvent] | None = None) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        page_token = None
        # 同期対象に過去の予定(前学期の課題など)が含まれていても既存登録を検出できる
        # よう、最も古い予定まで遡って取得する。ここを today 起点にすると、窓の外に
        # ある既存イベントが「未登録」と誤判定され、再実行のたびに重複登録される。
        oldest = min((e.date for e in events), default=None) if events else None
        start = min(oldest, date.today()) if oldest else date.today()
        time_min = (start - timedelta(days=30)).isoformat() + "T00:00:00Z"
        while True:
            kwargs = dict(
                calendarId=self._calendar_id,
                timeMin=time_min,
                maxResults=2500,
                singleEvents=True,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._service.events().list(**kwargs).execute()
            for item in resp.get("items", []):
                stable_id = item.get("extendedProperties", {}).get("private", {}).get(_EXT_PROPERTY_KEY)
                if stable_id:
                    by_id[stable_id] = item
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return by_id


def _to_calendar_body(event: SchoolEvent, timezone: str = "Asia/Tokyo") -> dict:
    summary = f"[{event.type.label_ja}] {event.title}"
    if event.subject:
        summary += f"({event.subject})"
    body: dict = {
        "summary": summary,
        "description": event.description,
        "colorId": _COLOR_BY_TYPE.get(event.type),
        "extendedProperties": {"private": {_EXT_PROPERTY_KEY: event.stable_id}},
    }
    if event.location:
        body["location"] = event.location

    end_date = event.end_date or event.date
    if event.all_day:
        body["start"] = {"date": event.date.isoformat()}
        # Googleカレンダーの終日イベントは end.date が「翌日」扱いなので +1 する。
        body["end"] = {"date": (end_date + timedelta(days=1)).isoformat()}
    else:
        # timeZone が無いと Google API に 400 "Missing time zone definition" で弾かれる。
        body["start"] = {
            "dateTime": f"{event.date.isoformat()}T{event.start_time.strftime('%H:%M')}:00",
            "timeZone": timezone,
        }
        end_time = event.end_time or event.start_time
        body["end"] = {
            "dateTime": f"{end_date.isoformat()}T{end_time.strftime('%H:%M')}:00",
            "timeZone": timezone,
        }
    return body


def _needs_update(existing: dict, new_body: dict, timezone: str = "Asia/Tokyo") -> bool:
    for key in ("summary", "description", "location", "colorId"):
        if existing.get(key) != new_body.get(key):
            return True
    for key in ("start", "end"):
        if not _same_endpoint(existing.get(key), new_body.get(key), timezone):
            return True
    return False


def _same_endpoint(existing: dict | None, new: dict | None, timezone: str) -> bool:
    """start/end が同じ時点を指しているかを比べる。

    Google APIは登録した dateTime をオフセット付き("...T10:00:00+09:00")で返す
    のに対し、こちらが組み立てる body は timeZone 別指定のナイーブな文字列なので、
    辞書をそのまま比較すると毎回「更新あり」と誤判定されてしまう。
    """
    if existing is None or new is None:
        return existing == new
    if "date" in existing or "date" in new:
        return existing.get("date") == new.get("date")
    return _to_instant(existing, timezone) == _to_instant(new, timezone)


def _to_instant(endpoint: dict, timezone: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    raw = endpoint.get("dateTime")
    if not raw:
        return None
    # Python 3.9 の fromisoformat は末尾 "Z" を解釈できない。
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(endpoint.get("timeZone") or timezone))
    return parsed
