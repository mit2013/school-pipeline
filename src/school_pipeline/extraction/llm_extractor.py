from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time
from typing import Protocol

from ..models import EventType, SchoolEvent, SourceRef
from ..pipeline.usage import UsageTotals
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
- 年が本文に書かれていない日付(「10月4日」など)の年は、基準日と学校の年度から判断してください。
  学校の年度は4月始まりで、4月〜12月は基準日の年度の開始年、1月〜3月はその翌年になります。
  出典のファイル名に含まれる数字列(「250925」など)を年の根拠にしないでください。
  それは資料の作成日や整理番号であることが多く、予定の年とは限りません。
- 実施日が特定できない予定(「9月中のどこか1日」「学期中に随時」「抜き打ちで実施」など)を、
  その期間全体にまたがる予定として登録しないでください。カレンダー上で毎日表示され続けて
  邪魔になるためです。日付が特定できないなら抽出しないか、どうしても残すなら1日だけの予定にして
  confidence を 0.3 以下にしてください。end_date は、行事が実際に複数日にわたって続く場合
  (2日間の文化祭、3日間の定期試験など)にだけ使ってください。
- 同じ日に複数の課題(B-3 と B-4 など)が締め切られる場合は、まとめて1件にせず、
  課題ごとに別々のイベントとして抽出してください。後から一部の課題だけ提出日が変更されることが
  あり、まとめてあると変更を追跡できなくなるためです。
- identity_key には、日付が変わっても同じ予定だと分かる番号や固有名(「B-1」など)を入れてください。
  同じ課題が別の資料では違う呼び方(「週末課題B-1」「長文B-1」)で書かれるので、呼称ではなく
  番号・記号を使うのが重要です。
- 「9月7日の週」のように週単位で指定された予定は、その週の起点の日付をそのまま実施日に
  しないでください。実際の実施日はその週の特定の曜日です。曜日が分かる手がかり(後述の
  補足情報など)があればその曜日の日付に直し、分からなければ週の起点の日付を使ったうえで
  confidence を 0.5 以下にしてください。
- audience には、その資料が誰向けかを本文から読み取って入れてください。クラス別・担当者別に
  内容の違う資料が配られることがあり、それらを取り違えないために使います。
- 1つの連絡の中でクラスごとに違う日程が示されている場合(「3・4・5・A組は9月7日、1・2組は
  9月8日」など)は、クラスごとに別々のイベントとして抽出し、それぞれの audience に対象クラスを
  必ず入れてください。ここを空にすると、どちらか一方が自分のクラスの予定として登録されて
  しまいます。identity_key は同じ(同じ試験なので)で構いません。
"""


class AnthropicLike(Protocol):
    model: str

    def create_message(self, *, system: str, user: str, tool_schema: dict) -> dict: ...


def build_system_prompt(notes: list | None = None) -> str:
    """設定に書かれた補足情報を、システムプロンプトの末尾に足す。

    時間割のように資料そのものには書かれていない前提(「現代文の小テストは金曜に実施」
    など)は、本文からは読み取れない。ここで渡さないと、「○日の週」といった書き方を
    実施日に変換できない。学校固有の情報なので、リポジトリではなく設定ファイルに置く。
    """
    if not notes:
        return SYSTEM_PROMPT
    lines = "\n".join(f"- {note}" for note in notes)
    return f"{SYSTEM_PROMPT}\n補足情報(この生徒についての前提。本文より優先して使ってよい):\n{lines}\n"


def extraction_fingerprint(model: str, notes: list | None = None) -> str:
    """抽出結果を左右する条件(モデル・プロンプト・ツール定義)の指紋。

    キャッシュはこれを本文のハッシュと一緒に持つ。プロンプトを1行直しただけでも
    指紋が変わってキャッシュが外れるため、改善が黙って無視されることがなくなる。
    モデルだけでなくプロンプトとスキーマも含めるのが要点で、実際に頻繁に変わるのは
    後者2つのほうである。
    """
    payload = json.dumps(
        {
            "model": model,
            "system_prompt": build_system_prompt(notes),
            "tool_schema": EVENT_TOOL_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class LLMExtractionError(RuntimeError):
    pass


class AnthropicExtractor:
    """Anthropic Claude を使った構造化イベント抽出器。"""

    def __init__(self, client: AnthropicLike, notes: list | None = None) -> None:
        self._client = client
        self._notes = list(notes or [])
        self._system_prompt = build_system_prompt(self._notes)
        # この実行で実際にAPIを呼んだ分の使用量(キャッシュヒットは含まれない)。
        self.usage = UsageTotals()

    @property
    def fingerprint(self) -> str:
        return extraction_fingerprint(getattr(self._client, "model", ""), self._notes)

    def extract(self, text: str, *, reference_date: date, source: SourceRef | None = None) -> list[SchoolEvent]:
        if not text.strip():
            return []
        context_label = source.label if source else "不明"
        user_prompt = (
            f"基準日(配布日/受信日): {reference_date.isoformat()}\n"
            f"出典: {context_label}\n\n"
            f"---本文---\n{text.strip()}\n---本文ここまで---"
        )
        response = self._client.create_message(
            system=self._system_prompt, user=user_prompt, tool_schema=EVENT_TOOL_SCHEMA
        )
        # 応答の解析に失敗しても課金は発生しているので、先に使用量を記録する。
        self.usage.add_response(response)
        audience, raw_events = _parse_tool_response(response)

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
            # 予定ごとの指定を優先し、無ければ資料全体の対象を引き継ぐ。
            event.audience = event.audience or audience
            events.append(event)
        return events


class AnthropicMessagesClient:
    """anthropic SDK を使った AnthropicLike の実装。"""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-5") -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def create_message(self, *, system: str, user: str, tool_schema: dict) -> dict:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
        )
        return response.model_dump()


def _normalize_events_field(events):
    """Claudeが record_events の 'events' を配列以外の形で返すことがある
    (文字列としてJSONエンコード、あるいは配列ではなくオブジェクトなど)。
    よくある崩れ方をここでまとめて配列に正規化する。
    """
    if isinstance(events, list):
        return events
    if isinstance(events, str):
        try:
            parsed = json.loads(events)
        except json.JSONDecodeError:
            return events
        return _normalize_events_field(parsed)
    if isinstance(events, dict):
        # 実際に本番で頻発したケース: "events" の中身が配列ではなく
        # {"events": [...]} というオブジェクト全体をもう一段階JSON文字列化
        # したもの(を↑でパースした結果)になっている。中の配列を取り出す。
        if "events" in events:
            return _normalize_events_field(events["events"])
        # 1件だけの予定を配列でなくオブジェクトそのもので返してしまったケース。
        if "type" in events and "date" in events:
            return [events]
        # {"0": {...}, "1": {...}} のように、配列ではなく添字付きの
        # オブジェクトとして返してしまったケース。
        if events and all(isinstance(k, str) and k.isdigit() for k in events):
            return [events[k] for k in sorted(events, key=int)]
    return events


def _parse_tool_response(response: dict) -> tuple[str | None, list[dict]]:
    """(audience, events) を返す。audience は資料全体が誰向けかの記述。"""
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
            tool_input = block.get("input", {}) or {}
            audience = tool_input.get("audience") or None
            raw_events_field = tool_input.get("events", [])
            events = _normalize_events_field(raw_events_field)
            if not isinstance(events, list):
                # 既知の崩れ方(文字列/単一オブジェクト/添字オブジェクト)の
                # どれにも当てはまらなかった。原因を切り分けられるよう、
                # 実際の中身をログに出しておく。
                logger.error("Unrecognized 'events' shape: %r", raw_events_field)
                raise LLMExtractionError(
                    f"record_events の 'events' がリスト形式ではありません(型: {type(events).__name__})。"
                    "詳細はログのUnrecognized 'events' shapeを参照してください。"
                )
            return audience, events
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
        identity_key=(raw.get("identity_key") or None),
        audience=(raw.get("audience") or None),
    )


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    return datetime.strptime(value, "%H:%M").time()
