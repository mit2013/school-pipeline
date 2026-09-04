"""Claude の tool-use による構造化イベント抽出用スキーマ。"""

EVENT_TOOL_SCHEMA = {
    "name": "record_events",
    "description": (
        "本文から読み取れる学校関連の予定(行事・小テスト・定期試験・課題提出など)を"
        "構造化して記録する。予定が1件も見つからない場合は空配列を返す。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["school_event", "quiz", "exam", "assignment", "other"],
                            "description": (
                                "school_event=学校行事, quiz=小テスト, exam=定期試験, "
                                "assignment=課題/提出物, other=その他"
                            ),
                        },
                        "title": {"type": "string", "description": "予定の短いタイトル"},
                        "date": {
                            "type": "string",
                            "description": (
                                "YYYY-MM-DD形式の開始日。相対表現(来週月曜など)は本文の"
                                "基準日から絶対日付に変換すること。"
                            ),
                        },
                        "end_date": {
                            "type": ["string", "null"],
                            "description": "複数日にわたる場合の終了日(YYYY-MM-DD)。単日ならnull。",
                        },
                        "start_time": {
                            "type": ["string", "null"],
                            "description": "HH:MM形式。終日/時刻不明ならnull。",
                        },
                        "end_time": {
                            "type": ["string", "null"],
                            "description": "HH:MM形式。不明ならnull。",
                        },
                        "subject": {
                            "type": ["string", "null"],
                            "description": "科目名(数学・英語など)。学校全体の行事はnull。",
                        },
                        "location": {"type": ["string", "null"]},
                        "description": {
                            "type": "string",
                            "description": "本文からの根拠となる短い要約(1〜2文)。",
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "0.0〜1.0。日付が本文に明記されている場合は高く、"
                                "相対表現からの推測や曖昧な場合は低くすること。"
                            ),
                        },
                    },
                    "required": ["type", "title", "date", "description", "confidence"],
                },
            }
        },
        "required": ["events"],
    },
}
