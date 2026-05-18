import os
import pytest
import hunter.config as config


# ── _parse_bool ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
def test_parse_bool_truthy(value, monkeypatch):
    monkeypatch.setenv("TEST_FLAG", value)
    assert config._parse_bool("TEST_FLAG", default=False) is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "off"])
def test_parse_bool_falsy(value, monkeypatch):
    monkeypatch.setenv("TEST_FLAG", value)
    assert config._parse_bool("TEST_FLAG", default=True) is False


def test_parse_bool_default_true(monkeypatch):
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert config._parse_bool("TEST_FLAG", default=True) is True


def test_parse_bool_default_false(monkeypatch):
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert config._parse_bool("TEST_FLAG", default=False) is False


def test_parse_bool_strips_whitespace(monkeypatch):
    monkeypatch.setenv("TEST_FLAG", "  true  ")
    assert config._parse_bool("TEST_FLAG", default=False) is True


# ── validate_config ───────────────────────────────────────────────────────────
# Patch the already-computed module-level vars rather than reloading the module
# (reload breaks test isolation via shared FILTER dict references).

def test_validate_config_passes_with_valid_env(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 123456)
    monkeypatch.setattr(config, "SCHEDULE_SOURCE_OFFSET_MIN", 40)
    monkeypatch.setattr(config, "MAX_JOBS_PER_RUN", 10)
    config.validate_config()  # must not raise or exit


def test_validate_config_exits_on_missing_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 123456)
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)


def test_validate_config_exits_on_missing_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 0)
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    assert "TELEGRAM_CHAT_ID" in str(exc_info.value)


def test_validate_config_exits_on_negative_offset(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 123456)
    monkeypatch.setattr(config, "SCHEDULE_SOURCE_OFFSET_MIN", -5)
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    assert "SCHEDULE_SOURCE_OFFSET_MIN" in str(exc_info.value)


def test_validate_config_exits_on_zero_max_jobs(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 123456)
    monkeypatch.setattr(config, "SCHEDULE_SOURCE_OFFSET_MIN", 40)
    monkeypatch.setattr(config, "MAX_JOBS_PER_RUN", 0)
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    assert "MAX_JOBS_PER_RUN" in str(exc_info.value)


def test_validate_config_collects_multiple_errors(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", 0)
    with pytest.raises(SystemExit) as exc_info:
        config.validate_config()
    msg = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in msg
    assert "TELEGRAM_CHAT_ID" in msg
