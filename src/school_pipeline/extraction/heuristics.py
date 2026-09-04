"""LLMによる抽出結果を軽く裏取りするための、日付表現に関する補助関数。

抽出のメインロジックはLLM側にあるが、ここでの関数は今後の検証機能
(例: 本文中に全く日付表現が見当たらないのに高い確信度が付いている、
といった食い違いの検出)のために用意している。
"""

from __future__ import annotations

import re
from datetime import date

_MD_RE = re.compile(
    r"(?:(?P<month1>\d{1,2})/(?P<day1>\d{1,2}))"
    r"|(?:(?P<month2>\d{1,2})月(?P<day2>\d{1,2})日)"
)

_RELATIVE_TERMS = (
    "来週",
    "今週",
    "明日",
    "明後日",
    "月曜",
    "火曜",
    "水曜",
    "木曜",
    "金曜",
    "土曜",
    "日曜",
)


def extract_explicit_dates(text: str, reference: date) -> list[date]:
    """本文中の "9/10" や "9月10日" のような明示的な日付表現を列挙する。

    年をまたぐ表現(例: 12月に配布された「1月」の予定)は、基準日より
    大きく過去にずれる場合に翌年とみなして補正する。
    """
    found: list[date] = []
    for m in _MD_RE.finditer(text):
        month = int(m.group("month1") or m.group("month2"))
        day = int(m.group("day1") or m.group("day2"))
        year = reference.year
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d < reference and (reference - d).days > 200:
            d = date(year + 1, month, day)
        found.append(d)
    return found


def has_relative_date_terms(text: str) -> bool:
    """「来週」「月曜」などの相対的な日付表現を含むかどうか。"""
    return any(term in text for term in _RELATIVE_TERMS)
