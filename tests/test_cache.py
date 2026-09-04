from datetime import date

from school_pipeline.models import EventType, SchoolEvent
from school_pipeline.pipeline.cache import ExtractionCache


def _event(**overrides) -> SchoolEvent:
    defaults = dict(
        type=EventType.QUIZ,
        title="漢字テスト",
        date=date(2026, 9, 10),
        subject="国語",
        confidence=0.9,
        description="範囲: p10-20",
    )
    defaults.update(overrides)
    return SchoolEvent(**defaults)


def test_get_returns_none_when_no_entry():
    cache = ExtractionCache()
    assert cache.get("pdf:foo.pdf", "somehash") is None


def test_put_then_get_round_trips_event_fields():
    cache = ExtractionCache()
    event = _event()
    content_hash = ExtractionCache.content_hash("本文テキスト")

    cache.put("pdf:foo.pdf", content_hash, [event])
    result = cache.get("pdf:foo.pdf", content_hash)

    assert result is not None
    assert len(result) == 1
    restored = result[0]
    assert restored.type == event.type
    assert restored.title == event.title
    assert restored.date == event.date
    assert restored.subject == event.subject
    assert restored.confidence == event.confidence
    assert restored.description == event.description


def test_get_returns_none_when_content_hash_changed():
    cache = ExtractionCache()
    old_hash = ExtractionCache.content_hash("旧本文")
    new_hash = ExtractionCache.content_hash("新本文")
    cache.put("pdf:foo.pdf", old_hash, [_event()])

    assert cache.get("pdf:foo.pdf", new_hash) is None


def test_content_hash_is_deterministic_and_sensitive_to_change():
    assert ExtractionCache.content_hash("同じ") == ExtractionCache.content_hash("同じ")
    assert ExtractionCache.content_hash("A") != ExtractionCache.content_hash("B")


def test_save_and_load_round_trip(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache = ExtractionCache()
    content_hash = ExtractionCache.content_hash("本文")
    cache.put("manual:note1.txt", content_hash, [_event()])
    cache.save(cache_path)

    reloaded = ExtractionCache.load(cache_path)
    result = reloaded.get("manual:note1.txt", content_hash)

    assert result is not None
    assert result[0].title == "漢字テスト"


def test_load_missing_file_returns_empty_cache(tmp_path):
    cache = ExtractionCache.load(tmp_path / "does_not_exist.json")
    assert cache.get("pdf:foo.pdf", "anyhash") is None


def test_load_corrupt_file_returns_empty_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not valid json", encoding="utf-8")

    cache = ExtractionCache.load(cache_path)

    assert cache.get("pdf:foo.pdf", "anyhash") is None
