from datetime import date

from school_pipeline.models import EventType, SchoolEvent, SourceRef
from school_pipeline.pipeline.cache import ExtractionCache
from school_pipeline.pipeline.runner import run_pipeline
from school_pipeline.sources.base import RawDocument


class FakeSource:
    def __init__(self, docs: list[RawDocument]):
        self._docs = docs

    def fetch(self):
        yield from self._docs


class FakeExtractor:
    def __init__(self, events_by_label: dict[str, list[SchoolEvent]]):
        self._events_by_label = events_by_label
        self.calls: list[str] = []

    def extract(self, text, *, reference_date, source=None):
        label = source.label if source else ""
        self.calls.append(label)
        events = self._events_by_label.get(label, [])
        for ev in events:
            ev.source = source
        return events


def _doc(label: str, text: str = "本文") -> RawDocument:
    return RawDocument(
        source=SourceRef(source_type="manual", source_id=label, label=label),
        text=text,
        reference_date=date(2026, 9, 1),
    )


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.QUIZ,
        title="小テスト",
        date=date(2026, 9, 10),
        subject="数学",
        confidence=0.9,
        description="",
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_run_pipeline_dedups_and_filters_low_confidence():
    doc_a = _doc("メールA")
    doc_b = _doc("メールB")
    extractor = FakeExtractor(
        {
            "メールA": [_event(confidence=0.9, description="担任より")],
            "メールB": [
                _event(confidence=0.9, description="教科担任より詳細範囲つき"),
                _event(subject="英語", confidence=0.2),
            ],
        }
    )

    result = run_pipeline([FakeSource([doc_a, doc_b])], extractor, confidence_threshold=0.5)

    assert len(result.events) == 1  # 数学の小テストは1件にdedupされる
    assert "教科担任より" in result.events[0].description
    assert len(result.low_confidence) == 1
    assert result.low_confidence[0].subject == "英語"


def test_run_pipeline_flags_possible_duplicates():
    doc = _doc("メールC")
    extractor = FakeExtractor(
        {
            "メールC": [
                _event(subject="国語", title="漢字テスト", confidence=0.9),
                _event(subject="社会", title="漢字テスト(範囲変更)", confidence=0.9),
            ],
        }
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert len(result.events) == 2
    assert len(result.possible_duplicates) == 1


def test_run_pipeline_continues_after_extraction_error():
    class FailingExtractor:
        def extract(self, text, *, reference_date, source=None):
            raise RuntimeError("boom")

    docs = [_doc("メールA")]
    result = run_pipeline([FakeSource(docs)], FailingExtractor(), confidence_threshold=0.5)

    assert result.events == []
    assert result.low_confidence == []


def test_run_pipeline_skips_extraction_when_cached_and_unchanged():
    doc = _doc("メールA", text="変わらない本文")
    extractor = FakeExtractor({"メールA": [_event(confidence=0.9)]})
    cache = ExtractionCache()

    result1 = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5, cache=cache)
    result2 = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5, cache=cache)

    assert extractor.calls == ["メールA"]  # 2回目はキャッシュがヒットして呼ばれない
    assert len(result1.events) == 1
    assert len(result2.events) == 1
    assert result2.events[0].title == "小テスト"


def test_run_pipeline_reextracts_when_content_changed():
    extractor = FakeExtractor(
        {
            "メールA": [_event(confidence=0.9, title="旧バージョン")],
        }
    )
    cache = ExtractionCache()
    doc_v1 = _doc("メールA", text="バージョン1")

    run_pipeline([FakeSource([doc_v1])], extractor, confidence_threshold=0.5, cache=cache)

    extractor._events_by_label["メールA"] = [_event(confidence=0.9, title="新バージョン")]
    doc_v2 = _doc("メールA", text="バージョン2(内容が変わった)")
    result = run_pipeline([FakeSource([doc_v2])], extractor, confidence_threshold=0.5, cache=cache)

    assert extractor.calls == ["メールA", "メールA"]  # 内容が変わったので再抽出される
    assert result.events[0].title == "新バージョン"


def test_run_pipeline_force_bypasses_cache():
    doc = _doc("メールA", text="変わらない本文")
    extractor = FakeExtractor({"メールA": [_event(confidence=0.9)]})
    cache = ExtractionCache()

    run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5, cache=cache)
    run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5, cache=cache, force=True)

    assert extractor.calls == ["メールA", "メールA"]


def test_long_all_day_events_lose_confidence():
    """日付が特定できていない予定が、期間まるごとの予定として登録されないこと。

    実例: 「9月中のどこか1日、抜き打ちで実施」という連絡が 9/1〜9/30 の
    1件として抽出され、カレンダー上で9月中ずっと表示され続けていた。
    """
    doc = _doc("規律検査")
    extractor = FakeExtractor(
        {
            "規律検査": [
                _event(
                    title="メイクチェック",
                    date=date(2026, 9, 1),
                    end_date=date(2026, 9, 30),
                    confidence=0.9,
                )
            ]
        }
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert result.events == []
    assert len(result.low_confidence) == 1
    assert result.low_confidence[0].confidence <= 0.3
    assert "30日間" in result.low_confidence[0].description


def test_genuinely_multi_day_events_are_left_alone():
    """2日間の文化祭や3日間の定期試験まで巻き添えにしないこと。"""
    doc = _doc("文化祭")
    extractor = FakeExtractor(
        {
            "文化祭": [
                _event(
                    type=EventType.EVENT,
                    title="文化祭",
                    date=date(2026, 10, 3),
                    end_date=date(2026, 10, 4),
                    confidence=0.9,
                )
            ]
        }
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert len(result.events) == 1
    assert result.events[0].confidence == 0.9


def test_exclusion_rules_remove_events_from_the_registration_set():
    from school_pipeline.pipeline.exclusions import ExclusionRule, ExclusionRules

    doc = _doc("規律検査")
    extractor = FakeExtractor(
        {
            "規律検査": [
                _event(title="メイクチェック", identity_key="メイクチェック"),
                _event(title="服装チェック", identity_key="服装チェック"),
            ]
        }
    )

    result = run_pipeline(
        [FakeSource([doc])],
        extractor,
        confidence_threshold=0.5,
        exclusions=ExclusionRules([ExclusionRule(title_contains="メイク", reason="該当しないため")]),
    )

    assert [e.title for e in result.events] == ["服装チェック"]
    assert len(result.excluded) == 1
    assert result.excluded[0][1].reason == "該当しないため"


def test_events_for_another_class_are_not_registered():
    """他クラス向けと明記された予定を取り込まないこと。"""
    doc = _doc("週末課題.pdf")
    extractor = FakeExtractor(
        {"週末課題.pdf": [_event(audience="1,2組", identity_key="B-1")]}
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert result.events == []
    assert len(result.other_class) == 1
    assert result.other_class[0].audience == "1,2組"


def test_class_specific_dates_in_one_notice_pick_our_class():
    """1つの連絡でクラスごとに日程が違う場合、自分のクラスのほうだけを採ること。

    実例: 数学の課題試験が「3・4・5・A組は9月7日、1・2組は9月8日」と案内された。
    どちらも同じ試験なので識別子は同じになり、対策が無いと片方が勝手に採用される。
    """
    doc = _doc("数学科からの連絡")
    extractor = FakeExtractor(
        {
            "数学科からの連絡": [
                _event(date=date(2026, 9, 7), identity_key="数学課題試験", audience="3・4・5・A組"),
                _event(date=date(2026, 9, 8), identity_key="数学課題試験", audience="1・2組"),
            ]
        }
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert len(result.events) == 1
    assert result.events[0].date == date(2026, 9, 7)
    assert result.conflicts == []
    assert len(result.other_class) == 1


def test_non_numeric_audiences_are_warned_not_dropped():
    """「B先生担当クラス」のような対象は機械的に判定できないので捨てない。"""
    doc = _doc("週末課題.pdf")
    extractor = FakeExtractor(
        {"週末課題.pdf": [_event(audience="B先生担当クラス", identity_key="B-1")]}
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert len(result.events) == 1
    assert result.other_class == []
    assert len(result.warnings) == 1
    assert "B先生担当クラス" in result.warnings[0]


def test_course_audiences_are_not_mistaken_for_class_numbers():
    """コース名(組の指定ではないもの)を他クラス扱いして落とさないこと。"""
    doc = _doc("文化祭のお知らせ")
    extractor = FakeExtractor(
        {"文化祭のお知らせ": [_event(audience="特進コース", identity_key="文化祭")]}
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert len(result.events) == 1
    assert result.other_class == []


def test_no_warning_when_our_class_is_listed():
    doc = _doc("週末課題 K.pdf")
    extractor = FakeExtractor(
        {"週末課題 K.pdf": [_event(audience="3, 4, 5, 12組", identity_key="B-1")]}
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert result.warnings == []


def _doc_dated(label: str, captured_at: str) -> RawDocument:
    return RawDocument(
        source=SourceRef(source_type="pdf", source_id=label, label=label, captured_at=captured_at),
        text="本文",
        reference_date=date.fromisoformat(captured_at),
    )


def test_events_from_a_different_school_year_lose_confidence():
    """年の取り違えを検出すること。

    実例: ファイル名の「250925」という数字列を根拠に、2026年度の行事が
    2025年として抽出された。曜日も日付も合っているので目視では気づきにくい。
    """
    doc = _doc_dated("お知らせ_250925.pdf", "2026-09-05")
    extractor = FakeExtractor(
        {"お知らせ_250925.pdf": [_event(date=date(2025, 10, 4), identity_key="文化祭")]}
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert result.events == []
    assert len(result.low_confidence) == 1
    assert "年の取り違え" in result.low_confidence[0].description


def test_past_events_within_the_same_school_year_are_untouched():
    """1学期の課題など、同じ年度内の過去の予定は巻き添えにしない。"""
    doc = _doc_dated("週末課題.pdf", "2026-09-05")
    extractor = FakeExtractor(
        {"週末課題.pdf": [_event(date=date(2026, 4, 20), identity_key="A-1")]}
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert len(result.events) == 1
    assert result.events[0].confidence == 0.9


def test_january_belongs_to_the_previous_school_year():
    """学校の年度は4月始まり。1月の予定は9月配布の資料と同じ年度。"""
    doc = _doc_dated("3学期の予定.pdf", "2026-09-05")
    extractor = FakeExtractor(
        {"3学期の予定.pdf": [_event(date=date(2027, 1, 15), identity_key="始業式")]}
    )

    result = run_pipeline([FakeSource([doc])], extractor, confidence_threshold=0.5)

    assert len(result.events) == 1
    assert result.events[0].confidence == 0.9


def test_grade_wide_audiences_do_not_warn():
    """学年全体向けの資料まで警告すると、肝心のクラス違いを読み飛ばしてしまう。"""
    doc = _doc("保護者向けチラシ.pdf")
    extractor = FakeExtractor(
        {"保護者向けチラシ.pdf": [_event(audience="中学1〜3年生", identity_key="説明会")]}
    )

    result = run_pipeline(
        [FakeSource([doc])], extractor, confidence_threshold=0.5, my_class="5組"
    )

    assert len(result.events) == 1
    assert result.warnings == []


def test_class_aliases_suppress_the_warning():
    """1クラスしかないコース名は自分のクラスとして扱う。"""
    doc = _doc("文化祭のお知らせ.pdf")
    extractor = FakeExtractor(
        {"文化祭のお知らせ.pdf": [_event(audience="特進コース保護者", identity_key="文化祭")]}
    )

    result = run_pipeline(
        [FakeSource([doc])],
        extractor,
        confidence_threshold=0.5,
        my_class="5組",
        my_class_aliases=["特進コース"],
    )

    assert len(result.events) == 1
    assert result.warnings == []
