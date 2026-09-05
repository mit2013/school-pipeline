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


def test_stable_id_ignores_date_when_identity_key_is_present():
    """提出日が変わっても同じ予定だと分かること。

    これが成り立たないと、日付変更のたびに新しい予定が作られ、古い日付の
    予定がカレンダーに取り残される。
    """
    before = _event(date=date(2026, 9, 7), identity_key="B-1")
    after = _event(date=date(2026, 9, 14), identity_key="B-1")
    assert before.stable_id == after.stable_id


def test_stable_id_differs_for_different_identity_keys():
    a = _event(identity_key="B-1")
    b = _event(identity_key="B-2")
    assert a.stable_id != b.stable_id


def test_stable_id_separates_identity_keys_by_subject():
    """科目が違えば別物。英語のB-1と国語のB-1が混ざらないこと。"""
    english = _event(subject="英語", identity_key="B-1")
    japanese = _event(subject="国語", identity_key="B-1")
    assert english.stable_id != japanese.stable_id


def test_identity_key_absorbs_full_width_and_case_differences():
    """資料によって「B-1」「Ｂ－１」「b - 1」と揺れるので、揃えて扱う。"""
    plain = _event(identity_key="B-1")
    full_width = _event(identity_key="Ｂ－１")
    spaced = _event(identity_key="b - 1")
    assert plain.stable_id == full_width.stable_id == spaced.stable_id


def test_stable_id_falls_back_to_date_without_identity_key():
    a = _event(date=date(2026, 9, 7), identity_key=None)
    b = _event(date=date(2026, 9, 14), identity_key=None)
    assert a.stable_id != b.stable_id


def test_identity_is_none_without_identity_key():
    assert _event(identity_key=None).identity is None
    assert _event(subject="英語", identity_key="B-1").identity == "英語:b-1"


def test_audience_excludes_only_when_classes_are_enumerated():
    from school_pipeline.models import audience_excludes

    # 自分のクラスが列挙に無い → 他クラス向けと判定できる
    assert audience_excludes("1・2組", "5組")
    assert audience_excludes("1,2組", "5組")
    # 自分のクラスが含まれている
    assert not audience_excludes("3・4・5・A組", "5組")
    assert not audience_excludes("3, 4, 5, 12組", "5組")
    # クラス番号で書かれていない対象は判定しない(勝手に捨てない)
    assert not audience_excludes("特進コース", "5組")
    assert not audience_excludes("B先生担当クラス", "5組")
    # 設定が無ければ何も判定しない
    assert not audience_excludes("1・2組", None)
    assert not audience_excludes(None, "5組")
