"""Tests for the account-store session lifecycle (spec section 4)."""

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
