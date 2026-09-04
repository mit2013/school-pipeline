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
