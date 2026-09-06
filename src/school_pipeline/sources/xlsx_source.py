from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from pathlib import Path

from ..models import EventType, SchoolEvent, SourceRef

logger = logging.getLogger(__name__)

# 予定として扱う列と、そこから作るイベントの種別。
# 「進度予定」「進度結果」は授業の進み具合の記録であって予定ではないので読まない。
# それらの列にはExcelが「7-2」を日付に自動変換した値が混ざっているが、
# 読まない以上そもそも問題にならない。
_EVENT_COLUMNS = {
    "提出": EventType.ASSIGNMENT,
    "小テスト": EventType.QUIZ,
    "チャレ": EventType.QUIZ,
}

_WEEKDAYS = "月火水木金土日"
_MONTH_PATTERN = re.compile(r"^\s*(\d{1,2})\s*月")
# 「B-1」「Ex2」「NTノートL1」のような、課題や小テストを一意に指す記号。
_CODE_PATTERN = re.compile(r"^(?P<prefix>[^\d]*?)(?P<letters>[A-Za-z]+)\s*-?\s*(?P<number>\d+)$")
_SEGMENT_SEPARATORS = re.compile(r"[・･／/]+")
_ITEM_SEPARATORS = re.compile(r"[、,]+")


class XlsxSource:
    """教科ごとの授業予定表(Excel)から、提出物と小テストの予定を読み取る。

    この表は1行が1日に対応していて日付が確定しているため、LLMに渡す必要がない。
    列を決め打ちで読めば費用ゼロで、しかも解釈のぶれが起きない。

    クラスごとにシートが分かれていることがあるので、`sheets_include` で
    自分のクラスのシートだけを対象にする。PDFを `_excluded/` へ退避させるのと
    同じ考え方で、他クラスの日程を取り込む事故を防ぐ。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        sheets_include: list | None = None,
        subject: str | None = None,
    ) -> None:
        self._directory = Path(directory)
        self._sheets_include = list(sheets_include or [])
        self._subject = subject

    def fetch_events(self) -> list[SchoolEvent]:
        if not self._directory.exists():
            return []
        try:
            import openpyxl
        except ImportError:
            logger.warning("openpyxl が未インストールのため %s を読み飛ばします", self._directory)
            return []

        events: list[SchoolEvent] = []
        for path in sorted(self._directory.glob("*.xlsx")):
            workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
            reference = date.fromtimestamp(path.stat().st_mtime)
            subject = self._subject or _subject_from_filename(path.name)
            for sheet in workbook.worksheets:
                if not self._wanted(sheet.title):
                    continue
                events.extend(_read_sheet(sheet, path=path, reference=reference, subject=subject))
            workbook.close()
        return events

    def _wanted(self, sheet_title: str) -> bool:
        if not self._sheets_include:
            return True
        title = _normalize(sheet_title)
        return any(_normalize(pattern) in title for pattern in self._sheets_include)


def _read_sheet(sheet, *, path: Path, reference: date, subject: str | None) -> list[SchoolEvent]:
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    layout = _find_layout(rows)
    if layout is None:
        # 想定した列が無いのに黙って空を返すと、取り込めていないことに気づけない。
        logger.warning(
            "%s の「%s」から想定した列(日/曜日/提出・小テスト)が見つからず、読み飛ばしました",
            path.name,
            sheet.title,
        )
        return []

    header_row, columns, day_col, weekday_col, month_col = layout
    fiscal_year = reference.year if reference.month >= 4 else reference.year - 1
    source = SourceRef(
        source_type="xlsx",
        source_id=f"{path}#{sheet.title}",
        label=f"{path.name}({sheet.title})",
        captured_at=reference.isoformat(),
    )

    events: list[SchoolEvent] = []
    month: int | None = None
    for row in rows[header_row + 1 :]:
        month = _month_of(row, month_col) or month
        when = _date_of(row, day_col, weekday_col, month, fiscal_year, path, sheet.title)
        if when is None:
            continue
        for column, event_type in columns.items():
            text = _text(row, column)
            if not text or _is_note_only(text):
                continue
            for title, identity in _items_of(text, event_type):
                events.append(
                    SchoolEvent(
                        type=event_type,
                        title=title,
                        date=when,
                        subject=subject,
                        identity_key=identity,
                        description=f"[出典: {source.label}]",
                        # 表から直接読んだ値なので、LLMの読み取りより確度は高い。
                        confidence=0.9,
                        source=source,
                    )
                )
    return events


def _items_of(text: str, event_type: EventType) -> list:
    """セルの文字列から (タイトル, 識別子) を作る。種別で扱いを変える。

    提出物は他の資料(プリントやメール)にも同じ課題が載るので、課題番号だけを
    識別子にして突き合わせられるようにする。「長文B-1, 2」のような省略形も
    B-1 と B-2 に展開する。

    小テストは逆に、この表以外に出典が無く、しかも同じ名前が周回で再登場する
    (「火チャレUnit 1」が1周目と2周目の両方にある)。番号を識別子にすると別の回が
    同じ予定と判定されるので、識別子は付けず日付で区別する。
    """
    if event_type is EventType.ASSIGNMENT:
        return _split_items(text)
    return [(text, None)]


def _is_note_only(text: str) -> bool:
    """「(2周目)」のような注記だけのセルは予定ではない。"""
    return bool(re.fullmatch(r"[(（][^)）]*[)）]", text.strip()))


def _find_layout(rows: list):
    """ヘッダ行と、必要な列の位置を探す。

    「日」「曜日」は表の左右に2組あることがあるので、最初に現れたほうを使う。
    """
    for index, row in enumerate(rows[:20]):
        headers = {_normalize(cell): position for position, cell in enumerate(row) if _normalize(cell)}
        columns = {
            headers[_normalize(name)]: event_type
            for name, event_type in _EVENT_COLUMNS.items()
            if _normalize(name) in headers
        }
        day_col = _first_index(row, "日")
        weekday_col = _first_index(row, "曜日")
        if columns and day_col is not None and weekday_col is not None:
            # 月は「4月」のように、日付列のすぐ左に月が変わる行だけ入っている。
            month_col = day_col - 1 if day_col > 0 else None
            return index, columns, day_col, weekday_col, month_col
    return None


def _first_index(row: list, name: str) -> int | None:
    target = _normalize(name)
    for position, cell in enumerate(row):
        if _normalize(cell) == target:
            return position
    return None


def _month_of(row: list, month_col: int | None) -> int | None:
    if month_col is None:
        return None
    match = _MONTH_PATTERN.match(_text(row, month_col))
    if not match:
        return None
    month = int(match.group(1))
    return month if 1 <= month <= 12 else None


def _date_of(row, day_col, weekday_col, month, fiscal_year, path, sheet_title):
    """行の日付を組み立てる。曜日欄と一致しない行は捨てる。

    セルに年が入っていないので、学校の年度(4月始まり)から補う。曜日と突き合わせる
    ことで、年の取り違えや行のずれをその場で検出できる。
    """
    if month is None:
        return None
    day = _int(row, day_col)
    if day is None:
        return None
    year = fiscal_year if month >= 4 else fiscal_year + 1
    try:
        when = date(year, month, day)
    except ValueError:
        return None

    stated = _text(row, weekday_col)[:1]
    if stated and stated in _WEEKDAYS and _WEEKDAYS[when.weekday()] != stated:
        logger.warning(
            "%s の「%s」: %s は%s曜日ですが表では%s曜日です。この行は読み飛ばしました",
            path.name,
            sheet_title,
            when.isoformat(),
            _WEEKDAYS[when.weekday()],
            stated,
        )
        return None
    return when


def _split_items(text: str) -> list:
    """セルの文字列を (タイトル, 識別子) の並びに分ける。

    「長文B-1, 2」のように、記号の頭を共有した省略形で書かれることがある。
    これを B-1 と B-2 に展開しておかないと、他の資料に載っている同じ課題と
    突き合わせられない。番号の付いていない項目はそのまま1件として扱う。
    """
    items: list = []
    for segment in _SEGMENT_SEPARATORS.split(text):
        segment = segment.strip()
        if not segment:
            continue
        label = ""
        letters = ""
        for piece in _ITEM_SEPARATORS.split(segment):
            piece = piece.strip()
            if not piece:
                continue
            match = _CODE_PATTERN.match(piece)
            if match:
                label = match.group("prefix").strip() or label
                letters = match.group("letters")
                code = f"{letters}-{match.group('number')}"
            elif letters and piece.isdigit():
                # 「B-1, 2」の「2」。直前の記号を引き継ぐ。
                code = f"{letters}-{piece}"
            else:
                items.append((piece, _normalize(piece)))
                continue
            items.append((f"{label}{code}" if label else code, _normalize(code)))
    return items


def _subject_from_filename(name: str) -> str | None:
    for subject in ("英語", "数学", "国語", "理科", "社会", "現代文", "古文", "漢文"):
        if subject in name:
            return subject
    return None


def _text(row: list, position: int | None) -> str:
    if position is None or position >= len(row):
        return ""
    value = row[position]
    return "" if value is None else str(value).strip()


def _int(row: list, position: int | None) -> int | None:
    value = _text(row, position)
    try:
        return int(float(value))
    except ValueError:
        return None


def _normalize(value) -> str:
    if value is None:
        return ""
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).lower()
