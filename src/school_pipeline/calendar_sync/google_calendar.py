from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..models import EventType, SchoolEvent

logger = logging.getLogger(__name__)

_EXT_PROPERTY_KEY = "school_pipeline_id"
# 最後にパイプラインが書き込んだ内容の指紋。人が手で直したかどうかの判定に使う。
_EXT_SIGNATURE_KEY = "school_pipeline_synced"

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
    # 人が手で編集したため、上書きせず残した予定の件数。
    skipped_manual: int = 0


class GoogleCalendarSync:
    """SchoolEvent のリストをGoogleカレンダーへ冪等に同期する。

    各イベントの extendedProperties に stable_id を保存しておくことで、
    再実行しても重複登録せず、内容が変わった予定だけ更新する。
    """

    def __init__(
        self,
        service,
        calendar_id: str,
        timezone: str = "Asia/Tokyo",
        *,
        overwrite_manual_edits: bool = False,
    ) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._timezone = timezone
        self._overwrite_manual_edits = overwrite_manual_edits

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
            elif self._was_edited_by_hand(existing):
                # 家族が手で直した予定を、抽出結果で黙って戻さない。
                stats.skipped_manual += 1
                logger.info("[SKIP] %s %s (手動で編集されているため上書きしません)", event.date, event.title)
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

    def _was_edited_by_hand(self, existing: dict) -> bool:
        """カレンダー上の予定が、前回パイプラインが書いた内容から変わっているか。

        書き込み時に内容の指紋を extendedProperties に残しておき、次回それと
        実際の内容を突き合わせる。ズレていれば人が触ったということ。
        指紋を持たない予定(この仕組みの導入前に作られたもの)は、判断材料が
        ないのでパイプラインの管理下とみなす。
        """
        if self._overwrite_manual_edits:
            return False
        stored = existing.get("extendedProperties", {}).get("private", {}).get(_EXT_SIGNATURE_KEY)
        if not stored:
            return False
        return stored != _content_signature(existing, self._timezone)

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
    # 指紋は内容から計算するので、内容が固まった後に入れる。
    body["extendedProperties"]["private"][_EXT_SIGNATURE_KEY] = _content_signature(body, timezone)
    return body


def _content_signature(event: dict, timezone: str) -> str:
    """パイプラインが管理する項目だけを取り出した内容の指紋。

    Google APIは登録時と違う形(オフセット付きの dateTime など)で返してくるため、
    正規化してから計算する。こうすることで、こちらが組み立てた body と、APIから
    読み戻した予定とを同じ土俵で比較できる。
    """
    parts = [
        event.get("summary") or "",
        event.get("description") or "",
        event.get("location") or "",
        event.get("colorId") or "",
        _endpoint_key(event.get("start"), timezone),
        _endpoint_key(event.get("end"), timezone),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _endpoint_key(endpoint: dict | None, timezone: str) -> str:
    if not endpoint:
        return ""
    if "date" in endpoint:
        return "d:" + (endpoint.get("date") or "")
    instant = _to_instant(endpoint, timezone)
    return "t:" + (instant.isoformat() if instant else "")


def _needs_update(existing: dict, new_body: dict, timezone: str = "Asia/Tokyo") -> bool:
    """カレンダー上の予定を抽出結果に合わせて書き換える必要があるか。

    「手で編集されたか」の判定(_was_edited_by_hand)と同じ指紋を使う。別々の
    比較ロジックを持つと、片方だけが差分を認識してしまい、編集を保護したはずの
    予定が更新されるといった食い違いが起きうるため。
    """
    return _content_signature(existing, timezone) != _content_signature(new_body, timezone)


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
