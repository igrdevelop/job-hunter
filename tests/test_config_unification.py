import sys

import hunter.config as config
from hunter.services.tracker_service import should_skip_url


def test_hunter_config_exposes_cli_retry_settings() -> None:
    assert hasattr(config, "CLI_MAX_RETRIES")
    assert hasattr(config, "CLI_RETRY_DELAY")
    assert isinstance(config.CLI_MAX_RETRIES, int)
    assert isinstance(config.CLI_RETRY_DELAY, int)
    assert config.CLI_MAX_RETRIES >= 1
    assert config.CLI_RETRY_DELAY >= 0


def test_should_skip_url_does_not_mutate_sys_path() -> None:
    before = list(sys.path)
    try:
        should_skip_url("https://example.com/jobs/42")
    except Exception:
        pass  # tracker.xlsx may not exist in test env
    after = list(sys.path)
    assert after == before
