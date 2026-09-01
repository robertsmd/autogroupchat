"""
Signal backend for autogroupchat, driving the signal-cli binary.

Design: docs/superpowers/specs/2026-08-31-signal-maker-design.md

Unlike GroupMe, Signal has no static API token. A signal-cli credential is a
mutable data directory (identity key, prekeys, per-recipient ratchet state,
cached group state) backed by a WAL-mode SQLite database. The config file
therefore carries account *identity and location* only; moving the mutable
store is signalaccountstore's job.
"""

import logging
from dataclasses import dataclass
from typing import Any

global logger
logger = logging.getLogger(__name__)

# signal-cli release this code is written against. `doctor` enforces the floor.
SIGNAL_CLI_MIN_VERSION = (0, 14)

DEFAULT_SIGNAL_CLI_PATH = "signal-cli"
DEFAULT_TRUST_NEW_IDENTITIES = "on-first-use"

# 540 s is the Cloud Run 2nd-gen event-driven ceiling; 60 s is reserved for
# account-store download and upload, leaving 480 s of working budget.
PLATFORM_TIMEOUT_SECONDS = 540
STORE_TRANSFER_RESERVE_SECONDS = 60
DEFAULT_INVOCATION_BUDGET_SECONDS = (
    PLATFORM_TIMEOUT_SECONDS - STORE_TRANSFER_RESERVE_SECONDS
)


class SignalConfigError(Exception):
    """Raised when the JSON config is missing a value that has no safe default."""


@dataclass(frozen=True)
class SignalConfig:
    """
    Validated contents of configs/config_signal.json.

    Required keys have no defaults on purpose: defaulting `signal_number` would
    operate on the wrong account, and defaulting `account_store` would write
    mutated credential state nowhere.
    """

    signal_number: str
    account_store: dict[str, Any]
    signal_cli_path: str = DEFAULT_SIGNAL_CLI_PATH
    trust_new_identities: str = DEFAULT_TRUST_NEW_IDENTITIES
    invocation_budget_seconds: int = DEFAULT_INVOCATION_BUDGET_SECONDS

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "SignalConfig":
        """Validate a parsed JSON config, raising SignalConfigError on absence."""
        number = config.get("signal_number")
        if not number:
            raise SignalConfigError(
                "config key 'signal_number' is required and must be non-empty")

        store = config.get("account_store")
        if not store:
            raise SignalConfigError(
                "config key 'account_store' is required and must be non-empty")

        if not store.get("type"):
            raise SignalConfigError(
                "config key 'account_store.type' is required "
                "(expected 'local' or 'gcs')")

        return cls(
            signal_number=number,
            account_store=store,
            signal_cli_path=config.get(
                "signal_cli_path", DEFAULT_SIGNAL_CLI_PATH),
            trust_new_identities=config.get(
                "trust_new_identities", DEFAULT_TRUST_NEW_IDENTITIES),
            invocation_budget_seconds=int(config.get(
                "invocation_budget_seconds", DEFAULT_INVOCATION_BUDGET_SECONDS)),
        )
