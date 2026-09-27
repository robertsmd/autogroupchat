"""Tests for SignalConfig validation (spec section 5)."""

import pytest

from autogroupchat.makers.automakesignal import (
    DEFAULT_INVOCATION_BUDGET_SECONDS,
    DEFAULT_SIGNAL_CLI_PATH,
    DEFAULT_TRUST_NEW_IDENTITIES,
    SignalConfig,
    SignalConfigError,
)

MINIMAL = {
    "signal_number": "+15551234567",
    "account_store": {"type": "local", "data_dir": "/tmp/store"},
}


def test_minimal_config_applies_documented_defaults():
    cfg = SignalConfig.from_dict(MINIMAL)

    assert cfg.signal_number == "+15551234567"
    assert cfg.signal_cli_path == DEFAULT_SIGNAL_CLI_PATH
    assert cfg.trust_new_identities == DEFAULT_TRUST_NEW_IDENTITIES
    assert cfg.invocation_budget_seconds == DEFAULT_INVOCATION_BUDGET_SECONDS


def test_explicit_values_override_defaults():
    cfg = SignalConfig.from_dict({
        **MINIMAL,
        "signal_cli_path": "/opt/signal-cli/signal-cli",
        "trust_new_identities": "always",
        "invocation_budget_seconds": 120,
    })

    assert cfg.signal_cli_path == "/opt/signal-cli/signal-cli"
    assert cfg.trust_new_identities == "always"
    assert cfg.invocation_budget_seconds == 120


@pytest.mark.parametrize("missing", ["signal_number", "account_store"])
def test_missing_required_key_raises(missing):
    config = {k: v for k, v in MINIMAL.items() if k != missing}

    with pytest.raises(SignalConfigError) as exc:
        SignalConfig.from_dict(config)

    assert missing in str(exc.value)


def test_account_store_without_type_raises():
    with pytest.raises(SignalConfigError) as exc:
        SignalConfig.from_dict({**MINIMAL, "account_store": {"data_dir": "/tmp"}})

    assert "type" in str(exc.value)


def test_empty_signal_number_raises():
    """An empty string is a missing value, not a valid account."""
    with pytest.raises(SignalConfigError):
        SignalConfig.from_dict({**MINIMAL, "signal_number": ""})


def test_config_is_frozen():
    cfg = SignalConfig.from_dict(MINIMAL)

    with pytest.raises(Exception):
        cfg.signal_number = "+10000000000"
