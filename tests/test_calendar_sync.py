from datetime import date

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

    def list(self, **kwargs):
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
