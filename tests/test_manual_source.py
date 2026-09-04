from school_pipeline.sources.manual_source import ManualTextSource


def test_manual_text_source_reads_txt_files(tmp_path):
    (tmp_path / "classroom_post1.txt").write_text("来週水曜に漢字テストをします", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("これは対象外", encoding="utf-8")

    docs = list(ManualTextSource(tmp_path).fetch())

    assert len(docs) == 1
    assert docs[0].source.source_type == "manual"
    assert docs[0].source.label == "classroom_post1"
    assert "漢字テスト" in docs[0].text


def test_manual_text_source_missing_directory_yields_nothing(tmp_path):
    docs = list(ManualTextSource(tmp_path / "does_not_exist").fetch())
    assert docs == []


def test_manual_text_source_skips_blank_files(tmp_path):
    (tmp_path / "empty.txt").write_text("   \n", encoding="utf-8")
    docs = list(ManualTextSource(tmp_path).fetch())
    assert docs == []
