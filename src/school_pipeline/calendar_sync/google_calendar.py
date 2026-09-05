from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
    # 抽出結果に無くなったため削除した予定の件数。
    deleted: int = 0
    # パイプラインが登録したが、今回の抽出結果には無い予定(カレンダー上の生データ)。
    orphans: list = dataclass_field(default_factory=list)


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

    def sync(
        self,
        events: list[SchoolEvent],
        *,
        dry_run: bool = True,
        prune: bool = False,
        keep_ids: set | None = None,
    ) -> SyncStats:
        """抽出結果をカレンダーへ反映する。

        keep_ids には「今回は登録しないが、消してもいけない予定」の識別子を渡す。
        確信度がしきい値を少し下回っただけの予定がここに入る。抽出のたびに確信度は
        多少ぶれるので、これが無いと 0.5 前後の予定が登録と削除を交互に繰り返す。
        """
        existing_by_stable_id = self._load_existing(events)
        stats = SyncStats()
        # 実際に照合できた既存予定のキー。どれとも結びつかなかったものが孤児になる。
        matched_keys: set[str] = set()
        # 1件の既存予定を2件の抽出結果が取り合わないようにするための記録。
        claimed: set[str] = set()
        for event in events:
            body = _to_calendar_body(event, self._timezone)
            existing, matched_key = self._find_existing(existing_by_stable_id, event, claimed)
            if matched_key:
                matched_keys.add(matched_key)
            if existing is not None:
                claimed.add(existing["id"])
            if existing is None:
                stats.created += 1
                logger.info("[CREATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
            elif self._was_edited_by_hand(existing):
                # 家族が手で直した予定を、抽出結果で黙って戻さない。
                stats.skipped_manual += 1
                logger.info("[SKIP] %s %s (手動で編集されているため上書きしません)", event.date, event.title)
            elif matched_key != event.stable_id or _needs_update(existing, body, self._timezone):
                # 旧形式の識別子で見つけた場合は内容が同じでも書き戻す。そうしないと
                # 新しい識別子が保存されず、いつまでも移行が終わらない。
                stats.updated += 1
                logger.info("[UPDATE] %s %s", event.date, event.title)
                if not dry_run:
                    self._service.events().update(
                        calendarId=self._calendar_id, eventId=existing["id"], body=body
                    ).execute()
            else:
                stats.unchanged += 1

        self._collect_orphans(
            matched_keys | (keep_ids or set()),
            existing_by_stable_id,
            stats,
            dry_run=dry_run,
            prune=prune,
        )
        return stats

    def _find_existing(self, existing_by_stable_id: dict[str, dict], event: SchoolEvent, claimed: set):
        """カレンダー上の対応する予定を探す。(見つかった予定, 照合に使ったキー)。

        識別子の付け方を変えたときに、既存の登録が全部「別物」に見えてしまうと、
        削除して入れ直すことになり、手で編集された内容が失われる。そうならないよう、
        新しい識別子で見つからなければ旧形式の識別子でも探す。見つかれば更新扱いに
        なり、その際に新しい識別子が書き込まれるので、次回からは移行済みになる。

        旧形式の識別子は日付までしか見ていないので、同じ日に締め切られる別々の課題
        (B-3 と B-4 など)が同じ既存予定を指してしまう。先に取られていたら諦めて
        新規作成に回す。そうしないと片方がもう片方を上書きして消えてしまう。
        """
        found = existing_by_stable_id.get(event.stable_id)
        if found is not None and found["id"] not in claimed:
            return found, event.stable_id
        legacy = event.legacy_stable_id
        if legacy != event.stable_id:
            found = existing_by_stable_id.get(legacy)
            if found is not None and found["id"] not in claimed:
                logger.info("[MIGRATE] %s %s (識別子を新形式へ移行します)", event.date, event.title)
                return found, legacy
        return None, None

    def _collect_orphans(
        self,
        matched_keys: set,
        existing_by_stable_id: dict[str, dict],
        stats: SyncStats,
        *,
        dry_run: bool,
        prune: bool,
    ) -> None:
        """パイプラインが登録したのに、今回の抽出結果には無い予定を洗い出す。

        提出日が変わって古い日付の予定が取り残された場合や、除外ルールで
        外した場合、資料そのものを inbox から消した場合に発生する。
        報告は常に行い、削除は prune を指定したときだけ行う。資料を一時的に
        退避しただけで予定が消えると困るため、既定では消さない。
        """
        for stable_id, item in existing_by_stable_id.items():
            if stable_id in matched_keys:
                continue
            if self._was_edited_by_hand(item):
                # 手で直された予定は、本人が必要としている可能性が高いので残す。
                stats.skipped_manual += 1
                logger.info("[SKIP] %s (手動で編集されているため削除しません)", item.get("summary"))
                continue
            stats.orphans.append(item)
            if not prune:
                logger.info("[ORPHAN] %s (--prune を付けると削除します)", item.get("summary"))
                continue
            stats.deleted += 1
            logger.info("[DELETE] %s", item.get("summary"))
            if not dry_run:
                self._service.events().delete(calendarId=self._calendar_id, eventId=item["id"]).execute()

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
