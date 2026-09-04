from datetime import date

from school_pipeline.models import EventType, SchoolEvent, SourceRef
from school_pipeline.pipeline.dedup import dedup_events, find_possible_duplicates


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.EXAM,
        title="定期試験(数学)",
        date=date(2026, 9, 15),
        subject="数学",
        confidence=0.9,
        description="",
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_dedup_merges_same_stable_id_and_keeps_longer_description():
    a = _event(
        description="短い",
        source=SourceRef(source_type="gmail", source_id="m1", label="担任からのメール"),
    )
    b = _event(
        description="定期試験の範囲は教科書p1-50、電卓持ち込み不可。",
        source=SourceRef(source_type="pdf", source_id="p1", label="試験範囲表.pdf"),
    )

    result = dedup_events([a, b])

    assert len(result) == 1
    merged = result[0]
    assert "範囲は教科書p1-50" in merged.description
    assert "担任からのメール" in merged.description
    assert merged.confidence == 0.9


def test_dedup_keeps_distinct_events_separate():
    a = _event(subject="数学")
    b = _event(subject="英語")
    result = dedup_events([a, b])
    assert len(result) == 2


def test_find_possible_duplicates_flags_similar_titles_same_day():
    a = _event(type=EventType.QUIZ, subject="国語", title="漢字テスト", date=date(2026, 9, 10))
    b = _event(type=EventType.QUIZ, subject="社会", title="漢字テスト(範囲変更)", date=date(2026, 9, 10))

    pairs = find_possible_duplicates([a, b])

    assert len(pairs) == 1


def test_find_possible_duplicates_ignores_unrelated_events():
    a = _event(type=EventType.QUIZ, subject="国語", title="漢字テスト", date=date(2026, 9, 10))
    b = _event(type=EventType.EVENT, subject=None, title="運動会", date=date(2026, 9, 20))

    assert find_possible_duplicates([a, b]) == []
