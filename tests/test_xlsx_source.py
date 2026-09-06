from datetime import date

import pytest

openpyxl = pytest.importorskip("openpyxl")

from school_pipeline.models import EventType
from school_pipeline.sources.xlsx_source import XlsxSource


def _workbook(tmp_path, rows, sheet_title="M2-5 月3水2金6", name="2026 英語 授業予定表.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


_HEADER = [None, "日", "曜日", None, "#", "進度予定", "進度結果", "提出", "小テスト", "チャレ"]


def test_reads_submissions_and_quizzes_only(tmp_path):
    """進度列は授業内容の記録であって予定ではないので読まない。"""
    _workbook(tmp_path, [
        _HEADER,
        ["9月", 7.0, "月", None, 2.0, "ユメリスUnit 9", None, "長文B-1", None, None],
        [None, 8.0, "火", None, None, None, None, None, None, "火チャレUnit 7"],
    ])
    events = XlsxSource(tmp_path, subject="英語").fetch_events()

    assert [(e.date, e.type, e.title) for e in events] == [
        (date(2026, 9, 7), EventType.ASSIGNMENT, "長文B-1"),
        (date(2026, 9, 8), EventType.QUIZ, "火チャレUnit 7"),
    ]


def test_excel_date_artifacts_in_progress_columns_are_ignored(tmp_path):
    """Excelが「7-2」を日付に自動変換した値から、幽霊予定を作らないこと。"""
    from datetime import datetime

    _workbook(tmp_path, [
        _HEADER,
        ["10月", 8.0, "木", None, 11.0, datetime(2026, 7, 2), None, None, None, None],
    ])
    assert XlsxSource(tmp_path, subject="英語").fetch_events() == []


def test_shared_prefix_is_expanded_into_separate_assignments(tmp_path):
    """「長文B-1, 2」を B-1 と B-2 に分けないと、他の資料と突き合わせられない。"""
    _workbook(tmp_path, [
        _HEADER,
        ["9月", 14.0, "月", None, None, None, None, "長文B-1, 2", None, None],
    ])
    events = XlsxSource(tmp_path, subject="英語").fetch_events()

    assert [e.identity_key for e in events] == ["b-1", "b-2"]
    assert [e.title for e in events] == ["長文B-1", "長文B-2"]
    assert all(e.date == date(2026, 9, 14) for e in events)


def test_assignment_identity_matches_other_sources(tmp_path):
    """Excelの「長文B-1」と、プリント由来の「B-1」が同じ予定になること。"""
    from school_pipeline.models import SchoolEvent

    _workbook(tmp_path, [
        _HEADER,
        ["9月", 14.0, "月", None, None, None, None, "長文B-1", None, None],
    ])
    from_excel = XlsxSource(tmp_path, subject="英語").fetch_events()[0]
    from_pdf = SchoolEvent(
        type=EventType.ASSIGNMENT, title="週末課題 B-1 提出",
        date=date(2026, 9, 14), subject="英語", identity_key="B-1",
    )
    assert from_excel.stable_id == from_pdf.stable_id


def test_quizzes_get_no_identity_so_repeats_stay_separate(tmp_path):
    """同じ名前の小テストが周回で再登場するので、番号では識別しない。"""
    _workbook(tmp_path, [
        _HEADER,
        ["4月", 14.0, "火", None, None, None, None, None, None, "火チャレUnit 1"],
        ["9月", 29.0, "火", None, None, None, None, None, None, "火チャレUnit 1"],
    ])
    events = XlsxSource(tmp_path, subject="英語").fetch_events()

    assert [e.identity_key for e in events] == [None, None]
    assert events[0].stable_id != events[1].stable_id


def test_rows_whose_weekday_disagrees_are_skipped(tmp_path):
    """曜日が合わない行は、年の取り違えや行ずれの疑いがあるので使わない。"""
    _workbook(tmp_path, [
        _HEADER,
        ["9月", 7.0, "月", None, None, None, None, "長文B-1", None, None],
        [None, 8.0, "日", None, None, None, None, "長文B-2", None, None],  # 実際は火曜
    ])
    events = XlsxSource(tmp_path, subject="英語").fetch_events()
    assert [e.identity_key for e in events] == ["b-1"]


def test_january_belongs_to_the_next_calendar_year(tmp_path):
    """学校の年度は4月始まり。1月は年が繰り上がる。"""
    _workbook(tmp_path, [
        _HEADER,
        ["1月", 13.0, "水", None, None, None, None, "長文C-1", None, None],
    ])
    events = XlsxSource(tmp_path, subject="英語").fetch_events()
    assert events[0].date == date(2027, 1, 13)


def test_only_included_sheets_are_read(tmp_path):
    """クラス別シートの取り違えを防ぐ。"""
    wb = openpyxl.Workbook()
    ours = wb.active
    ours.title = "M2-5 月3水2金6"
    theirs = wb.create_sheet("M2-3 火5金1土1")
    for ws, cell in ((ours, "長文B-1"), (theirs, "長文B-9")):
        ws.append(_HEADER)
        ws.append(["9月", 7.0, "月", None, None, None, None, cell, None, None])
    path = tmp_path / "予定表.xlsx"
    wb.save(path)

    events = XlsxSource(tmp_path, sheets_include=["M2-5"], subject="英語").fetch_events()
    assert [e.identity_key for e in events] == ["b-1"]


def test_parenthetical_notes_are_not_events(tmp_path):
    _workbook(tmp_path, [
        _HEADER,
        ["9月", 30.0, "水", None, None, None, None, None, None, "(2周目)"],
    ])
    assert XlsxSource(tmp_path, subject="英語").fetch_events() == []


def test_missing_columns_are_reported_not_silently_empty(tmp_path, caplog):
    """想定した列が無いのに黙って空を返すと、取り込めていないことに気づけない。"""
    _workbook(tmp_path, [
        [None, "日", "曜日", "備考"],
        ["9月", 7.0, "月", "なにか"],
    ])
    with caplog.at_level("WARNING"):
        assert XlsxSource(tmp_path, subject="英語").fetch_events() == []
    assert "読み飛ばしました" in caplog.text


def test_subject_falls_back_to_the_filename(tmp_path):
    _workbook(tmp_path, [
        _HEADER,
        ["9月", 7.0, "月", None, None, None, None, "長文B-1", None, None],
    ])
    assert XlsxSource(tmp_path).fetch_events()[0].subject == "英語"


def test_missing_directory_is_not_an_error(tmp_path):
    assert XlsxSource(tmp_path / "none").fetch_events() == []
