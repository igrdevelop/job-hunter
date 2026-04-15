import sys

import apply_agent
import hunter.config as config


def test_hunter_config_exposes_cli_retry_settings() -> None:
    assert hasattr(config, "CLI_MAX_RETRIES")
    assert hasattr(config, "CLI_RETRY_DELAY")
    assert isinstance(config.CLI_MAX_RETRIES, int)
    assert isinstance(config.CLI_RETRY_DELAY, int)
    assert config.CLI_MAX_RETRIES >= 1
    assert config.CLI_RETRY_DELAY >= 0


def test_already_processed_does_not_mutate_sys_path() -> None:
    before = list(sys.path)
    apply_agent._already_processed("https://example.com/jobs/42")
    after = list(sys.path)
    assert after == before
