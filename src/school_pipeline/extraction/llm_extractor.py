from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Protocol

from ..models import EventType, SchoolEvent, SourceRef
from .schema import EVENT_TOOL_SCHEMA

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは私立中学校の保護者向けに、メールやプリントの本文から
学校行事・小テスト・定期試験・課題提出などの予定を抽出するアシスタントです。

ルール:
- 本文に具体的な日付(または曜日と週の手がかり)が書かれている予定だけを抽出してください。日付が全く特定できない予定は無視してください。
- 「来週」「今週」などの相対表現は、与えられた基準日(このメール/プリントが配布された日)から絶対日付に変換してください。
- 同じ本文内に複数の予定があれば、すべて別々のイベントとして抽出してください。
- 科目名が分かる場合は subject に入れてください。学校全体の行事は subject を null にしてください。
- 個々の予定の行に科目名が書かれていなくても、文書のタイトル・差出人・冒頭などから文書全体が特定の1科目に関するもの(例: 「英語科 週末課題スケジュール」というタイトルのプリント)だと分かる場合は、その科目名をすべての予定の subject に適用してください。特に、本文中のどこかに「英語科」「国語科」「数学科」のような「◯◯科」という表記があれば、それは文書全体の担当科目を表しているので、必ずその科目(例: 「英語科」→ subject は「英語」)を該当する予定すべてに適用してください。本文の冒頭だけでなく、本文全体を見て探してください。
- 予定のタイトルや出典(ファイル名・件名)に「英単語」「漢字」「英語長文」のように科目を明確に示す語が含まれている場合は、そこから科目(英語・国語など)を推測して subject に設定してください。ただし、そうした手がかりが本当に何もない場合(例: 単に「週末課題」とだけ書かれた、複数科目共通の課題)は、無理に推測せず subject を null のままにしてください。
- 確信が持てない(相対表現からの推測、日付が曖昧など)場合は confidence を下げてください。
- 予定ではない一般的な連絡(お知らせ、挨拶など)は抽出しないでください。
"""


class AnthropicLike(Protocol):
    def create_message(self, *, system: str, user: str, tool_schema: dict) -> dict: ...


class LLMExtractionError(RuntimeError):
    pass


class AnthropicExtractor:
    """Anthropic Claude を使った構造化イベント抽出器。"""

    def __init__(self, client: AnthropicLike) -> None:
        self._client = client

    def extract(self, text: str, *, reference_date: date, source: SourceRef | None = None) -> list[SchoolEvent]:
        if not text.strip():
            return []
        context_label = source.label if source else "不明"
        user_prompt = (
            f"基準日(配布日/受信日): {reference_date.isoformat()}\n"
            f"出典: {context_label}\n\n"
            f"---本文---\n{text.strip()}\n---本文ここまで---"
        )
        response = self._client.create_message(system=SYSTEM_PROMPT, user=user_prompt, tool_schema=EVENT_TOOL_SCHEMA)
        raw_events = _parse_tool_response(response)

        events: list[SchoolEvent] = []
        for raw in raw_events:
            try:
                if not isinstance(raw, dict):
                    raise ValueError(f"expected an object, got {type(raw).__name__}")
                event = _to_school_event(raw)
            except (KeyError, ValueError, TypeError) as exc:
                # 1件が壊れていても、同じ文書から取れた他の正しいイベントまで
                # 巻き添えで捨てないよう、ここでスキップして処理を続ける。
                logger.warning("Skipping malformed event from LLM output: %s (%r)", exc, raw)
                continue
            event.source = source
            events.append(event)
        return events


class AnthropicMessagesClient:
    """anthropic SDK を使った AnthropicLike の実装。"""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-5") -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def create_message(self, *, system: str, user: str, tool_schema: dict) -> dict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
        )
        return response.model_dump()


def _parse_tool_response(response: dict) -> list[dict]:
    if response.get("stop_reason") == "max_tokens":
        # 応答が途中で切れると tool_use の input が壊れたJSONになり、
        # "events" がリストではなく生テキストの断片になることがある。
        # そのまま1文字ずつイテレートして大量の警告を出す代わりに、
        # ここでまとめて1件のエラーとして扱う。
        raise LLMExtractionError(
            "Claudeの応答が max_tokens に達し、途中で切れました(本文が長すぎる可能性があります)"
        )
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "record_events":
            events = block.get("input", {}).get("events", [])
            if isinstance(events, str):
                # Claudeがまれに配列を二重にJSONエンコードした文字列として
                # 返すことがある。妥当なJSON配列であれば救済する。
                try:
                    events = json.loads(events)
                except json.JSONDecodeError:
                    pass
            if not isinstance(events, list):
                raise LLMExtractionError(
                    f"record_events の 'events' がリスト形式ではありません(型: {type(events).__name__})"
                )
            return events
    raise LLMExtractionError("record_events tool call not found in model response")


def _to_school_event(raw: dict) -> SchoolEvent:
    event_date = date.fromisoformat(raw["date"])
    end_date_raw = raw.get("end_date")
    end_date = date.fromisoformat(end_date_raw) if end_date_raw else None
    start_time = _parse_time(raw.get("start_time"))
    end_time = _parse_time(raw.get("end_time"))
    return SchoolEvent(
        type=EventType(raw["type"]),
        title=raw["title"].strip(),
        date=event_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        all_day=start_time is None,
        subject=(raw.get("subject") or None),
        location=(raw.get("location") or None),
        description=raw.get("description", "").strip(),
        confidence=float(raw.get("confidence", 0.5)),
    )


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    return datetime.strptime(value, "%H:%M").time()
