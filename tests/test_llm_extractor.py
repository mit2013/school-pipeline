import json
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


def test_extract_skips_non_dict_event_entries_without_dropping_the_rest():
    # LLMの出力が壊れて "events" の要素が文字列など非オブジェクトになる
    # ケースがある(長い/複雑な文書でJSON生成が乱れた場合など)。
    # 1件が壊れていても、同じ文書内の他の正しいイベントは失われないことを確認する。
    good = {
        "type": "quiz",
        "title": "漢字テスト",
        "date": "2026-09-10",
        "description": "",
        "confidence": 0.9,
    }
    malformed = "2026-09-10"
    client = FakeAnthropicClient(_tool_response([malformed, good]))
    extractor = AnthropicExtractor(client)

    events = extractor.extract("本文", reference_date=date(2026, 9, 1))

    assert len(events) == 1
    assert events[0].title == "漢字テスト"


def test_extract_raises_one_clear_error_when_response_truncated():
    # max_tokens に達して応答が途中で切れると、tool_use の input が壊れたJSONに
    # なり "events" がリストではなくなることがある。以前はこれを1文字ずつ
    # イテレートして大量の warning ログを出していたので、1件のエラーとして
    # 検知できることを確認する。
    truncated_response = {
        "stop_reason": "max_tokens",
        "content": [
            {
                "type": "tool_use",
                "name": "record_events",
                "input": {"events": '...と推測。","subject":"英語","confid'},
            }
        ],
    }
    client = FakeAnthropicClient(truncated_response)
    extractor = AnthropicExtractor(client)

    with pytest.raises(LLMExtractionError, match="max_tokens"):
        extractor.extract("長い本文", reference_date=date(2026, 9, 1))


def test_extract_raises_when_events_field_is_not_a_list():
    client = FakeAnthropicClient(
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_events",
                    "input": {"events": "not-a-list"},
                }
            ]
        }
    )
    extractor = AnthropicExtractor(client)

    with pytest.raises(LLMExtractionError):
        extractor.extract("本文", reference_date=date(2026, 9, 1))


def test_extract_recovers_when_events_is_a_json_encoded_string():
    # Claudeがまれに "events" を(配列そのものではなく)配列をJSON文字列に
    # エンコードしたものとして返すことがある。実際に何度もこの形で返って
    # きたケースを再現し、救済できることを確認する。
    raw_event = {
        "type": "assignment",
        "title": "週末課題 A-1",
        "date": "2026-04-20",
        "description": "",
        "confidence": 0.9,
    }
    events_json_string = json.dumps([raw_event], ensure_ascii=False)
    client = FakeAnthropicClient(
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_events",
                    "input": {"events": events_json_string},
                }
            ]
        }
    )
    extractor = AnthropicExtractor(client)

    events = extractor.extract("本文", reference_date=date(2026, 9, 1))

    assert len(events) == 1
    assert events[0].title == "週末課題 A-1"


def test_extract_recovers_when_events_is_a_json_encoded_wrapper_object():
    # 実際に本番で頻発した崩れ方: "events" の中身が配列そのものではなく、
    # {"events": [...]} というトップレベルのオブジェクト全体をもう一段階
    # JSON文字列化した文字列になっている。
    raw_event = {
        "type": "assignment",
        "title": "英語長文チャレンジ B-1 提出",
        "date": "2026-09-07",
        "description": "",
        "confidence": 0.9,
    }
    wrapper_json_string = json.dumps({"events": [raw_event]}, ensure_ascii=False)
    client = FakeAnthropicClient(
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_events",
                    "input": {"events": wrapper_json_string},
                }
            ]
        }
    )
    extractor = AnthropicExtractor(client)

    events = extractor.extract("本文", reference_date=date(2026, 9, 1))

    assert len(events) == 1
    assert events[0].title == "英語長文チャレンジ B-1 提出"


def test_extract_recovers_when_events_is_a_single_object_not_wrapped_in_a_list():
    raw_event = {
        "type": "quiz",
        "title": "漢字テスト",
        "date": "2026-09-10",
        "description": "",
        "confidence": 0.9,
    }
    client = FakeAnthropicClient(
        {"content": [{"type": "tool_use", "name": "record_events", "input": {"events": raw_event}}]}
    )
    extractor = AnthropicExtractor(client)

    events = extractor.extract("本文", reference_date=date(2026, 9, 1))

    assert len(events) == 1
    assert events[0].title == "漢字テスト"


def test_extract_recovers_when_events_is_an_indexed_object_instead_of_an_array():
    # 実際に本番で観測した崩れ方: {"0": {...}, "1": {...}} のように、
    # 配列ではなく添字文字列をキーとするオブジェクトで返ってくることがある。
    event_0 = {"type": "assignment", "title": "A-1", "date": "2026-04-20", "description": "", "confidence": 0.9}
    event_1 = {"type": "assignment", "title": "A-2", "date": "2026-04-27", "description": "", "confidence": 0.9}
    client = FakeAnthropicClient(
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_events",
                    "input": {"events": {"1": event_1, "0": event_0}},
                }
            ]
        }
    )
    extractor = AnthropicExtractor(client)

    events = extractor.extract("本文", reference_date=date(2026, 9, 1))

    assert [e.title for e in events] == ["A-1", "A-2"]


def test_extract_raises_when_tool_call_missing():
    client = FakeAnthropicClient({"content": [{"type": "text", "text": "no tool call"}]})
    extractor = AnthropicExtractor(client)

    with pytest.raises(LLMExtractionError):
        extractor.extract("何か本文", reference_date=date(2026, 9, 1))
