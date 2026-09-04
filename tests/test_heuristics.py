from datetime import date

from school_pipeline.extraction.heuristics import extract_explicit_dates, has_relative_date_terms


def test_extract_explicit_dates_slash_format():
    dates = extract_explicit_dates("9/10(木)までに提出してください", reference=date(2026, 9, 1))
    assert date(2026, 9, 10) in dates


def test_extract_explicit_dates_kanji_format():
    dates = extract_explicit_dates("定期試験は9月15日から実施します", reference=date(2026, 9, 1))
    assert date(2026, 9, 15) in dates


def test_extract_explicit_dates_rolls_over_to_next_year():
    # 12月に配布された「1月」の予定は翌年として解釈する
    dates = extract_explicit_dates("1/20に実力テストを行います", reference=date(2026, 12, 1))
    assert date(2027, 1, 20) in dates


def test_extract_explicit_dates_ignores_invalid_dates():
    dates = extract_explicit_dates("13/40というのは日付ではありません", reference=date(2026, 9, 1))
    assert dates == []


def test_has_relative_date_terms():
    assert has_relative_date_terms("来週の月曜までに提出") is True
    assert has_relative_date_terms("運動会は10月10日です") is False
