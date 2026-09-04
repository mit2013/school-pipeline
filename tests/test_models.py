from datetime import date

from school_pipeline.models import EventType, SchoolEvent, SourceRef


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.QUIZ,
        title="漢字テスト",
        date=date(2026, 9, 10),
        subject="国語",
        confidence=0.9,
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_stable_id_is_deterministic_for_same_key_fields():
    a = _event(title="漢字テスト")
    b = _event(title="漢字テスト")
    assert a.stable_id == b.stable_id


def test_stable_id_ignores_title_and_description():
    a = _event(title="漢字テスト", description="第3回")
    b = _event(title="漢字の小テスト", description="範囲: 教科書p10-20")
    assert a.stable_id == b.stable_id


def test_stable_id_differs_when_date_differs():
    a = _event(date=date(2026, 9, 10))
    b = _event(date=date(2026, 9, 11))
    assert a.stable_id != b.stable_id


def test_stable_id_differs_when_subject_differs():
    a = _event(subject="国語")
    b = _event(subject="数学")
    assert a.stable_id != b.stable_id


def test_needs_review_threshold():
    assert _event(confidence=0.4).needs_review is True
    assert _event(confidence=0.9).needs_review is False


def test_source_ref_holds_metadata():
    ref = SourceRef(source_type="gmail", source_id="msg1", label="件名", account="school_common")
    ev = _event()
    ev.source = ref
    assert ev.source.account == "school_common"
