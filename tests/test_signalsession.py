"""Tests for the account-store session lifecycle (spec section 4)."""

import logging

import pytest

from autogroupchat.makers.automakesignal import SignalConfig, SignalSession
from autogroupchat.makers.signalaccountstore import AccountStore, OnMissing
from tests.conftest import FakeClock, FakeRunner

CONFIG = SignalConfig.from_dict({
    "signal_number": "+15551234567",
    "account_store": {"type": "local", "data_dir": "/unused"},
    "invocation_budget_seconds": 300,
})


class RecordingStore(AccountStore):
    """Records acquire/release without touching a filesystem."""

    def __init__(self, data_dir: str = "/tmp/materialised") -> None:
        self.data_dir = data_dir
        self.acquired: list[OnMissing] = []
        self.released: list[BaseException | None] = []

    def acquire(self, on_missing: OnMissing = OnMissing.ERROR) -> str:
        self.acquired.append(on_missing)
        return self.data_dir

    def release(self, error: BaseException | None = None) -> None:
        self.released.append(error)


def test_session_yields_a_cli_bound_to_the_materialised_dir():
    store = RecordingStore("/tmp/materialised")
    runner = FakeRunner()

    with SignalSession(CONFIG, store=store, runner=runner) as cli:
        assert cli.data_dir == "/tmp/materialised"
        assert cli.number == "+15551234567"

    assert store.acquired == [OnMissing.ERROR]


def test_session_sets_the_deadline_from_the_configured_budget():
    store = RecordingStore()
    clock = FakeClock(now=1_000.0)

    with SignalSession(CONFIG, store=store, runner=FakeRunner(),
                       clock=clock) as cli:
        assert cli.deadline == 1_300.0


def test_session_releases_the_store_on_success():
    store = RecordingStore()

    with SignalSession(CONFIG, store=store, runner=FakeRunner()):
        pass

    assert store.released == [None]


def test_session_releases_the_store_on_failure_and_reraises():
    """
    The store must be persisted even when the run failed: anything already sent
    has advanced recipients' ratchet state.
    """
    store = RecordingStore()
    boom = RuntimeError("send blew up")

    with pytest.raises(RuntimeError):
        with SignalSession(CONFIG, store=store, runner=FakeRunner()):
            raise boom

    assert store.released == [boom]


def test_session_forwards_the_bootstrap_mode():
    store = RecordingStore()

    with SignalSession(CONFIG, store=store, runner=FakeRunner(),
                       on_missing=OnMissing.EMPTY):
        pass

    assert store.acquired == [OnMissing.EMPTY]


def test_session_builds_a_store_from_config_when_none_is_injected(tmp_path):
    config = SignalConfig.from_dict({
        "signal_number": "+15551234567",
        "account_store": {"type": "local", "data_dir": str(tmp_path)},
    })

    with SignalSession(config, runner=FakeRunner()) as cli:
        assert cli.data_dir == str(tmp_path)


class ReleaseRaisingStore(AccountStore):
    """A store whose release() always raises, to test __exit__'s asymmetry."""

    def __init__(self, release_error: BaseException,
                 data_dir: str = "/tmp/materialised") -> None:
        self.data_dir = data_dir
        self.release_error = release_error

    def acquire(self, on_missing: OnMissing = OnMissing.ERROR) -> str:
        return self.data_dir

    def release(self, error: BaseException | None = None) -> None:
        raise self.release_error


def test_exit_prefers_the_bodys_exception_over_a_release_failure(caplog):
    """
    When both the body and release() fail, the body's exception is the more
    informative one -- it is what a caller's `except <SpecificType>` was
    written to catch -- so it must be what escapes. The release failure is
    not swallowed silently; it is logged.
    """
    store = ReleaseRaisingStore(ValueError("upload failed"))
    boom = RuntimeError("send blew up")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            with SignalSession(CONFIG, store=store, runner=FakeRunner()):
                raise boom

    assert "upload failed" in caplog.text


def test_exit_propagates_a_release_failure_when_the_body_succeeded():
    """
    With no body exception to prefer, a release failure must propagate:
    swallowing it would report success for a run that failed to persist the
    credential store.
    """
    store = ReleaseRaisingStore(ValueError("upload failed"))

    with pytest.raises(ValueError):
        with SignalSession(CONFIG, store=store, runner=FakeRunner()):
            pass
