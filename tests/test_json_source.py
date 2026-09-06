import json
from datetime import date, time

import pytest

from school_pipeline.models import EventType
from school_pipeline.sources.json_source import (
    ExtractedEventsError,
    JsonEventsSource,
    load_extracted_events,
)


def _write(tmp_path, payload, name="doc.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_reads_a_minimal_event(tmp_path):
    path = _write(tmp_path, {"events": [{"type": "assignment", "title": "B-1 提出", "date": "2026-09-14"}]})
    events = load_extracted_events(path)

    assert len(events) == 1
    assert events[0].type is EventType.ASSIGNMENT
    assert events[0].date == date(2026, 9, 14)
    assert events[0].confidence == 0.9  # 省略時の既定
    assert events[0].all_day is True


def test_reads_every_field(tmp_path):
    path = _write(tmp_path, {
        "source": {"label": "週末課題.pdf", "source_id": "p1", "captured_at": "2026-09-05"},
        "audience": "3,4,5組",
        "events": [{
            "type": "school_event", "title": "特別プログラム", "identity_key": "AP-1",
            "date": "2026-10-11", "end_date": "2026-10-12",
            "start_time": "10:00", "end_time": "17:00",
            "subject": "英語", "location": "体育館",
            "description": "説明", "confidence": 0.8,
        }],
    })
    event = load_extracted_events(path)[0]

    assert (event.start_time, event.end_time) == (time(10, 0), time(17, 0))
    assert event.all_day is False
    assert event.end_date == date(2026, 10, 12)
    assert (event.location, event.subject, event.confidence) == ("体育館", "英語", 0.8)
    assert event.source.label == "週末課題.pdf"
    assert event.source.captured_at == "2026-09-05"
    assert event.audience == "3,4,5組"


def test_event_audience_overrides_the_document_one(tmp_path):
    """1つの連絡でクラスごとに日程が違う場合、予定ごとの指定を優先する。"""
    path = _write(tmp_path, {
        "audience": "2年生",
        "events": [
            {"type": "exam", "title": "課題試験", "date": "2026-09-07", "audience": "3・4・5組"},
            {"type": "exam", "title": "課題試験", "date": "2026-09-08"},
        ],
    })
    events = load_extracted_events(path)
    assert [e.audience for e in events] == ["3・4・5組", "2年生"]


def test_identity_key_matches_other_sources(tmp_path):
    """JSON由来の予定が、Excelやプリント由来の同じ課題と突き合うこと。"""
    from school_pipeline.models import SchoolEvent

    path = _write(tmp_path, {"events": [{
        "type": "assignment", "title": "週末課題 B-1 提出",
        "date": "2026-09-14", "subject": "英語", "identity_key": "B-1",
    }]})
    from_json = load_extracted_events(path)[0]
    other = SchoolEvent(
        type=EventType.ASSIGNMENT, title="長文B-1",
        date=date(2026, 9, 14), subject="英語", identity_key="b-1",
    )
    assert from_json.stable_id == other.stable_id


def test_source_falls_back_to_the_filename(tmp_path):
    path = _write(tmp_path, {"events": []}, name="川本先生の連絡.json")
    source = JsonEventsSource(tmp_path)
    assert source.fetch_events() == []
    events = load_extracted_events(_write(
        tmp_path, {"events": [{"type": "other", "title": "x", "date": "2026-09-01"}]},
        name="連絡メモ.json",
    ))
    assert events[0].source.label == "連絡メモ"


@pytest.mark.parametrize("payload, message", [
    ({"events": [{"title": "x", "date": "2026-09-01"}]}, "'type' が不正"),
    ({"events": [{"type": "nope", "title": "x", "date": "2026-09-01"}]}, "'type' が不正"),
    ({"events": [{"type": "quiz", "date": "2026-09-01"}]}, "'title' は必須"),
    ({"events": [{"type": "quiz", "title": "x"}]}, "'date' は必須"),
    ({"events": [{"type": "quiz", "title": "x", "date": "9/1"}]}, "YYYY-MM-DD"),
    ({"events": [{"type": "quiz", "title": "x", "date": "2026-09-01", "start_time": "10時"}]}, "HH:MM"),
    ({"events": [{"type": "quiz", "title": "x", "date": "2026-09-01", "confidence": 5}]}, "0.0〜1.0"),
    ({"events": "たくさん"}, "'events' は配列"),
    ([], "最上位はオブジェクト"),
])
def test_bad_input_says_what_is_wrong(tmp_path, payload, message):
    """人が手で書く形式なので、どこがどう悪いか読んで直せること。"""
    path = _write(tmp_path, payload)
    with pytest.raises(ExtractedEventsError) as excinfo:
        load_extracted_events(path)
    assert message in str(excinfo.value)


def test_error_names_the_offending_event(tmp_path):
    path = _write(tmp_path, {"events": [
        {"type": "quiz", "title": "良い", "date": "2026-09-01"},
        {"type": "quiz", "title": "悪い", "date": "だめ"},
    ]})
    with pytest.raises(ExtractedEventsError) as excinfo:
        load_extracted_events(path)
    assert "events[1]" in str(excinfo.value)


def test_one_broken_file_does_not_hide_the_others(tmp_path, caplog):
    """1ファイル壊れていても他は取り込む。ただし黙って減らさない。"""
    _write(tmp_path, {"events": [{"type": "quiz", "title": "良い", "date": "2026-09-01"}]}, name="ok.json")
    _write(tmp_path, {"events": [{"type": "quiz", "title": "悪い", "date": "だめ"}]}, name="ng.json")

    with caplog.at_level("ERROR"):
        events = JsonEventsSource(tmp_path).fetch_events()

    assert [e.title for e in events] == ["良い"]
    assert "ng.json" in caplog.text


def test_invalid_json_is_reported(tmp_path, caplog):
    (tmp_path / "broken.json").write_text("{ぜんぜんJSONじゃない", encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert JsonEventsSource(tmp_path).fetch_events() == []
    assert "broken.json" in caplog.text


def test_missing_directory_is_not_an_error(tmp_path):
    assert JsonEventsSource(tmp_path / "none").fetch_events() == []
