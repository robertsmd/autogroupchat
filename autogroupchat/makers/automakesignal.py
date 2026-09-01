"""
Signal backend for autogroupchat, driving the signal-cli binary.

Design: docs/superpowers/specs/2026-08-31-signal-maker-design.md

Unlike GroupMe, Signal has no static API token. A signal-cli credential is a
mutable data directory (identity key, prekeys, per-recipient ratchet state,
cached group state) backed by a WAL-mode SQLite database. The config file
therefore carries account *identity and location* only; moving the mutable
store is signalaccountstore's job.
"""

import argparse
import contextlib
import datetime
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import TracebackType
from typing import IO, Any, Callable, Iterator, Protocol

from autogroupchat.makers.automakegroupchat import (
    MESSAGE_ALWAYS_SEND,
    AutoMakeGroupChat,
)
from autogroupchat.makers.signalaccountstore import (
    AccountStore,
    OnMissing,
    build_store,
)

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


class InvocationDeadline:
    """
    The single wall-clock cutoff every session in one invocation shares.

    The platform ceiling is per *invocation*, but AutoScrapeGroup.create_groups
    calls group_startup once per group and each call builds its own maker and
    its own session. A deadline anchored per session would therefore hand N
    groups N x 480 s of budget under one 540 s ceiling: group 1 spends 400 s in
    legitimate backoff inside its own budget, group 2 starts at t=400 with a
    brand-new 480 s deadline, and Cloud Run kills it at t=540 mid-operation --
    precisely the send-then-no-upload window the budget exists to prevent
    (spec section 12).

    Latched on first use and resettable rather than fixed at import: Cloud Run
    reuses a warm container across invocations, so an import-time anchor would
    be minutes stale by the second invocation and every session in it would
    start already out of budget -- worse than the per-session anchor it
    replaces. The request handler calls `reset()` at each invocation boundary;
    a one-shot CLI run never needs to, because first use is its start.
    """

    def __init__(self) -> None:
        """Create an un-anchored invocation; the first `at` call anchors it."""
        self._started_at: float | None = None

    def reset(self) -> None:
        """
        Un-anchor, so the next `at` call starts a fresh invocation.

        Called at each invocation boundary by the request handler.
        """
        self._started_at = None

    def at(self, budget_seconds: float,
           clock: Callable[[], float]) -> float:
        """
        Deadline for `budget_seconds`, anchoring the invocation if unstarted.

        The clock is the caller's rather than one held here, so the anchor and
        the SignalCli that measures itself against the resulting deadline can
        never end up reading two different clocks.
        """
        if self._started_at is None:
            self._started_at = clock()

        return self._started_at + budget_seconds


# Shared by every session in this process unless one is injected. Module-level
# because the invocation, not the session, is what the platform times out.
INVOCATION_DEADLINE = InvocationDeadline()


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
    """
    A signal-cli invocation failed.

    Usually that means a non-zero exit, and `exit_code` is signal-cli's own
    status. It is legitimately ExitCode.SUCCESS on the paths where the process
    exited 0 but its output could not be used -- unparseable JSON, updateGroup
    reporting no groupId, --version reporting no version. Those are client-side
    failures with no signal-cli status of their own, and fabricating one would
    misinform anything keying off `exit_code`, the retry policy included.
    """

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


class StreamingProcess(Protocol):
    """
    A launched process whose stdout can be read line by line as it runs.

    The Runner seam above cannot express this: it returns output that is
    already complete, so a command whose output must be read *while the
    process is still running* needs its own seam. `subprocess.Popen`
    satisfies this protocol as-is.
    """

    stdout: IO[str] | None

    def wait(self) -> int:
        """Block until the process exits, then return its exit status."""


# argv -> a running process. No timeout parameter: the only caller is `link`,
# which waits on a human.
Launcher = Callable[[list[str]], StreamingProcess]


def _popen_launcher(argv: list[str]) -> StreamingProcess:
    """
    Default Launcher: line-buffered text output, stderr folded into stdout.

    bufsize=1 with text=True gives line buffering on our side of the pipe, so
    each line becomes readable when signal-cli writes it rather than when the
    process exits. stderr is merged rather than piped separately because
    nothing reads a second pipe concurrently, and a full stderr buffer would
    block the process we are waiting on.

    Never invoked by unit tests, which inject their own.
    """
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


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
                 launcher: Launcher | None = None,
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
            launcher: Seam for launching a process whose output is streamed
                rather than captured, used only by `link`. Separate from
                `runner` because a Runner's output is already complete by
                the time it returns, and `link` must show its URI while the
                process is still running.
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
        self._launch = launcher or _popen_launcher
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
            # SUCCESS, not a fabricated code: the process exited 0 and only
            # its output was unusable, exactly as in the non-dict branch above.
            raise SignalCliError(
                int(ExitCode.SUCCESS),
                "updateGroup exited 0 but returned no groupId, so no group "
                "was created",
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
            # _run_bare only returns on a zero exit, so 0 is the real code.
            raise SignalCliError(
                int(ExitCode.SUCCESS),
                "--version exited 0 but reported no version",
                [self.binary])

        numbers: list[int] = []
        for component in parts[-1].split("."):
            match = VERSION_COMPONENT_RE.match(component)
            if not match:
                break

            numbers.append(int(match.group()))

        return tuple(numbers)

    def link(self, name: str) -> None:
        """
        Provision this data dir as a secondary device, streaming as it goes.

        `signal-cli link` prints an `sgnl://linkdevice?...` URI and then blocks
        until the operator scans it from the phone's Signal app -- it must, or
        provisioning never completes and the data dir holds no account. So the
        output is echoed line by line as it arrives: capturing it and printing
        it after the process exits cannot work, because the process cannot exit
        until someone scans a URI they were never shown.

        No deadline, for the same reason: a human at a QR code is not on the
        platform's invocation budget, so this bypasses _check_budget as well as
        argv() (signal-cli forbids -a on `link`).

        Args:
            name: Device name shown in Signal's Linked Devices list.

        Raises:
            SignalCliError: signal-cli exited non-zero. Its streamed output
                stands in for stderr, which the launcher folds into stdout.
        """
        argv = [self.binary, "--data-dir", self.data_dir, "link", "-n", name]
        process = self._launch(argv)

        output: list[str] = []

        # Echo each line the moment it arrives; a URI still sitting in a pipe
        # buffer cannot be scanned, and nothing else will end the wait.
        if process.stdout is not None:
            for line in process.stdout:
                output.append(line)
                print(line.rstrip("\n"), flush=True)

        exit_code = process.wait()
        if exit_code != int(ExitCode.SUCCESS):
            raise SignalCliError(exit_code, "".join(output), argv)

    def _run_bare(self, *args: str) -> str:
        """
        Run the binary with no account globals, for --version.

        --version needs no account, so it cannot go through argv(), which
        always emits the global -a.
        """
        remaining = self._check_budget(f"running {args[0] if args else 'binary'}")
        timeout = self._timeout(remaining)
        argv = [self.binary, *args]

        exit_code, stdout, stderr = self._run_process(argv, timeout)
        if exit_code != int(ExitCode.SUCCESS):
            raise SignalCliError(exit_code, stderr, argv)

        return stdout


class SignalSession:
    """
    Owns one account-store lifecycle and hands out a SignalCli bound to it.

    Scoped to a whole run rather than a single command: the store is downloaded
    once on entry and uploaded once on exit, so wrapping individual operations
    would mean a GCS round trip per signal-cli call.
    """

    def __init__(self,
                 config: SignalConfig,
                 *,
                 store: AccountStore | None = None,
                 runner: Runner | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 invocation: InvocationDeadline | None = None,
                 on_missing: OnMissing = OnMissing.ERROR) -> None:
        """
        Configure a session for one run, without touching the store yet.

        Args:
            config: Validated account identity and location.
            store: Seam for the account-store backend. Tests inject a fake
                here so no test touches a real filesystem or GCS bucket.
                Defaults to a store built from `config.account_store`.
            runner: Seam forwarded to the SignalCli this session yields.
            clock: Seam forwarded to the SignalCli this session yields, for
                reading the current time against the deadline. Tests inject
                a fake here so budget checks are deterministic.
            sleep: Seam forwarded to the SignalCli this session yields.
            invocation: The deadline anchor shared with every other session
                in this invocation. Defaults to the module-level one, which
                is what makes N groups in one Pub/Sub invocation share a
                single budget rather than getting one each.
            on_missing: What to do when no store exists yet. Only bootstrap
                commands (`link`, `register`) should pass EMPTY; defaulting
                to EMPTY here would silently mask an unlinked account.
        """
        self.config = config
        self.store = store or build_store(config.account_store)
        self.on_missing = on_missing

        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._invocation = invocation or INVOCATION_DEADLINE

    def __enter__(self) -> SignalCli:
        """Materialise the store and read the invocation's shared deadline."""
        data_dir = self.store.acquire(self.on_missing)

        return SignalCli(
            self.config.signal_number,
            data_dir,
            binary=self.config.signal_cli_path,
            trust_new_identities=self.config.trust_new_identities,
            deadline=self._invocation.at(
                self.config.invocation_budget_seconds, self._clock),
            runner=self._runner,
            sleep=self._sleep,
            clock=self._clock,
        )

    def __exit__(self,
                 exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> bool:
        """
        Persist the store, then let the more informative exception propagate.

        A release failure while the body already failed is logged rather than
        raised: the body's exception is what a caller's `except
        <SpecificType>` was written to catch, and letting release() replace
        it would break that catchability. A release failure with no body
        exception has nothing more informative to defer to, so it must
        propagate -- swallowing it would report success for a run that
        failed to persist the credential store.
        """
        try:
            self.store.release(exc)
        except Exception as release_error:
            if exc is None:
                raise

            logger.error(
                "store release failed while handling %r: %s",
                exc, release_error, exc_info=release_error)

        return False


STAMP_PREFIX = "autogroupchat"
STAMP_VERSION = 1

# listGroups reports no creation timestamp, so the creation date rides in the
# group description. Anchored at the start and versioned so the format can
# change later without stranding groups already created under version 1.
STAMP_RE = re.compile(
    rf"^{STAMP_PREFIX}:(?P<version>\d+):(?P<created>\d{{4}}-\d{{2}}-\d{{2}})\b")


def stamp_description(description: str, created: datetime.date) -> str:
    """Prefix a description with the machine-readable creation stamp."""
    return f"{STAMP_PREFIX}:{STAMP_VERSION}:{created.isoformat()} - {description}"


def parse_stamp(description: str | None) -> tuple[int, datetime.date] | None:
    """
    Read the stamp from a group description, or None when it is not ours.

    Returns the version even when unrecognised, so the caller can log "unknown
    stamp version" rather than silently treating a future format as foreign.

    A description that is not text at all (e.g. a malformed listing entry)
    cannot be our stamp either, so it is treated the same way as "not ours"
    rather than raising: re.match requires str/bytes, and a caller such as
    purge_groups must not be able to crash the whole sweep on one bad group.
    """
    if not isinstance(description, str) or not description:
        return None

    match = STAMP_RE.match(description)
    if not match:
        return None

    try:
        created = datetime.date.fromisoformat(match.group("created"))
    except ValueError:
        return None

    return (int(match.group("version")), created)


class PurgeDecision(Enum):
    """Why a group was or was not purged. Every value but PURGE means keep."""

    PURGE = "purge"
    NOT_OURS = "not-ours"
    UNKNOWN_STAMP_VERSION = "unknown-stamp-version"
    FUTURE_DATE = "future-date"
    TOO_YOUNG = "too-young"
    NOT_A_MEMBER = "not-a-member"


def purge_decision(group: dict[str, Any],
                   today: datetime.date,
                   max_age_days: int) -> PurgeDecision:
    """
    Decide whether one group should be purged. Pure, so it is cheap to test.

    Fail-closed by construction: every path that cannot prove the group is ours
    and old returns a keep decision. The asymmetry is deliberate. A missed purge
    leaves a stale group; a false positive abandons a live one.
    """
    if not group.get("isMember"):
        return PurgeDecision.NOT_A_MEMBER

    parsed = parse_stamp(group.get("description"))
    if parsed is None:
        return PurgeDecision.NOT_OURS

    version, created = parsed
    if version != STAMP_VERSION:
        return PurgeDecision.UNKNOWN_STAMP_VERSION

    if created > today:
        return PurgeDecision.FUTURE_DATE

    if (today - created) <= datetime.timedelta(days=int(max_age_days)):
        return PurgeDecision.TOO_YOUNG

    return PurgeDecision.PURGE


def _dir_size(path: str) -> int:
    """Total bytes under `path`, for reporting the store's real footprint."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue

    return total


class AutoMakeSignal(AutoMakeGroupChat):
    """
    Signal backend, driving signal-cli through SignalCli.

    The `group` handle passed between the ABC's methods is the base64 group id
    string, where the GroupMe backend passes a Group object. The ABC treats it
    as opaque, so this costs nothing.
    """

    def __init__(self,
                 config_file: str,
                 *,
                 store: AccountStore | None = None,
                 runner: Runner | None = None,
                 today: Callable[[], datetime.date] = datetime.date.today,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 invocation: InvocationDeadline | None = None) -> None:
        """
        Configure a Signal maker; opens no session and touches no store yet.

        Args:
            config_file: Path to configs/config_signal.json.
            store: Seam for the account-store backend, forwarded to
                SignalSession. Tests inject a fake here so no test touches
                a real filesystem or GCS bucket.
            runner: Seam for the subprocess call, forwarded to SignalSession.
                Tests inject a fake here so the real binary is never run.
            today: Seam for the purge stamp's creation date. Tests inject a
                fixed date here so stamped descriptions are deterministic.
            sleep: Seam for retry backoff waits, forwarded to SignalSession.
                Tests inject a fake here so no test sleeps in real time; a
                retry-exhaustion test would otherwise fall back to the real
                time.sleep and wait out the full backoff.
            clock: Seam for reading the current time against the deadline,
                forwarded to SignalSession. Tests inject a fake here so
                budget-exhaustion behaviour (e.g. purge_groups letting
                BudgetExhausted escape rather than swallowing it as a
                per-group failure) is deterministic instead of depending on
                real elapsed time.
            invocation: Seam for the deadline anchor shared across every
                session in this invocation, forwarded to SignalSession.
                Tests inject one here to assert that a second group does not
                get a second full budget.
        """
        super(AutoMakeSignal, self).__init__(config_file)

        self.signal_config = SignalConfig.from_dict(self.config)
        self._store = store
        self._runner = runner
        self._today = today
        self._sleep = sleep
        self._clock = clock
        self._invocation = invocation
        self._cli: SignalCli | None = None

    @contextlib.contextmanager
    def session(self, on_missing: OnMissing = OnMissing.ERROR) -> Iterator[SignalCli]:
        """
        Hold one account-store session for the duration of the block.

        Every operation below reads `self.cli`, which only exists inside here.
        """
        session = SignalSession(
            self.signal_config,
            store=self._store,
            runner=self._runner,
            sleep=self._sleep,
            clock=self._clock,
            invocation=self._invocation,
            on_missing=on_missing,
        )

        with session as cli:
            self._cli = cli
            try:
                yield cli
            finally:
                self._cli = None

    @property
    def cli(self) -> SignalCli:
        """The active driver, or a loud error when no session is open."""
        if self._cli is None:
            raise RuntimeError(
                "no signal-cli session is open; "
                "wrap this call in `with maker.session():`")

        return self._cli

    def create_group(self,
                     group_name: str,
                     image: str,
                     description: str) -> str:
        """
        Create a group and return its base64 id.

        The description is always stamped, including when the caller supplied
        its own: purge reads the creation date back out of it, so an unstamped
        group could never be cleaned up.
        """
        body = description or MESSAGE_ALWAYS_SEND
        stamped = stamp_description(body, self._today())

        group_id = self.cli.create_group(
            group_name, stamped, avatar=image or None)

        logger.info(f"created Signal group {group_name!r} with id {group_id}")

        return group_id

    def add_members_group(self, group: str, members: dict[str, str]) -> None:
        """
        Add every member in one call, falling back to one call each on rejection.

        signal-cli rejects the entire updateGroup call with exit 1 if any single
        number is not a registered Signal user, so the batch is retried
        per-member to isolate the bad ones. Matches AutoMakeGroupMe's tolerance
        of partial failure: log and continue rather than abort.

        The dict keys are display names. Signal has no per-group nickname -- each
        member shows their own profile name -- so the names are log labels only.
        """
        numbers = [number for number in members.values() if number]
        if not numbers:
            logger.info(f"no members to add to group {group}")
            return

        try:
            self.cli.update_group(group, members=numbers)
        except SignalCliError as e:
            if e.exit_code != int(ExitCode.USER_ERROR):
                raise

            logger.warning(
                f"batch add of {len(numbers)} members was rejected "
                f"({e.stderr.strip()}); retrying one at a time")
            self._add_members_individually(group, members)

        self._log_membership(group, members)

    def _add_members_individually(self,
                                  group: str,
                                  members: dict[str, str]) -> None:
        """
        Add members one by one so one bad number cannot block the rest.

        Only a USER_ERROR (not-registered) rejection is tolerated per member,
        matching the top-level dispatch in add_members_group. A transport
        error means the infrastructure is broken, not the number: logging it
        as a per-member failure and marching on to the next member would
        mislabel the cause and hammer the same broken call once per remaining
        member, for nothing.
        """
        for name, number in members.items():
            if not number:
                continue

            try:
                self.cli.update_group(group, members=[number])
            except SignalCliError as e:
                if e.exit_code != int(ExitCode.USER_ERROR):
                    raise

                logger.error(
                    f"could not add {name} ({number}) to {group}: "
                    f"{e.stderr.strip()}")

    def group_by_id(self, group_id: str) -> dict[str, Any] | None:
        """Find one group in listGroups output, or None."""
        for group in self.cli.list_groups():
            # listGroups spells the id "id"; updateGroup spells it "groupId".
            if group.get("id") == group_id:
                return group

        return None

    def _group_listing_or_none(self, group: str) -> dict[str, Any] | None:
        """
        group_by_id, tolerant of a failed re-read.

        Used both to decide whether add_member_group can skip a number
        already present, and to report status after an add. Either use sits
        on top of an add that may have already succeeded, so a failure here
        is logged and treated as "unknown" rather than raised -- it must
        never sink the add it is checking or describing.
        """
        try:
            return self.group_by_id(group)
        except SignalCliError as e:
            logger.warning(f"could not re-read group {group}: {e}")
            return None

    def _membership_sets(self,
                         listing: dict[str, Any]) -> tuple[set[str], set[str]]:
        """Split one listGroups entry into (joined numbers, pending numbers)."""
        joined = {m.get("number") for m in listing.get("members") or []}
        pending = {m.get("number") for m in listing.get("pendingMembers") or []}

        return joined, pending

    def _log_membership(self,
                        group: str,
                        members: dict[str, str],
                        listing: dict[str, Any] | None = None) -> None:
        """
        Report who actually joined, who was only invited, and who is absent.

        Signal adds a member as *pending* when we do not hold their profile key;
        they must accept before they are really in the group. Without this the
        caller cannot tell a successful add from an ignored invitation.

        `listing` lets a caller that already re-read the group (add_member_group's
        idempotency check) reuse it instead of asking again. This runs after an
        add that may have already succeeded, so a failed re-read is logged and
        swallowed here too, not raised: a diagnostic must never sink the
        operation it describes.
        """
        if listing is None:
            listing = self._group_listing_or_none(group)

        if listing is None:
            logger.warning(
                f"could not confirm membership for group {group} "
                f"(missing from listGroups, or the re-read failed)")
            return

        joined, pending = self._membership_sets(listing)

        for name, number in members.items():
            if number in joined:
                logger.info(f"{name} ({number}) joined {group}")
                continue

            if number in pending:
                logger.info(
                    f"{name} ({number}) is pending in {group}; "
                    f"they must accept the invitation")
                continue

            logger.error(f"{name} ({number}) is not in {group} at all")

    def add_member_group(self,
                         group: str,
                         name: str,
                         phone_number: str) -> None:
        """
        Add a single member, skipping when already present.

        Signature follows the ABC's *call site*, which passes the group first,
        rather than its declaration, which omits it. See spec section 15.

        signal-cli does not document what updateGroup -m does for a number
        already in the group, and group_startup adds the admin after the
        member batch -- so rather than find out, the group is re-read first
        and the add is skipped outright when the number is already joined or
        pending. That same read doubles as this call's status report when it
        skips; when it does add, the pre-add read cannot reflect the post-add
        state, so a fresh read follows instead -- reported exactly like
        add_members_group does, since a profile-key-less admin still lands
        pending, and an unconditional "added" here would hide that from the
        change_group_owner call that follows.
        """
        listing = self._group_listing_or_none(group)
        if listing is not None:
            joined, pending = self._membership_sets(listing)
            if phone_number in joined or phone_number in pending:
                logger.info(
                    f"{name} ({phone_number}) is already in {group}; "
                    f"skipping the add")
                self._log_membership(group, {name: phone_number}, listing=listing)
                return

        self.cli.update_group(group, members=[phone_number])

        self._log_membership(group, {name: phone_number})

    def change_group_owner(self,
                           group: str,
                           name: str,
                           phone_number: str) -> None:
        """
        Promote a member to admin.

        Signal has no owner: GV2 carries a set of admins, and we stay an admin
        too. "Owner" is a GroupMe-ism preserved by the ABC.
        """
        self.cli.update_group(group, admins=[phone_number])

        logger.info(f"made {name} ({phone_number}) an admin of {group}")

    def send_message_to_group(self, group: str, message: str) -> None:
        """Send one message to the group."""
        self.cli.send_group(group, message)

    def _successors_from_listing(self, listing: dict[str, Any]) -> list[str]:
        """
        Pick promotion candidates from an already-fetched group listing.

        Pure selection logic pulled out of successors_for_leave, so
        purge_groups can reuse the listing from its own list_groups() call
        instead of re-fetching once per purge candidate. Private: called only
        from within this class (successors_for_leave, purge_groups).

        Empty unless we are the only admin and somebody else remains. The pick
        is sorted by uuid so it is reproducible rather than dependent on dict
        order.
        """
        members = listing.get("members") or []
        self_number = self.signal_config.signal_number

        admins = [m for m in members if m.get("isAdmin")]
        others = [m for m in members if m.get("number") != self_number]

        we_are_sole_admin = (
            len(admins) == 1
            and admins[0].get("number") == self_number)

        if not we_are_sole_admin or not others:
            return []

        others.sort(key=lambda m: str(m.get("uuid") or ""))
        successor = others[0].get("number") or others[0].get("uuid")

        return [successor] if successor else []

    def successors_for_leave(self, group_id: str) -> list[str]:
        """
        Members to promote before leaving, which Signal requires of a last admin.

        Fetches the listing fresh via _group_listing_or_none, returning []
        when the read fails or the group is absent rather than guessing a
        successor blind. If we are in fact the sole admin, signal-cli will
        reject the quit with "You need to specify a new admin" -- the correct
        loud failure in that case.
        """
        listing = self._group_listing_or_none(group_id)
        if listing is None:
            return []

        return self._successors_from_listing(listing)

    def remove_self_group(self, group: str) -> None:
        """
        Leave a group, promoting a successor first if we are the only admin.

        This leaves rather than deletes. Signal has no destroy-group operation,
        so the group survives for its remaining members.
        """
        self.cli.quit_group(group, new_admins=self.successors_for_leave(group))

        logger.info(f"left Signal group {group}")

    def purge_groups(self, group_delete_age_days: int = 30) -> None:
        """
        Leave groups this tool created more than `group_delete_age_days` ago.

        Age comes from the stamp in the group description, because signal-cli
        reports no creation timestamp. Anything unparseable is left alone and
        logged; see purge_decision for the full fail-closed rule.

        Successor selection reuses the listing already in hand from the
        list_groups() call above via _successors_from_listing, rather than
        successors_for_leave's own re-fetch -- that would cost one redundant
        signal-cli call per purge candidate for no benefit.

        One group failing must not stop the others -- a group we cannot leave,
        or cannot even evaluate, should not strand the whole cleanup. This is
        why every per-group step below sits inside the loop's own try: a
        malformed entry (parse_stamp is guarded at its root against a
        non-string description, but that is one known failure mode, not every
        possible one) or a quit failure both log and move on to the next
        group instead of aborting the sweep. purge_groups runs at the tail of
        group_startup, so an abort here would silently skip the rest of that
        sweep's cleanup with nothing to retry it.

        BudgetExhausted is the one exception this tolerance does not cover.
        It is a run-level condition, not a per-group one -- its whole purpose
        is to stop work cleanly inside our own invocation budget rather than
        be killed mid-write by the platform. Logging it as "could not
        evaluate this group" and continuing would convert "we ran out of
        time" into a silently successful return, with the caller never
        learning the run did not finish. It is re-raised ahead of the
        catch-all below rather than swallowed with everything else.
        """
        today = self._today()

        for group in self.cli.list_groups():
            group_id = group.get("id")
            if not group_id:
                # An id-less entry can't be addressed by quitGroup at all --
                # signal-cli would see the literal string "None" as -g and
                # reject it. Skipping is the only safe action available.
                logger.error(
                    f"skipping group listing with no id: {group.get('name')!r}")
                continue

            try:
                decision = purge_decision(group, today, group_delete_age_days)

                if decision is not PurgeDecision.PURGE:
                    logger.info(
                        f"keeping group {group_id} ({group.get('name')!r}): "
                        f"{decision.value}")
                    continue

                self.cli.quit_group(
                    group_id, new_admins=self._successors_from_listing(group))
                logger.info(
                    f"purged group {group_id} ({group.get('name')!r})")
            except SignalCliError as e:
                logger.error(
                    f"could not purge group {group_id}: {e.stderr.strip()}")
            except BudgetExhausted:
                # Run-level, not per-group: let it escape rather than log a
                # misleading per-group failure and report the sweep as done.
                raise
            except Exception as e:
                # Safety net for failure modes neither purge_decision's own
                # guards nor this loop's callers anticipated -- one bad group
                # must still not abort the rest of the sweep.
                logger.error(
                    f"could not evaluate group {group_id} for purge: {e}")

    def doctor(self) -> list[str]:
        """
        Recompute the deployment's assumptions and return every problem found.

        Exists so the numbers in the design are checked rather than trusted:
        the version floor, whether the account is actually registered, and
        whether the store is usable at all.

        "Every problem in one run" holds only once the binary is actually
        invocable. A version below the floor is a problem that does not stop
        the rest of the checks -- it is appended and the store check still
        runs. A version() call that raises outright (the binary is missing or
        unusable) returns immediately instead: list_groups() would fail the
        same way for the same underlying reason, and reporting that as a
        second, seemingly independent problem would mislead rather than help.
        """
        problems: list[str] = []

        try:
            version = self.cli.version()
            if version < SIGNAL_CLI_MIN_VERSION:
                problems.append(
                    f"signal-cli {'.'.join(str(p) for p in version)} is older "
                    f"than the required "
                    f"{'.'.join(str(p) for p in SIGNAL_CLI_MIN_VERSION)}")
        except (SignalCliError, OSError, ValueError) as e:
            problems.append(f"could not run {self.signal_config.signal_cli_path}: {e}")
            return problems

        try:
            groups = self.cli.list_groups()
            logger.info(f"account is usable; it knows about {len(groups)} groups")
        except SignalCliError as e:
            problems.append(f"account store is not usable: {e.stderr.strip()}")

        size = _dir_size(self.cli.data_dir)
        logger.info(
            f"account store at {self.cli.data_dir} is {size / 1e6:.1f} MB; "
            f"set Cloud Run --memory from this plus headroom")

        return problems

    def group_startup(clazz,
                      config_file: str,
                      group_name: str,
                      members: dict[str, str],
                      admin: dict[str, str] = {},
                      startup_messages: list[str] = [],
                      image: str = None,
                      description: str = None,
                      dont_leave_group: bool = True,
                      group_delete_age_days: int = 30) -> str:
        """
        Signal override of AutoMakeGroupChat.group_startup.

        Reimplemented rather than inherited for two reasons:

        1. The whole sequence must run inside ONE account-store session. The
           store is downloaded on entry and uploaded on exit, and the ABC gives
           no teardown hook for a session opened in __init__. Wrapping the
           inherited method would build a second instance and, in the cloud,
           deadlock against the lock the first one holds.
        2. The ordering differs. GroupMe must promote the admin before adding
           members, because adding an existing member fails there. Signal has no
           such constraint, so members go in first and the admin is promoted
           afterwards, which is one fewer special case.

        Not decorated @classmethod, matching the ABC's existing convention of
        being called as `clazz.group_startup(clazz, ...)`.

        `admin`, when non-empty, is emptied by `.popitem()` -- inherited from
        the base class's own behaviour, not introduced here. A caller that
        reuses one `admin` dict across several `group_startup` calls will
        find promotion silently skipped from the second group onward.
        """
        agc = clazz(config_file)

        if not description:
            description = MESSAGE_ALWAYS_SEND

        if admin:
            assert len(admin) == 1, "Only one admin may be promoted per group."

        with agc.session():
            group = agc.create_group(group_name, image, description)

            agc.add_members_group(group, members)

            if admin:
                admin_name, admin_phone_number = admin.popitem()
                agc.add_member_group(group, admin_name, admin_phone_number)
                agc.change_group_owner(group, admin_name, admin_phone_number)

            agc.send_message_to_group(group, MESSAGE_ALWAYS_SEND)

            if not startup_messages:
                startup_messages = [f"Welcome to {group_name}. {description}"]

            for message in startup_messages:
                agc.send_message_to_group(group, message)

            if not dont_leave_group:
                agc.remove_self_group(group)

            agc.purge_groups(group_delete_age_days=group_delete_age_days)

        return group


def run(args: argparse.Namespace) -> None:
    """Create one group from command-line arguments."""
    members = {m.split(":")[0]: m.split(":")[1] for m in args.members if m}

    admin = {args.admin.split(":")[0]: args.admin.split(":")[1]} \
        if args.admin else {}

    AutoMakeSignal.group_startup(
        AutoMakeSignal,
        args.config_file,
        args.group_name,
        members,
        admin,
        args.startup_messages,
        args.image,
        args.description,
        args.dont_leave_group,
    )


def run_link(args: argparse.Namespace) -> None:
    """
    Link this account as a secondary device, then persist the new store.

    One-time and human-driven: SignalCli.link streams the sgnl:// URI to the
    operator as signal-cli emits it, then blocks until the phone scans it, at
    which point the session's exit persists the newly provisioned store.
    """
    maker = AutoMakeSignal(args.config_file)

    with maker.session(on_missing=OnMissing.EMPTY) as cli:
        logger.info("scan the URI below from Signal on your phone: "
                    "Settings > Linked Devices > +")
        cli.link(args.name)


def run_register(args: argparse.Namespace) -> None:
    """Register a dedicated number, rather than linking to an existing phone."""
    maker = AutoMakeSignal(args.config_file)

    with maker.session(on_missing=OnMissing.EMPTY) as cli:
        register_args = ["register"]
        if args.voice:
            register_args.append("--voice")

        if args.captcha:
            register_args += ["--captcha", args.captcha]

        cli.run(*register_args, retry=Retry.DISABLED)
        logger.info("registered; now run `verify` with the code you receive")


def run_verify(args: argparse.Namespace) -> None:
    """Complete registration with the code sent by Signal."""
    maker = AutoMakeSignal(args.config_file)

    with maker.session() as cli:
        cli.run("verify", args.code, retry=Retry.DISABLED)
        logger.info("verified")


def run_doctor(args: argparse.Namespace) -> None:
    """Check the deployment's assumptions; exit non-zero on any problem."""
    maker = AutoMakeSignal(args.config_file)

    with maker.session():
        problems = maker.doctor()

    for problem in problems:
        logger.error(problem)

    sys.exit(1 if problems else 0)


def build_parser() -> argparse.ArgumentParser:
    """Build the module's CLI. Subcommands mirror signal-cli's own vocabulary."""
    default_config = (
        f"{os.path.dirname(__file__)}/../../configs/config_signal.json")

    parser = argparse.ArgumentParser(
        description="Create Signal groups with signal-cli")
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument("-g", "--config-file", default=default_config,
                        help="json configuration file specifying credentials")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a group")
    create.add_argument("group_name")
    create.add_argument("members", nargs="+",
                        help="members as 'Name:+15551234567'")
    create.add_argument("-a", "--admin", default="",
                        help="member to promote to admin, as 'Name:+1555...'")
    create.add_argument("-s", "--startup-messages", nargs="+", default=[],
                        help="messages to send after forming the group")
    create.add_argument("--image", default="")
    create.add_argument("--description", default=MESSAGE_ALWAYS_SEND,
                        help="Don't make this dynamic. Purge relies on the "
                             "stamped prefix this becomes.")
    create.add_argument("--dont-leave-group", action='store_true')
    create.set_defaults(func=run)

    link = subparsers.add_parser(
        "link", help="link to an existing Signal account on your phone")
    link.add_argument("--name", default="autogroupchat",
                      help="device name shown in Signal's Linked Devices")
    link.set_defaults(func=run_link)

    register = subparsers.add_parser(
        "register", help="register a dedicated number")
    register.add_argument("--voice", action="store_true",
                          help="verify by voice call instead of SMS")
    register.add_argument("--captcha", default="",
                          help="captcha token, if registration was refused")
    register.set_defaults(func=run_register)

    verify = subparsers.add_parser("verify", help="finish registration")
    verify.add_argument("code", help="the verification code Signal sent")
    verify.set_defaults(func=run_verify)

    doctor = subparsers.add_parser(
        "doctor", help="check the binary, account and store")
    doctor.set_defaults(func=run_doctor)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=f'[{log_level}] %(message)s')
    logger = logging.getLogger(__name__)

    args.func(args)
    sys.exit()
