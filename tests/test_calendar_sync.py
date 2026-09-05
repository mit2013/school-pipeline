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
        self.deleted: list[str] = []
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

    def delete(self, calendarId, eventId):
        self.deleted.append(eventId)
        return _Execuable({})


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
        "extendedProperties": {
            "private": {
                "school_pipeline_id": event.stable_id,
                "school_pipeline_datekey": event.legacy_stable_id,
            }
        },
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


def test_orphans_are_reported_but_not_deleted_without_prune():
    """資料を一時的に退避しただけで予定が消えると困るので、既定では消さない。"""
    stale = _synced_existing(_event(date=date(2026, 9, 7)))
    service = FakeCalendarService(existing_items=[stale])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([_event(date=date(2026, 9, 15))], dry_run=False)

    assert len(stats.orphans) == 1
    assert stats.deleted == 0
    assert service.events().deleted == []


def test_prune_deletes_events_that_are_no_longer_extracted():
    stale = _synced_existing(_event(date=date(2026, 9, 7)))
    service = FakeCalendarService(existing_items=[stale])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([_event(date=date(2026, 9, 15))], dry_run=False, prune=True)

    assert stats.deleted == 1
    assert service.events().deleted == ["existing-id"]


def test_prune_dry_run_counts_but_does_not_delete():
    stale = _synced_existing(_event(date=date(2026, 9, 7)))
    service = FakeCalendarService(existing_items=[stale])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([_event(date=date(2026, 9, 15))], dry_run=True, prune=True)

    assert stats.deleted == 1
    assert service.events().deleted == []


def test_prune_keeps_hand_edited_events():
    """手で直された予定は、本人が必要としている可能性が高いので消さない。"""
    stale = _synced_existing(_event(date=date(2026, 9, 7)))
    stale["description"] = "範囲: 教科書p1-50\n持ち物メモ"  # 家族が追記した

    service = FakeCalendarService(existing_items=[stale])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([_event(date=date(2026, 9, 15))], dry_run=False, prune=True)

    assert stats.deleted == 0
    assert stats.skipped_manual == 1
    assert service.events().deleted == []


def test_events_still_extracted_are_never_orphans():
    event = _event()
    service = FakeCalendarService(existing_items=[_synced_existing(event)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False, prune=True)

    assert stats.orphans == []
    assert stats.deleted == 0


def test_date_change_moves_the_event_instead_of_creating_a_duplicate():
    """identity_key を持つ予定は、提出日が変わっても同じ予定を更新すること。"""
    before = _event(date=date(2026, 9, 7), identity_key="B-1", subject="英語")
    after = _event(date=date(2026, 9, 14), identity_key="B-1", subject="英語")
    service = FakeCalendarService(existing_items=[_synced_existing(before)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([after], dry_run=False, prune=True)

    assert (stats.created, stats.updated, stats.deleted) == (0, 1, 0)
    assert service.events().updated[0][1]["start"] == {"date": "2026-09-14"}


def _legacy_existing(event) -> dict:
    """identity_key 導入前の形式(旧IDのみ)で登録されている既存予定。"""
    from school_pipeline.calendar_sync.google_calendar import _to_calendar_body

    plain = _event(
        type=event.type, title=event.title, date=event.date, subject=event.subject,
        description=event.description, confidence=event.confidence,
    )
    body = _to_calendar_body(plain, "Asia/Tokyo")
    return {"id": "legacy-id", **body}


def test_legacy_ids_are_migrated_instead_of_recreated():
    """識別子の形式を変えたときに、既存の登録が全部作り直しにならないこと。

    作り直すと、家族が手で書き足した内容がその過程で失われる。
    """
    event = _event(identity_key="B-5", subject="英語")
    service = FakeCalendarService(existing_items=[_legacy_existing(event)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([event], dry_run=False, prune=True)

    assert (stats.created, stats.updated, stats.deleted) == (0, 1, 0)
    assert stats.orphans == []
    eventId, body = service.events().updated[0]
    assert eventId == "legacy-id"
    # 更新の際に新しい識別子が書き込まれ、次回からは移行済みになる。
    assert body["extendedProperties"]["private"]["school_pipeline_id"] == event.stable_id


def test_migration_does_not_resurrect_events_whose_date_moved():
    """日付が変わった予定は旧IDでは見つからないので、古いほうは削除候補に残る。"""
    moved = _event(date=date(2026, 9, 14), identity_key="B-1", subject="英語")
    stale = _legacy_existing(_event(date=date(2026, 9, 7), identity_key="B-1", subject="英語"))
    service = FakeCalendarService(existing_items=[stale])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([moved], dry_run=False)

    assert stats.created == 1
    assert len(stats.orphans) == 1


def test_keep_ids_protect_events_from_being_pruned():
    """確信度がわずかに下がっただけの予定を、削除してしまわないこと。

    抽出のたびに確信度は多少ぶれる。保護が無いと、しきい値付近の予定が
    実行のたびに登録と削除を繰り返すことになる。
    """
    borderline = _event(identity_key="漢字小テスト第1回", subject="現代文")
    service = FakeCalendarService(existing_items=[_synced_existing(borderline)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([], dry_run=False, prune=True, keep_ids={borderline.stable_id})

    assert stats.deleted == 0
    assert stats.orphans == []
    assert service.events().deleted == []


def test_unprotected_events_are_still_pruned():
    unwanted = _event(identity_key="メイクチェック")
    service = FakeCalendarService(existing_items=[_synced_existing(unwanted)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([], dry_run=False, prune=True, keep_ids={"別の予定のID"})

    assert stats.deleted == 1


def test_two_events_do_not_claim_the_same_legacy_entry():
    """旧IDが同じ2件が、1つの既存予定を取り合って片方消えないこと。

    旧IDは日付までしか見ていないため、同じ日に締め切られる B-3 と B-4 は
    同じ旧IDになる。先に取られていたら新規作成に回す必要がある。
    """
    b3 = _event(date=date(2026, 9, 28), identity_key="B-3", subject="英語", title="週末課題 B-3 提出")
    b4 = _event(date=date(2026, 9, 28), identity_key="B-4", subject="英語", title="週末課題 B-4 提出")
    assert b3.legacy_stable_id == b4.legacy_stable_id  # 前提の確認

    service = FakeCalendarService(existing_items=[_legacy_existing(b3)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([b3, b4], dry_run=False, prune=True)

    assert (stats.created, stats.updated, stats.deleted) == (1, 1, 0)
    assert len(service.events().inserted) == 1
    assert len(service.events().updated) == 1


def test_identity_key_drift_updates_instead_of_recreating():
    """LLMがキーの文言を変えても、同じ予定として引き継ぐこと。

    identity_key は毎回書き起こされるので「英単語テスト2周目」と
    「英単語テスト火曜チャレンジ2周目」のように揺れる。揺れるたびに
    別物と判定されると、同じ日に作り直しと削除が発生する。
    """
    before = _event(identity_key="英単語テスト2周目", subject="英語", date=date(2026, 9, 29))
    after = _event(identity_key="英単語テスト火曜チャレンジ2周目", subject="英語", date=date(2026, 9, 29))
    assert before.stable_id != after.stable_id  # 前提の確認

    service = FakeCalendarService(existing_items=[_synced_existing(before)])
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([after], dry_run=False, prune=True)

    assert (stats.created, stats.updated, stats.deleted) == (0, 1, 0)
    assert stats.orphans == []
    body = service.events().updated[0][1]
    assert body["extendedProperties"]["private"]["school_pipeline_id"] == after.stable_id


def test_drift_fallback_does_not_merge_different_events_on_one_day():
    """同じ日の別々の課題を、キーが揺れた同一予定と取り違えないこと。"""
    b3 = _event(date=date(2026, 9, 28), identity_key="B-3", subject="英語", title="B-3")
    b4 = _event(date=date(2026, 9, 28), identity_key="B-4", subject="英語", title="B-4")
    service = FakeCalendarService(existing_items=[_synced_existing(b3), _synced_existing(b4)])
    service.events()._existing_items[1]["id"] = "second-id"
    syncer = GoogleCalendarSync(service, calendar_id="primary")

    stats = syncer.sync([b3, b4], dry_run=False, prune=True)

    assert (stats.created, stats.deleted) == (0, 0)
    assert stats.orphans == []
