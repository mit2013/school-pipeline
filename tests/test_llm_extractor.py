from datetime import date, time

import pytest

from school_pipeline.extraction.llm_extractor import AnthropicExtractor, LLMExtractionError
from school_pipeline.models import EventType, SourceRef


class FakeAnthropicClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create_message(self, *, system: str, user: str, tool_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user, "tool_schema": tool_schema})
        return self.response


def _tool_response(events: list[dict]) -> dict:
    return {
        "content": [
            {
                "type": "tool_use",
                "name": "record_events",
                "input": {"events": events},
            }
        ]
    }


def test_extract_returns_empty_list_for_blank_text():
    client = FakeAnthropicClient(_tool_response([]))
    extractor = AnthropicExtractor(client)
    assert extractor.extract("   ", reference_date=date(2026, 9, 1)) == []
    assert client.calls == []


def test_extract_parses_events_and_attaches_source():
    raw_event = {
        "type": "quiz",
        "title": "漢字テスト",
        "date": "2026-09-10",
        "end_date": None,
        "start_time": None,
        "end_time": None,
        "subject": "国語",
        "location": None,
        "description": "来週水曜に実施予定",
        "confidence": 0.6,
    }
    client = FakeAnthropicClient(_tool_response([raw_event]))
    extractor = AnthropicExtractor(client)
    source = SourceRef(source_type="gmail", source_id="m1", label="国語科より")

    events = extractor.extract("来週水曜に漢字テストをします", reference_date=date(2026, 9, 1), source=source)

    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.QUIZ
    assert ev.title == "漢字テスト"
    assert ev.date == date(2026, 9, 10)
    assert ev.all_day is True
    assert ev.confidence == 0.6
    assert ev.source is source

    assert len(client.calls) == 1
    assert "2026-09-01" in client.calls[0]["user"]


def test_extract_parses_timed_event():
    raw_event = {
        "type": "school_event",
        "title": "保護者会",
        "date": "2026-09-20",
        "end_date": None,
        "start_time": "18:00",
        "end_time": "19:30",
        "subject": None,
        "location": "体育館",
        "description": "18:00より体育館にて",
        "confidence": 0.95,
    }
    client = FakeAnthropicClient(_tool_response([raw_event]))
    extractor = AnthropicExtractor(client)

    events = extractor.extract("保護者会のお知らせ", reference_date=date(2026, 9, 1))

    ev = events[0]
    assert ev.all_day is False
    assert ev.start_time == time(18, 0)
    assert ev.end_time == time(19, 30)
    assert ev.location == "体育館"


def test_extract_skips_malformed_events_without_failing():
    good = {
        "type": "assignment",
        "title": "提出物",
        "date": "2026-09-05",
        "description": "",
        "confidence": 0.8,
    }
    bad = {
        "type": "assignment",
        "title": "壊れたイベント",
        "date": "not-a-date",
        "description": "",
        "confidence": 0.8,
    }
    client = FakeAnthropicClient(_tool_response([bad, good]))
    extractor = AnthropicExtractor(client)

    events = extractor.extract("課題のお知らせ", reference_date=date(2026, 9, 1))

    assert len(events) == 1
    assert events[0].title == "提出物"


def test_extract_raises_when_tool_call_missing():
    client = FakeAnthropicClient({"content": [{"type": "text", "text": "no tool call"}]})
    extractor = AnthropicExtractor(client)

    with pytest.raises(LLMExtractionError):
        extractor.extract("何か本文", reference_date=date(2026, 9, 1))
