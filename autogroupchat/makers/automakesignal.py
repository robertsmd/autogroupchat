"""
Signal backend for autogroupchat, driving the signal-cli binary.

Design: docs/superpowers/specs/2026-08-31-signal-maker-design.md

Unlike GroupMe, Signal has no static API token. A signal-cli credential is a
mutable data directory (identity key, prekeys, per-recipient ratchet state,
cached group state) backed by a WAL-mode SQLite database. The config file
therefore carries account *identity and location* only; moving the mutable
store is signalaccountstore's job.
"""

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
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

# Cap on how much raw stdout gets embedded in a JSON-parse-failure message.
# Enough to see the shape of the bad output; not derived from anything, just
# short enough that one bad response can't flood a log line.
RUN_JSON_ERROR_PREVIEW_CHARS = 500

# A dot-separated version component's leading digit run, e.g. "7" out of
# "7-SNAPSHOT". Components with no leading digit (a bare qualifier like
# "rc1" preceded by its own dot) end the parse.
VERSION_COMPONENT_RE = re.compile(r"\d+")


class ExitCode(IntEnum):
    """
    signal-cli's documented exit codes.

    Source: signal-cli(1) "Exit codes". These drive the retry policy directly,
    so no error-string matching is needed to decide whether to retry.
    """

    SUCCESS = 0
    USER_ERROR = 1
    UNEXPECTED = 2
    SERVER_OR_IO = 3
    UNTRUSTED_KEY = 4
    RATE_LIMIT = 5
    CAPTCHA_REJECTED = 6


class Retry(Enum):
    """Whether an operation may be attempted more than once."""

    ENABLED = "enabled"
    DISABLED = "disabled"


# Matches AutoMakeGroupMe's min(2 ** attempt, 8) so both backends behave alike.
BACKOFF_SERVER_OR_IO = (1, 2, 4, 8)

# PROVISIONAL, not derived. signal-cli surfaces no Retry-After through its exit
# code, so there is nothing to measure yet (spec section 16, item 3). If rate
# limiting turns out to be routine, the documented fix is the signal-cli
# `submitRateLimitChallenge` command rather than longer sleeps here.
BACKOFF_RATE_LIMIT = (60, 120)

BACKOFFS: dict[int, tuple[int, ...]] = {
    int(ExitCode.SERVER_OR_IO): BACKOFF_SERVER_OR_IO,
    int(ExitCode.RATE_LIMIT): BACKOFF_RATE_LIMIT,
}


class Delete(Enum):
    """Whether quitGroup also discards the local copy of the group's state."""

    LOCAL_DATA = "local-data"
    KEEP_LOCAL_DATA = "keep-local-data"


class SignalCliError(Exception):
    """A signal-cli invocation exited non-zero."""

    def __init__(self, exit_code: int, stderr: str, argv: list[str]) -> None:
        """
        Args:
            exit_code: signal-cli's exit status.
            stderr: Captured stderr from the invocation.
            argv: The full argv that was run, for reproduction. Nothing in
                it is a secret (the account number already lives in the
                config file, and the real credential is the on-disk store),
                so it is quoted for safe display, not stripped.
        """
        self.exit_code = exit_code
        self.stderr = stderr
        self.argv = argv

        quoted = " ".join(shlex.quote(a) for a in argv)
        super().__init__(
            f"signal-cli exited {exit_code}: {stderr.strip()} [{quoted}]")


class BudgetExhausted(Exception):
    """
    The invocation's wall-clock budget ran out.

    Raised instead of starting work we cannot finish, so the run fails inside
    its own budget rather than being killed mid-write by the platform.
    """


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


# (argv, timeout_seconds) -> (exit_code, stdout, stderr). timeout is None for
# "no limit", matching subprocess.run's own convention.
Runner = Callable[[list[str], float | None], tuple[int, str, str]]


def _subprocess_runner(
        argv: list[str], timeout: float | None) -> tuple[int, str, str]:
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
        """
        Configure a driver for one signal-cli account.

        Args:
            number: The +E164 account number, sent as the global -a flag.
            data_dir: signal-cli's data directory for this account.
            binary: Path to the signal-cli executable.
            trust_new_identities: Value for --trust-new-identities.
            deadline: Absolute wall-clock cutoff on the same scale as
                `clock()`, or None for no limit. `run` refuses to start a
                subprocess or a backoff sleep once this passes, so retries
                fail inside their own budget instead of being killed mid-
                operation by the platform.
            runner: Seam for the subprocess call: (argv, timeout) ->
                (exit_code, stdout, stderr). Tests inject a fake here so the
                real binary is never executed.
            sleep: Seam for backoff waits. Tests inject a fake here so no
                test actually sleeps.
            clock: Seam for reading the current time against `deadline`.
                Tests inject a fake here so retry-budget tests run without
                real elapsed time.
        """
        self.number = number
        self.data_dir = data_dir
        self.binary = binary
        self.trust_new_identities = trust_new_identities
        self.deadline = deadline

        self._run_process = runner or _subprocess_runner
        self._sleep = sleep
        self._clock = clock

    def global_flags(self) -> list[str]:
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

        return [self.binary, *self.global_flags(), subcommand, *[str(a) for a in args]]

    def _remaining(self) -> float:
        """Seconds left before the invocation deadline, or infinity if unset."""
        if self.deadline is None:
            return float("inf")

        return self.deadline - self._clock()

    def _check_budget(self, about_to: str) -> float:
        """Raise unless there is budget left; return the remaining seconds."""
        remaining = self._remaining()
        if remaining <= 0:
            raise BudgetExhausted(
                f"invocation budget exhausted before {about_to}")

        return remaining

    def _timeout(self, remaining: float) -> float | None:
        """
        Convert remaining budget to a subprocess timeout.

        `_remaining()` reports an unset deadline as float("inf"), which is not
        a value `subprocess.run(timeout=...)` should ever see; None is its own
        convention for "no limit". Both `run()` and `_run_bare()` funnel
        through here so that conversion happens in exactly one place.
        """
        return None if remaining == float("inf") else remaining

    def run(self,
            subcommand: str,
            *args: str,
            retry: Retry = Retry.ENABLED) -> str:
        """
        Invoke signal-cli once, retrying only on codes that can succeed later.

        Retry is bounded by the invocation deadline rather than by attempt count
        alone: two rate-limited calls backing off 60 s then 120 s would exceed
        the platform's 540 s ceiling on their own.

        Attempts are tracked per exit code, not as one shared counter: a call
        that fails SERVER_OR_IO then RATE_LIMIT must give the rate limit its
        own first backoff (60s), not the position a different code's failures
        left behind. Resetting the counter whenever the code changes was
        rejected too — alternating failures would then reset each other
        forever, leaving the deadline as the only bound. A dict caps total
        retries at sum(len(b) for b in BACKOFFS.values()) regardless.
        """
        argv = self.argv(subcommand, *args)
        attempts: dict[int, int] = {}

        while True:
            remaining = self._check_budget(f"running {subcommand}")
            timeout = self._timeout(remaining)

            exit_code, stdout, stderr = self._run_process(argv, timeout)
            if exit_code == int(ExitCode.SUCCESS):
                return stdout

            error = SignalCliError(exit_code, stderr, argv)

            if retry is Retry.DISABLED:
                raise error

            backoff = BACKOFFS.get(exit_code, ())
            attempt = attempts.get(exit_code, 0)
            if attempt >= len(backoff):
                raise error

            wait = backoff[attempt]
            attempts[exit_code] = attempt + 1

            # Refuse to sleep past the deadline; fail now instead of being
            # killed mid-operation by the platform.
            if wait >= self._check_budget(f"backing off before {subcommand}"):
                raise BudgetExhausted(
                    f"{wait}s backoff for {subcommand} exceeds remaining budget")

            logger.warning(
                f"{subcommand} exited {exit_code} "
                f"(attempt {attempt + 1}/{len(backoff)}); retrying in {wait}s")
            self._sleep(wait)

    def run_json(self,
                 subcommand: str,
                 *args: str,
                 retry: Retry = Retry.ENABLED) -> Any:
        """Run a subcommand and parse its JSON output, or {} if it printed none."""
        stdout = self.run(subcommand, *args, retry=retry)
        if not stdout.strip():
            return {}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            # This is a client-side parse failure, not a signal-cli error
            # exit: run() only returns here when the process exited 0, so
            # ExitCode.SUCCESS is the real code, not a fabricated one. The
            # raw stdout is included (truncated) so the bad output is not
            # lost, but capped so one huge blob can't flood a log line.
            preview = stdout[:RUN_JSON_ERROR_PREVIEW_CHARS]
            if len(stdout) > RUN_JSON_ERROR_PREVIEW_CHARS:
                preview += "...(truncated)"

            raise SignalCliError(
                int(ExitCode.SUCCESS),
                f"{subcommand} exited 0 but printed unparseable JSON "
                f"({e}): {preview!r}",
                self.argv(subcommand, *args),
            ) from e

    def create_group(self,
                     name: str,
                     description: str,
                     avatar: str | None = None) -> str:
        """
        Create a group and return its base64 id.

        Deliberately never retried. `updateGroup` with no -g creates a new
        group every time, so a retry after a transport error leaves an orphan
        group behind and returns the id of the second one.

        signal-cli reports groupId only when it actually created a group
        (UpdateGroupCommand.java:213), so its absence is a hard error rather
        than something to paper over.
        """
        args = ["-n", name, "-d", description]
        if avatar:
            # updateGroup's -a is --avatar. Safe here only because
            # global_flags() already consumed the account's -a ahead of the
            # subcommand.
            args += ["-a", avatar]

        response = self.run_json("updateGroup", *args, retry=Retry.DISABLED)

        if not isinstance(response, dict):
            raise SignalCliError(
                int(ExitCode.SUCCESS),
                f"updateGroup returned {type(response).__name__}, not an "
                "object, so no groupId could be read",
                self.argv("updateGroup", *args),
            )

        group_id = response.get("groupId")
        if not group_id:
            raise SignalCliError(
                int(ExitCode.UNEXPECTED),
                "updateGroup returned no groupId, so no group was created",
                self.argv("updateGroup", *args),
            )

        return group_id

    def update_group(self,
                     group_id: str,
                     *,
                     members: list[str] = (),
                     admins: list[str] = (),
                     name: str | None = None,
                     description: str | None = None) -> None:
        """Modify an existing group. Does nothing when there is nothing to change."""
        args: list[str] = []
        if name:
            args += ["-n", name]

        if description:
            args += ["-d", description]

        if admins:
            args += ["--admin", *admins]

        # -m is nargs="*", so it must come last or it swallows following flags.
        if members:
            args += ["-m", *members]

        if not args:
            logger.debug(f"update_group({group_id}) had nothing to change")
            return

        self.run_json("updateGroup", "-g", group_id, *args)

    def send_group(self, group_id: str, message: str) -> None:
        """Send one text message to a group."""
        self.run_json("send", "-g", group_id, "-m", message)

    def list_groups(self) -> list[dict[str, Any]]:
        """
        Return every group this account knows about, with member detail.

        Note the id key is "id" here, while updateGroup calls the same value
        "groupId" (ListGroupsCommand.java:149 vs UpdateGroupCommand.java:214).

        An unexpected (non-list) shape raises rather than reading as "zero
        groups": a silently empty listing is indistinguishable from a
        legitimately empty account, and would make purge selection,
        membership logging, and admin succession all silently no-op if
        signal-cli's output shape ever changes. Empty stdout is not this
        case: run_json already turns it into {}, which list_groups turns
        into [] below, matching a brand-new account that has no groups.
        """
        groups = self.run_json("listGroups", "-d")
        if groups == {}:
            return []

        if not isinstance(groups, list):
            raise SignalCliError(
                int(ExitCode.SUCCESS),
                f"listGroups returned {type(groups).__name__}, not a list",
                self.argv("listGroups", "-d"),
            )

        return groups

    def quit_group(self,
                   group_id: str,
                   *,
                   new_admins: list[str] = (),
                   delete: Delete = Delete.LOCAL_DATA) -> None:
        """
        Leave a group, optionally naming successor admins.

        signal-cli requires --admin when the departing account is the only
        admin. This leaves rather than deletes: the group survives for its
        remaining members, since Signal has no destroy-group operation.
        """
        args = ["-g", group_id]
        if delete is Delete.LOCAL_DATA:
            args.append("--delete")

        if new_admins:
            args += ["--admin", *new_admins]

        self.run_json("quitGroup", *args)

    def version(self) -> tuple[int, ...]:
        """
        Parse the version signal-cli reports, e.g. (0, 14, 7).

        Each dot-separated component is read for its leading digit run, so a
        build qualifier suffixed onto the last component (e.g. "7-SNAPSHOT",
        "0-rc1") still yields the numeric value instead of dropping the whole
        component. Parsing stops at the first component with no leading
        digit at all.
        """
        stdout = self._run_bare("--version")
        parts = stdout.strip().split()
        if not parts:
            raise SignalCliError(
                int(ExitCode.UNEXPECTED), "no version reported", [self.binary])

        numbers: list[int] = []
        for component in parts[-1].split("."):
            match = VERSION_COMPONENT_RE.match(component)
            if not match:
                break

            numbers.append(int(match.group()))

        return tuple(numbers)

    def _run_bare(self, *args: str) -> str:
        """
        Run the binary with no account globals, for --version and link.

        `link` forbids -a entirely, and --version needs no account, so neither
        can go through argv().
        """
        remaining = self._check_budget(f"running {args[0] if args else 'binary'}")
        timeout = self._timeout(remaining)
        argv = [self.binary, *args]

        exit_code, stdout, stderr = self._run_process(argv, timeout)
        if exit_code != int(ExitCode.SUCCESS):
            raise SignalCliError(exit_code, stderr, argv)

        return stdout
