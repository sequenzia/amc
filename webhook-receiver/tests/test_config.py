"""Config loading tests."""

from __future__ import annotations

import pytest

from amg_receiver.config import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    ReceiverConfig,
    ReceiverConfigError,
)


def test_from_env_with_only_required_fields() -> None:
    cfg = ReceiverConfig.from_env({"AMG_WEBHOOK_SECRET": "s", "AMG_BEARER_TOKEN": "t"})
    assert cfg.webhook_secret == "s"
    assert cfg.bearer_token == "t"
    assert cfg.bind_host == DEFAULT_BIND_HOST
    assert cfg.bind_port == DEFAULT_BIND_PORT
    assert cfg.dangerous is False
    assert cfg.agent_id == "amg-receiver"


def test_from_env_missing_secret() -> None:
    with pytest.raises(ReceiverConfigError, match="AMG_WEBHOOK_SECRET"):
        ReceiverConfig.from_env({"AMG_BEARER_TOKEN": "t"})


def test_from_env_missing_bearer() -> None:
    with pytest.raises(ReceiverConfigError, match="AMG_BEARER_TOKEN"):
        ReceiverConfig.from_env({"AMG_WEBHOOK_SECRET": "s"})


def test_from_env_overrides() -> None:
    cfg = ReceiverConfig.from_env(
        {
            "AMG_WEBHOOK_SECRET": "s",
            "AMG_BEARER_TOKEN": "t",
            "AMG_RECEIVER_BIND_HOST": "0.0.0.0",  # noqa: S104
            "AMG_RECEIVER_BIND_PORT": "9090",
            "AMG_RECEIVER_DANGEROUS": "true",
            "AMG_AGENT_ID": "alt-agent",
        }
    )
    assert cfg.bind_host == "0.0.0.0"  # noqa: S104
    assert cfg.bind_port == 9090
    assert cfg.dangerous is True
    assert cfg.agent_id == "alt-agent"


def test_from_env_invalid_port() -> None:
    with pytest.raises(ReceiverConfigError, match="AMG_RECEIVER_BIND_PORT"):
        ReceiverConfig.from_env(
            {
                "AMG_WEBHOOK_SECRET": "s",
                "AMG_BEARER_TOKEN": "t",
                "AMG_RECEIVER_BIND_PORT": "not-a-number",
            }
        )


def test_from_env_port_out_of_range() -> None:
    with pytest.raises(ReceiverConfigError, match="<= 65535"):
        ReceiverConfig.from_env(
            {
                "AMG_WEBHOOK_SECRET": "s",
                "AMG_BEARER_TOKEN": "t",
                "AMG_RECEIVER_BIND_PORT": "70000",
            }
        )
