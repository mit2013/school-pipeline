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

    def __init__(self, service, calendar_id: str) -> None:
        self._service = service
        self._calendar_id = calendar_id

    def sync(self, events: list[SchoolEvent], *, dry_run: bool = True) -> SyncStats:
        existing_by_stable_id = self._load_existing()
        stats = SyncStats()
        for event in events:
            body = _to_calendar_body(event)
            existing = existing_by_stable_id.get(event.stable_id)
            if existing is None:
                stats.created += 1
                logger.info("[CREATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
            elif _needs_update(existing, body):
                stats.updated += 1
                logger.info("[UPDATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().update(
                        calendarId=self._calendar_id, eventId=existing["id"], body=body
                    ).execute()
            else:
                stats.unchanged += 1
        return stats

    def _load_existing(self) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        page_token = None
        # 直近登録済みの予定を検出できるよう、少し過去まで遡って取得する。
        time_min = (date.today() - timedelta(days=30)).isoformat() + "T00:00:00Z"
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


def _to_calendar_body(event: SchoolEvent) -> dict:
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
        body["start"] = {"dateTime": f"{event.date.isoformat()}T{event.start_time.strftime('%H:%M')}:00"}
        end_time = event.end_time or event.start_time
        body["end"] = {"dateTime": f"{end_date.isoformat()}T{end_time.strftime('%H:%M')}:00"}
    return body


def _needs_update(existing: dict, new_body: dict) -> bool:
    for key in ("summary", "description", "location", "start", "end", "colorId"):
        if existing.get(key) != new_body.get(key):
            return True
    return False
