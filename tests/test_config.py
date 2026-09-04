from school_pipeline.config import load_settings


def test_load_settings_handles_empty_gmail_accounts_key(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "gmail_accounts:\n"
        "manual_text_directory: inbox/manual\n"
        "calendar_id: primary\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.gmail_accounts == []
    assert settings.manual_text_directory == "inbox/manual"


def test_load_settings_parses_gmail_accounts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "gmail_accounts:\n"
        "  - name: school_common\n"
        "    credentials_path: secrets/a.json\n"
        "    token_path: secrets/a_token.json\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert len(settings.gmail_accounts) == 1
    assert settings.gmail_accounts[0].name == "school_common"


def test_load_settings_defaults_for_missing_file_fields(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_id: primary\n", encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.gmail_accounts == []
    assert settings.confidence_threshold == 0.5
