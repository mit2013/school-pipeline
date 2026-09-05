from datetime import date, time

from school_pipeline.calendar_sync.google_calendar import GoogleCalendarSync
from school_pipeline.models import EventType, SchoolEvent


class _Execuable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeEventsResource:
    def __init__(self, existing_items: list[dict]):
        self._existing_items = existing_items
        self.inserted: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.list_kwargs: list[dict] = []

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        return _Execuable({"items": self._existing_items})

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return _Execuable({"id": "new-id"})

    def update(self, calendarId, eventId, body):
        self.updated.append((eventId, body))
        return _Execuable({"id": eventId})


class FakeCalendarService:
    def __init__(self, existing_items: list[dict]):
        self._events = FakeEventsResource(existing_items)

    def events(self):
        return self._events


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.EXAM,
        title="定期試験(数学)",
        date=date(2026, 9, 15),
        subject="数学",
        confidence=0.9,
        description="範囲: 教科書p1-50",
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_sync_creates_new_event_when_not_present():
    service = FakeCalendarService(existing_items=[])
    syncer = GoogleCalendarSync(service, calendar_id="primary")
    event = _event()

    stats = syncer.sync([event], dry_run=False)

    assert stats.created == 1
    assert stats.updated == 0
    assert stats.unchanged == 0
    assert len(service.events().inserted) == 1
    assert service.events().inserted[0]["extendedProperties"]["private"]["school_pipeline_id"] == event.stable_id


def test_sync_dry_run_does_not_write():
    service = FakeCalendarService(existing_items=[])
    syncer = GoogleCalendarSync(service, calendar_id="primary")
    event = _event()

    stats = syncer.sync([event], dry_run=True)

    assert stats.created == 1
    assert service.events().inserted == []


def test_sync_marks_unchanged_when_body_matches_existing():
    event = _event()
    from school_pipeline.calendar_sync.google_calendar import _to_calendar_body

    body = _to_calendar_body(event)
    existing_item = {
        "id": "existing-1",
        "extendedProperties": {"private": {"school_pipeline_id": event.stable_id}},
        **body,
    }
    service = FakeCalendarService(existing_items=[existing_item])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False)

    assert stats.unchanged == 1
    assert stats.created == 0
    assert stats.updated == 0


def test_sync_updates_when_description_changed():
    event = _event(description="旧: 教科書p1-30")
    from school_pipeline.calendar_sync.google_calendar import _to_calendar_body

    old_body = _to_calendar_body(_event(description="範囲未定"))
    existing_item = {
        "id": "existing-1",
        "extendedProperties": {"private": {"school_pipeline_id": event.stable_id}},
        **old_body,
    }
    service = FakeCalendarService(existing_items=[existing_item])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False)

    assert stats.updated == 1
    assert len(service.events().updated) == 1
    updated_event_id, updated_body = service.events().updated[0]
    assert updated_event_id == "existing-1"
    assert updated_body["description"] == "旧: 教科書p1-30"


def _timed_event() -> SchoolEvent:
    return _event(
        type=EventType.EVENT,
        title="アントレプレナーシップ教育プログラム",
        date=date(2026, 10, 11),
        subject=None,
        start_time=time(10, 0),
        end_time=time(17, 0),
        all_day=False,
    )


def test_timed_event_body_carries_timezone():
    """timeZone が無いと Google API が 400 Missing time zone definition を返す。"""
    service = FakeCalendarService(existing_items=[])
    syncer = GoogleCalendarSync(service, calendar_id="primary", timezone="Asia/Tokyo")

    syncer.sync([_timed_event()], dry_run=False)

    body = service.events().inserted[0]
    assert body["start"] == {"dateTime": "2026-10-11T10:00:00", "timeZone": "Asia/Tokyo"}
    assert body["end"] == {"dateTime": "2026-10-11T17:00:00", "timeZone": "Asia/Tokyo"}


def test_timed_event_is_unchanged_on_rerun():
    """APIはオフセット付きで返すので、素朴な辞書比較だと毎回「更新」になってしまう。"""
    event = _timed_event()
    existing = {
        "id": "existing-id",
        "summary": "[学校行事] アントレプレナーシップ教育プログラム",
        "description": event.description,
        "colorId": "9",
        "start": {"dateTime": "2026-10-11T10:00:00+09:00", "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": "2026-10-11T17:00:00+09:00", "timeZone": "Asia/Tokyo"},
        "extendedProperties": {"private": {"school_pipeline_id": event.stable_id}},
    }
    service = FakeCalendarService(existing_items=[existing])
    syncer = GoogleCalendarSync(service, calendar_id="primary", timezone="Asia/Tokyo")

    stats = syncer.sync([event], dry_run=False)

    assert (stats.created, stats.updated, stats.unchanged) == (0, 0, 1)
    assert service.events().updated == []


def test_existing_lookup_reaches_back_to_oldest_event():
    """過去の予定を含めて同期するとき、既存検索の窓が狭いと毎回重複登録される。"""
    service = FakeCalendarService(existing_items=[])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    syncer.sync([_event(date=date(2026, 4, 20)), _event(date=date(2026, 11, 24))], dry_run=True)

    time_min = service.events().list_kwargs[0]["timeMin"]
    assert time_min < "2026-04-20", f"最古の予定より前まで遡っていない: {time_min}"


def _synced_existing(event, **overrides) -> dict:
    """パイプラインが書き込んだ直後の状態(指紋付き)をAPIの返す形で組み立てる。"""
    from school_pipeline.calendar_sync.google_calendar import _to_calendar_body

    body = _to_calendar_body(event, "Asia/Tokyo")
    existing = {"id": "existing-id", **body}
    existing.update(overrides)
    return existing


def test_hand_edited_event_is_not_overwritten():
    """妻が手で直した予定を、次の実行で抽出結果に戻してしまわないこと。"""
    event = _event()
    existing = _synced_existing(event)
    existing["description"] = "範囲: 教科書p1-50\n体操服いる"  # 家族が追記した

    service = FakeCalendarService(existing_items=[existing])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False)

    assert stats.skipped_manual == 1
    assert stats.updated == 0
    assert service.events().updated == []


def test_untouched_event_still_updates_when_the_source_changes():
    """手つかずの予定は、プリントの内容が変わればこれまで通り更新する。"""
    event = _event()
    existing = _synced_existing(event)

    service = FakeCalendarService(existing_items=[existing])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    changed = _event(title="定期試験(数学) ※範囲変更")
    stats = syncer.sync([changed], dry_run=False)

    assert stats.updated == 1
    assert stats.skipped_manual == 0


def test_untouched_and_unchanged_event_is_left_alone():
    event = _event()
    service = FakeCalendarService(existing_items=[_synced_existing(event)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False)

    assert (stats.created, stats.updated, stats.unchanged, stats.skipped_manual) == (0, 0, 1, 0)


def test_overwrite_flag_forces_hand_edited_events_back():
    event = _event()
    existing = _synced_existing(event)
    existing["summary"] = "家族が変えたタイトル"

    service = FakeCalendarService(existing_items=[existing])
    syncer = GoogleCalendarSync(
        service, calendar_id="primary", overwrite_manual_edits=True
    )

    stats = syncer.sync([event], dry_run=False)

    assert stats.updated == 1
    assert stats.skipped_manual == 0


def test_events_without_a_signature_stay_pipeline_managed():
    """この仕組みの導入前に作られた予定は、判断材料がないので従来通り更新する。"""
    event = _event()
    existing = _synced_existing(event)
    del existing["extendedProperties"]["private"]["school_pipeline_synced"]
    existing["summary"] = "古い形式のまま残っている予定"

    service = FakeCalendarService(existing_items=[existing])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False)

    assert stats.updated == 1
    assert stats.skipped_manual == 0


def test_new_events_are_written_with_a_signature():
    service = FakeCalendarService(existing_items=[])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    syncer.sync([_event()], dry_run=False)

    private = service.events().inserted[0]["extendedProperties"]["private"]
    assert private["school_pipeline_synced"]
