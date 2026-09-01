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
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

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


# (argv, timeout_seconds) -> (exit_code, stdout, stderr)
Runner = Callable[[list[str], float], tuple[int, str, str]]


def _subprocess_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Default Runner. Never invoked by unit tests, which inject their own."""
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False)

    return (completed.returncode, completed.stdout, completed.stderr)


class SignalCli:
    """
    Thin driver over the signal-cli binary.

    Speaks signal-cli's vocabulary only: it knows about groups, members and
    admins as command-line arguments, not about "owners" or purge policy. It is
    handed an already-materialised data dir and never moves credential state.
    """

    def __init__(self,
                 number: str,
                 data_dir: str,
                 *,
                 binary: str = DEFAULT_SIGNAL_CLI_PATH,
                 trust_new_identities: str = DEFAULT_TRUST_NEW_IDENTITIES,
                 deadline: float | None = None,
                 runner: Runner | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.number = number
        self.data_dir = data_dir
        self.binary = binary
        self.trust_new_identities = trust_new_identities
        self.deadline = deadline

        self._run_process = runner or _subprocess_runner
        self._sleep = sleep
        self._clock = clock

    def globals(self) -> list[str]:
        """
        Flags that precede every subcommand.

        --data-dir is always explicit rather than inherited from
        $XDG_DATA_HOME: the cloud path materialises the store elsewhere, and an
        implicit default would silently operate on an empty account.
        """
        return [
            "--output=json",
            "-a", self.number,
            "--data-dir", self.data_dir,
            "--trust-new-identities", self.trust_new_identities,
        ]

    def argv(self, subcommand: str, *args: str) -> list[str]:
        """
        Build a full argv with globals guaranteed ahead of the subcommand.

        Callers cannot get the ordering wrong, which matters because global -a
        means --account while updateGroup's -a means --avatar.
        """
        if subcommand.startswith("-"):
            raise ValueError(
                f"subcommand must not look like a flag: {subcommand!r}")

        return [self.binary, *self.globals(), subcommand, *[str(a) for a in args]]
