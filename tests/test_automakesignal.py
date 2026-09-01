"""Tests for the AutoMakeSignal maker (spec sections 8, 9)."""

import datetime
import json

import pytest

from autogroupchat.makers.automakegroupchat import MESSAGE_ALWAYS_SEND
from autogroupchat.makers.automakesignal import (
    STAMP_VERSION,
    AutoMakeSignal,
    ExitCode,
    parse_stamp,
    stamp_description,
)
from tests.conftest import FakeRunner, FakeSleeper

TODAY = datetime.date(2026, 8, 31)

CONFIG_JSON = {
    "signal_number": "+15551234567",
    "account_store": {"type": "local", "data_dir": "/unused"},
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config_signal.json"
    path.write_text(json.dumps(CONFIG_JSON))
    return str(path)


class FakeStore:
    """Minimal AccountStore stand-in; no filesystem, no locking."""

    def __init__(self, data_dir: str = "/tmp/store") -> None:
        self.data_dir = data_dir

    def acquire(self, on_missing=None) -> str:
        return self.data_dir

    def release(self, error=None) -> None:
        return None


def make_maker(config_file, runner: FakeRunner, today: datetime.date = TODAY):
    return AutoMakeSignal(
        config_file, store=FakeStore(), runner=runner, today=lambda: today)


def test_stamp_description_uses_the_documented_grammar():
    stamped = stamp_description("hello", datetime.date(2026, 8, 31))

    assert stamped.startswith("autogroupchat:1:2026-08-31")
    assert "hello" in stamped


def test_parse_stamp_round_trips():
    stamped = stamp_description(MESSAGE_ALWAYS_SEND, TODAY)

    assert parse_stamp(stamped) == (STAMP_VERSION, TODAY)


@pytest.mark.parametrize("description", [
    None,
    "",
    MESSAGE_ALWAYS_SEND,
    "autogroupchat:2026-08-31 missing the version",
    "autogroupchat:1:31-08-2026 wrong date order",
    "autogroupchat:1:not-a-date",
    " autogroupchat:1:2026-08-31 leading space",
    "prefixed autogroupchat:1:2026-08-31",
])
def test_parse_stamp_refuses_anything_off_grammar(description):
    """
    Fail-closed: an unparseable description must not become a purge candidate.
    The anchor matters -- a stamp anywhere but the start does not count.
    """
    assert parse_stamp(description) is None


def test_parse_stamp_reports_an_unknown_version_rather_than_guessing():
    parsed = parse_stamp("autogroupchat:99:2026-08-31 from the future")

    assert parsed == (99, datetime.date(2026, 8, 31))


def test_create_group_stamps_the_description(config_file):
    runner = FakeRunner().queue(0, json.dumps({"groupId": "gid"}))
    maker = make_maker(config_file, runner)

    with maker.session():
        group_id = maker.create_group("Test Group", "", MESSAGE_ALWAYS_SEND)

    assert group_id == "gid"
    argv = runner.last
    description = argv[argv.index("-d") + 1]

    assert parse_stamp(description) == (STAMP_VERSION, TODAY)
    assert MESSAGE_ALWAYS_SEND in description


def test_create_group_stamps_a_custom_description_too(config_file):
    """Without the stamp the group can never be purged, so it is not optional."""
    runner = FakeRunner().queue(0, json.dumps({"groupId": "gid"}))
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.create_group("Test Group", "", "a custom description")

    argv = runner.last
    description = argv[argv.index("-d") + 1]

    assert parse_stamp(description) == (STAMP_VERSION, TODAY)
    assert "a custom description" in description


def test_create_group_falls_back_to_the_shared_marker(config_file):
    runner = FakeRunner().queue(0, json.dumps({"groupId": "gid"}))
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.create_group("Test Group", "", "")

    argv = runner.last

    assert MESSAGE_ALWAYS_SEND in argv[argv.index("-d") + 1]


def test_create_group_passes_the_image_as_an_avatar(config_file):
    runner = FakeRunner().queue(0, json.dumps({"groupId": "gid"}))
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.create_group("Test Group", "/tmp/avatar.png", "desc")

    argv = runner.last
    after = argv[argv.index("updateGroup"):]

    assert after[after.index("-a") + 1] == "/tmp/avatar.png"


def test_using_the_cli_outside_a_session_raises(config_file):
    maker = make_maker(config_file, FakeRunner())

    with pytest.raises(RuntimeError):
        maker.create_group("Test Group", "", "desc")


def test_sleep_seam_is_forwarded_so_retries_never_sleep_for_real(config_file):
    """
    Addition A1: sleep is an injectable seam, forwarded through to the
    SignalCli a session yields. Without this, a retry-exhaustion test would
    fall back to the real time.sleep and wait out the full backoff.
    """
    runner = (FakeRunner()
              .queue(int(ExitCode.SERVER_OR_IO), "", "transient")
              .queue(0, "{}"))
    sleep = FakeSleeper()
    maker = AutoMakeSignal(
        config_file, store=FakeStore(), runner=runner, today=lambda: TODAY,
        sleep=sleep)

    with maker.session() as cli:
        cli.run("someCommand")

    assert sleep.slept == [1]


def test_shared_marker_constant_is_untouched():
    """
    AutoMakeGroupMe.purge_groups compares descriptions against this exactly.
    Changing it would silently orphan every existing GroupMe group.
    """
    assert MESSAGE_ALWAYS_SEND == (
        "Group created by autogroupchat. "
        "Please contact s41l8hu2@duck.com with any issues.")
