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
            "audience": {
                "type": ["string", "null"],
                "description": (
                    "この資料が誰向けかを本文から読み取って書く(例: 「3,4,5,12組」「B先生担当クラス」"
                    "「特進コース」)。同じ種類の資料がクラス別・担当者別に別バージョンで"
                    "配られることがあり、それらを取り違えないために使う。本文に明記が無ければ null。"
                ),
            },
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
                        "identity_key": {
                            "type": ["string", "null"],
                            "description": (
                                "日付が変わってもこの予定を同じものだと特定できる短いキー。"
                                "課題番号・試験名など、その予定に固有の記号や番号を使う。"
                                "資料によって呼び方が違っても同じキーになるよう、呼称ではなく"
                                "番号・記号を中心に書くこと。"
                                "例: 「週末課題 B-1 提出」「長文B-1」「長文問題集のB-1」は"
                                "いずれも identity_key を「B-1」にする。"
                                "「2学期中間試験」は「2学期中間試験」、"
                                "「漢字小テスト第2回」は「漢字小テスト第2回」。"
                                "科目名は含めないこと(科目は subject に入れる)。"
                                "番号や固有の名前が無く、日付でしか区別できない予定は null。"
                            ),
                        },
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
                        "audience": {
                            "type": ["string", "null"],
                            "description": (
                                "この予定だけが特定のクラス向けである場合に、その対象を書く"
                                "(例: 「3・4・5・A組」)。1つの連絡の中でクラスごとに違う日程が"
                                "示されている場合は、必ず予定ごとにここを埋めること。"
                                "資料全体で共通なら null (その場合は全体の audience が使われる)。"
                            ),
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
