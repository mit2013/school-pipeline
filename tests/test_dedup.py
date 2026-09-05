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

    result, conflicts = dedup_events([a, b])

    assert conflicts == []
    assert len(result) == 1
    merged = result[0]
    assert "範囲は教科書p1-50" in merged.description
    assert "担任からのメール" in merged.description
    assert merged.confidence == 0.9


def test_dedup_keeps_distinct_events_separate():
    a = _event(subject="数学")
    b = _event(subject="英語")
    result, _ = dedup_events([a, b])
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


def _dated(label: str, captured_at: str, **overrides) -> SchoolEvent:
    return _event(
        source=SourceRef(source_type="pdf", source_id=label, label=label, captured_at=captured_at),
        **overrides,
    )


def test_date_change_updates_the_same_event_instead_of_adding_one():
    """新しい資料で提出日が変わったら、1件のまま日付だけ差し替わること。

    実際にあったケース: 週末課題のプリントで9/7だったB-1が、あとから届いた
    教科担当のメールで9/14に変更された。
    """
    from_pdf = _dated("週末課題.pdf", "2026-09-01", date=date(2026, 9, 7), identity_key="B-1", subject="英語")
    from_mail = _dated("A先生メール", "2026-09-05", date=date(2026, 9, 14), identity_key="B-1", subject="英語")

    result, conflicts = dedup_events([from_pdf, from_mail])

    assert len(result) == 1
    assert result[0].date == date(2026, 9, 14)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "date"
    assert conflicts[0].chosen is result[0] or conflicts[0].chosen.date == date(2026, 9, 14)
    assert "9/7" in result[0].description and "9/14" in result[0].description


def test_conflicting_audiences_are_not_merged():
    """クラス別・担当者別に内容の違う資料を「変更」と誤認しないこと。

    週末課題プリントには「3,4,5,12組用」版とB先生担当クラス版があり、
    提出日が最大14日ずれていた。片方を勝手に採用すると間違った日程が入る。
    """
    ours = _dated(
        "週末課題 K.pdf", "2026-09-01", date=date(2026, 10, 7), identity_key="B-5",
        subject="英語", audience="3,4,5,12組",
    )
    theirs = _dated(
        "週末課題.pdf", "2026-09-02", date=date(2026, 10, 19), identity_key="B-5",
        subject="英語", audience="B先生担当クラス",
    )

    result, conflicts = dedup_events([ours, theirs])

    assert result == []  # どちらも登録しない
    assert len(conflicts) == 1
    assert conflicts[0].kind == "audience"
    assert conflicts[0].chosen is None


def test_matching_audiences_still_merge():
    a = _dated("案内1", "2026-09-01", date=date(2026, 9, 7), identity_key="B-1", audience="3,4,5,12組")
    b = _dated("案内2", "2026-09-02", date=date(2026, 9, 7), identity_key="B-1", audience="3, 4, 5, 12組")

    result, conflicts = dedup_events([a, b])

    assert len(result) == 1
    assert conflicts == []


def test_one_sided_audience_is_not_treated_as_a_conflict():
    """片方に記載が無いだけでは食い違いとみなさない(誤検出で止まりすぎないため)。"""
    stated = _dated("プリント", "2026-09-01", date=date(2026, 9, 7), identity_key="B-1", audience="3,4,5,12組")
    silent = _dated("メール", "2026-09-05", date=date(2026, 9, 14), identity_key="B-1")

    result, conflicts = dedup_events([stated, silent])

    assert len(result) == 1
    assert result[0].date == date(2026, 9, 14)
    assert conflicts[0].kind == "date"


def test_two_assignments_due_the_same_day_stay_separate():
    """同じ日に締め切られる別々の課題が、1件に潰れないこと。

    identity_key が無かった頃は 種別|科目|日付 が同じだと統合されてしまい、
    同日提出のB-1とB-2のうち片方が消えていた。
    """
    b1 = _dated("メール", "2026-09-05", date=date(2026, 9, 14), identity_key="B-1", subject="英語")
    b2 = _dated("メール", "2026-09-05", date=date(2026, 9, 14), identity_key="B-2", subject="英語")

    result, conflicts = dedup_events([b1, b2])

    assert len(result) == 2
    assert conflicts == []
