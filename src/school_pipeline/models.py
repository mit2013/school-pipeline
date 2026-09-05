from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, time
from enum import Enum
from typing import Iterable


class EventType(str, Enum):
    EVENT = "school_event"
    QUIZ = "quiz"
    EXAM = "exam"
    ASSIGNMENT = "assignment"
    OTHER = "other"

    @property
    def label_ja(self) -> str:
        return {
            EventType.EVENT: "学校行事",
            EventType.QUIZ: "小テスト",
            EventType.EXAM: "定期試験",
            EventType.ASSIGNMENT: "課題",
            EventType.OTHER: "その他",
        }[self]


@dataclass
class SourceRef:
    """予定の出典(取得元)情報。"""

    source_type: str  # "gmail" | "pdf" | "image" | "manual"
    source_id: str  # メッセージID / ファイルパスなど、出典内で一意な識別子
    label: str  # 件名やファイル名など、人が読める表示名
    account: str | None = None  # gmail の場合のアカウント名
    captured_at: str | None = None  # 受信日/配布日 (ISO date)


@dataclass
class SchoolEvent:
    type: EventType
    title: str
    date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    all_day: bool = True
    subject: str | None = None  # 科目名。学校全体の行事などは None
    location: str | None = None
    description: str = ""
    confidence: float = 1.0
    source: SourceRef | None = None
    # 日付が変わっても「同じ予定」だと分かる識別子。課題番号や試験名など
    # (例: "B-1")。科目は含めず、stable_id を作るときに科目と組み合わせる。
    # 手がかりが無ければ None。
    identity_key: str | None = None
    # この予定の出典が誰向けの資料だったか (例: "3,4,5,12組")。クラス別・
    # 担当者別に内容の違う資料を「変更」と誤認しないための判断材料。
    audience: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.7

    @property
    def stable_id(self) -> str:
        """カレンダー上で同一の予定を指すための識別子。

        identity_key があれば日付を含めない。提出日が後から変更されても
        同じIDのままになるので、カレンダー上は新規登録ではなく更新になる。
        identity_key が無い予定は、従来どおり日付を含めて識別する。
        """
        if self.identity_key:
            return _digest("k", normalize_identity(self.subject), normalize_identity(self.identity_key))
        return self.legacy_stable_id

    @property
    def legacy_stable_id(self) -> str:
        """identity_key を導入する前の識別子(日付を含む)。

        すでにカレンダーへ登録済みの予定は、この形式のIDを持っている。同期時に
        こちらでも照合することで、識別子の変更が「全部消して入れ直し」にならず、
        手で編集された内容を保ったまま新しいIDへ移行できる。
        """
        # タイトルは意図的に含めない: LLMが同じ予定を毎回微妙に異なる文言で
        # 出力しても、type/subject/date が同じなら同一予定とみなして
        # カレンダー上で重複登録せず更新扱いにするため。
        return _digest(
            self.type.value,
            (self.subject or "").strip(),
            self.date.isoformat(),
            self.end_date.isoformat() if self.end_date else "",
            self.start_time.isoformat() if self.start_time else "",
        )

    @property
    def identity(self) -> str | None:
        """同一性の比較・表示に使う正規化済みキー。identity_key が無ければ None。"""
        if not self.identity_key:
            return None
        return f"{normalize_identity(self.subject)}:{normalize_identity(self.identity_key)}"


def normalize_identity(value: str | None) -> str:
    """表記ゆれを吸収する。全角/半角・大文字小文字・空白の違いを無視したい。

    資料によって「B-1」「Ｂ－１」「b - 1」のように揺れるため、これを揃えないと
    同じ課題が別IDになり、二重登録の原因になる。
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(normalized.split()).lower()


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def audience_matches(audience: str | None, my_class: str | None, aliases: Iterable[str] = ()) -> bool:
    """その対象の書き方が、自分のクラスを指していると分かるか。

    クラスは番号以外の呼び方でも書かれる。コースが1クラスしかない場合、
    コース名がそのままクラスの別名になる(設定の my_class_aliases で指定する)。
    """
    stated = normalize_identity(audience)
    if not stated:
        return False
    for name in [my_class, *aliases]:
        normalized = normalize_identity(name)
        if normalized and normalized in stated:
            return True
    return False


def audience_lists_my_class(audience: str | None, my_class: str | None) -> bool:
    """対象がクラスの列挙で書かれていて、その中に自分のクラスがあるか。

    「3, 4, 5, 12組」のような書き方から「5組が含まれる」と読み取るためのもの。
    列挙でない対象(「B先生担当クラス」など)には False を返す。
    """
    if not audience or not my_class:
        return False
    stated = normalize_identity(audience)
    if "組" not in stated:
        return False
    my_numbers = re.findall(r"\d+", normalize_identity(my_class))
    return bool(my_numbers) and my_numbers[0] in set(re.findall(r"\d+", stated))


def audience_excludes(audience: str | None, my_class: str | None, aliases: Iterable[str] = ()) -> bool:
    """その資料/予定の対象に、自分のクラスが含まれていないと言い切れるか。

    同じ連絡の中でクラスごとに違う日付が示されることがある(「3・4・5・A組は9月7日、
    1・2組は9月8日」など)。自分のクラス向けでないほうを取り込むと、そのまま
    間違った日程が登録されてしまう。

    判断できるのは対象がクラスの列挙になっている場合だけである。「B先生担当クラス」
    のようにクラス番号で書かれておらず、別名にも当てはまらない対象は、含まれるか
    どうかを機械的に判定できないので、除外しない(判断を人に残す)。
    """
    if not audience or not my_class:
        return False
    if audience_matches(audience, my_class, aliases):
        return False
    if audience_lists_my_class(audience, my_class):
        return False
    stated = normalize_identity(audience)
    if "組" not in stated:
        return False
    my_numbers = re.findall(r"\d+", normalize_identity(my_class))
    stated_numbers = set(re.findall(r"\d+", stated))
    if not my_numbers or not stated_numbers:
        return False
    return my_numbers[0] not in stated_numbers
