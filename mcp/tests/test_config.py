"""Unit tests for ``amc_mcp.config`` (mirrors mcp-wrapper's config.test.ts)."""

from __future__ import annotations

import pytest

from amc_mcp.config import (
    DEFAULT_BASE_URL,
    ENV_AGENT_ID,
    ENV_BASE_URL,
    ENV_BEARER_TOKEN,
    WrapperConfigError,
    get_config,
    load_config,
    reset_config,
)


class TestLoadConfig:
    def test_returns_config_when_required_vars_present(self) -> None:
        cfg = load_config({ENV_BEARER_TOKEN: "secret-token", ENV_AGENT_ID: "claude-code"})
        assert cfg.bearer_token == "secret-token"
        assert cfg.agent_id == "claude-code"
        assert cfg.base_url == DEFAULT_BASE_URL

    def test_default_base_url_when_unset(self) -> None:
        cfg = load_config({ENV_BEARER_TOKEN: "t", ENV_AGENT_ID: "a"})
        assert cfg.base_url == "http://127.0.0.1:8080"

    def test_honors_amc_base_url_when_set(self) -> None:
        cfg = load_config(
            {
                ENV_BASE_URL: "http://localhost:9999",
                ENV_BEARER_TOKEN: "t",
                ENV_AGENT_ID: "a",
            }
        )
        assert cfg.base_url == "http://localhost:9999"

    def test_trims_surrounding_whitespace(self) -> None:
        cfg = load_config(
            {
                ENV_BASE_URL: "  http://example.test  ",
                ENV_BEARER_TOKEN: "  token  ",
                ENV_AGENT_ID: "  agent  ",
            }
        )
        assert cfg.base_url == "http://example.test"
        assert cfg.bearer_token == "token"
        assert cfg.agent_id == "agent"

    def test_missing_bearer_token(self) -> None:
        with pytest.raises(WrapperConfigError, match="AMC_BEARER_TOKEN"):
            load_config({ENV_AGENT_ID: "a"})

    def test_missing_agent_id(self) -> None:
        with pytest.raises(WrapperConfigError, match="AMC_AGENT_ID"):
            load_config({ENV_BEARER_TOKEN: "t"})

    def test_lists_every_missing_var_in_one_message(self) -> None:
        with pytest.raises(WrapperConfigError) as excinfo:
            load_config({})
        msg = str(excinfo.value)
        assert "AMC_BEARER_TOKEN" in msg
        assert "AMC_AGENT_ID" in msg

    def test_treats_whitespace_only_values_as_missing(self) -> None:
        with pytest.raises(WrapperConfigError, match="AMC_BEARER_TOKEN"):
            load_config({ENV_BEARER_TOKEN: "   ", ENV_AGENT_ID: "a"})

    def test_rejects_invalid_base_url(self) -> None:
        with pytest.raises(WrapperConfigError, match="AMC_BASE_URL"):
            load_config(
                {
                    ENV_BASE_URL: "not a url",
                    ENV_BEARER_TOKEN: "t",
                    ENV_AGENT_ID: "a",
                }
            )


class TestGetConfig:
    def test_returns_cached_config_after_load(self) -> None:
        load_config({ENV_BEARER_TOKEN: "t", ENV_AGENT_ID: "a"})
        assert get_config().bearer_token == "t"

    def test_raises_if_not_loaded(self) -> None:
        with pytest.raises(WrapperConfigError):
            get_config()

    def test_reset_clears_cache(self) -> None:
        load_config({ENV_BEARER_TOKEN: "t", ENV_AGENT_ID: "a"})
        reset_config()
        with pytest.raises(WrapperConfigError):
            get_config()
