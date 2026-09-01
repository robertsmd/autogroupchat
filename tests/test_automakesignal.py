"""Tests for the AutoMakeSignal maker (spec sections 8, 9)."""

import datetime
import json

import pytest

from autogroupchat.makers.automakegroupchat import MESSAGE_ALWAYS_SEND
from autogroupchat.makers.automakesignal import (
    BACKOFF_SERVER_OR_IO,
    STAMP_VERSION,
    AutoMakeSignal,
    ExitCode,
    SignalCliError,
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
    """
    Build a maker wired to `runner`, with a no-op sleeper.

    Without a sleeper seam, a test that exhausts the SERVER_OR_IO backoff
    (1+2+4+8 = 15s) would sleep for real; FakeSleeper records durations
    instead.
    """
    return AutoMakeSignal(
        config_file, store=FakeStore(), runner=runner, today=lambda: today,
        sleep=FakeSleeper())


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
    12345,
    {"not": "a string"},
])
def test_parse_stamp_refuses_anything_off_grammar(description):
    """
    Fail-closed: an unparseable description must not become a purge candidate.
    The anchor matters -- a stamp anywhere but the start does not count.

    Review finding (Important 1): a non-string description (int, dict -- a
    malformed listing entry) must not raise. re.match requires str/bytes, so
    without the isinstance guard this crashes purge_decision and, through it,
    the whole purge_groups sweep on a single bad group.
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


MEMBERS = {"Alice": "+15551112222", "Bob": "+15553334444"}


def group_listing(**overrides) -> str:
    """One listGroups entry, shaped like ListGroupsCommand's JsonGroup record."""
    group = {
        "id": "gid",
        "name": "Test Group",
        "description": stamp_description(MESSAGE_ALWAYS_SEND, TODAY),
        "isMember": True,
        "isBlocked": False,
        "messageExpirationTime": 0,
        "members": [
            {"number": "+15551234567", "uuid": "uuid-self", "isAdmin": True},
        ],
        "pendingMembers": [],
        "requestingMembers": [],
        "admins": [],
        "banned": [],
        "groupInviteLink": None,
    }
    group.update(overrides)

    return json.dumps([group])


def test_add_members_sends_one_batched_call(config_file):
    runner = FakeRunner()
    runner.queue(0, "{}")
    runner.queue(0, group_listing())
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.add_members_group("gid", MEMBERS)

    update = runner.calls[0]
    assert update[update.index("-m") + 1:] == ["+15551112222", "+15553334444"]


def test_add_members_falls_back_to_one_call_each_on_user_error(config_file):
    """
    signal-cli raises 'The user X is not registered.' (exit 1) for any single
    unregistered number, which aborts the whole batch. Falling back isolates
    the bad numbers so the good ones still land.
    """
    runner = FakeRunner()
    runner.queue(int(ExitCode.USER_ERROR), "", "The user +15553334444 is not registered.")
    runner.queue(0, "{}")
    runner.queue(int(ExitCode.USER_ERROR), "", "The user +15553334444 is not registered.")
    runner.queue(0, group_listing())

    maker = make_maker(config_file, runner)

    with maker.session():
        maker.add_members_group("gid", MEMBERS)

    per_member = [c for c in runner.calls if "-m" in c]
    assert len(per_member) == 3
    assert per_member[1][per_member[1].index("-m") + 1:] == ["+15551112222"]
    assert per_member[2][per_member[2].index("-m") + 1:] == ["+15553334444"]


def test_add_members_does_not_fall_back_on_a_transport_error(config_file):
    """Exit 3 is retried inside SignalCli; a per-member retry storm on top of
    that would multiply calls against the invocation budget for nothing."""
    runner = FakeRunner()
    for _ in range(len(BACKOFF_SERVER_OR_IO) + 1):
        runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")

    maker = make_maker(config_file, runner)

    with pytest.raises(SignalCliError):
        with maker.session():
            maker.add_members_group("gid", MEMBERS)


def test_add_members_with_no_numbers_makes_no_call(config_file):
    runner = FakeRunner()
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.add_members_group("gid", {})

    assert runner.calls == []


def test_add_members_logs_the_pending_split(config_file, caplog):
    """
    Members whose profile key we lack are invited, not added. Silence here would
    read as success when nobody actually joined.
    """
    runner = FakeRunner()
    runner.queue(0, "{}")
    runner.queue(0, group_listing(
        members=[
            {"number": "+15551234567", "uuid": "uuid-self", "isAdmin": True},
            {"number": "+15551112222", "uuid": "uuid-alice", "isAdmin": False},
        ],
        pendingMembers=[{"number": "+15553334444", "uuid": "uuid-bob"}],
    ))

    maker = make_maker(config_file, runner)

    with caplog.at_level("INFO"):
        with maker.session():
            maker.add_members_group("gid", MEMBERS)

    logged = caplog.text
    assert "+15553334444" in logged
    assert "pending" in logged.lower()


def test_add_members_individually_reraises_on_transport_error(config_file):
    """
    Review finding (Important 1): the per-member fallback must not swallow a
    transport error the way it swallows a not-registered rejection. Without
    the exit-code check, a SERVER_OR_IO failure (already retried and
    exhausted inside SignalCli) reads as "this number is bad" and the loop
    marches on to hammer the same broken infrastructure for every remaining
    member, burning the invocation budget for nothing.
    """
    members = {
        "Alice": "+15551112222",
        "Bob": "+15553334444",
        "Carol": "+15556667777",
    }
    runner = FakeRunner()
    runner.queue(
        int(ExitCode.USER_ERROR), "", "The user +15553334444 is not registered.")
    runner.queue(0, "{}")  # Alice's individual add succeeds
    for _ in range(len(BACKOFF_SERVER_OR_IO) + 1):
        runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")  # Bob's calls

    maker = make_maker(config_file, runner)

    with pytest.raises(SignalCliError) as excinfo:
        with maker.session():
            maker.add_members_group("gid", members)

    assert excinfo.value.exit_code == int(ExitCode.SERVER_OR_IO)
    # 1 batch call + 1 for Alice + Bob's exhausted retries. Carol's turn must
    # never come: the loop has to stop at Bob, not log-and-continue past his
    # transport error.
    assert len(runner.calls) == 1 + 1 + (len(BACKOFF_SERVER_OR_IO) + 1)


def test_add_members_tolerates_a_failing_membership_reread(config_file, caplog):
    """
    Review finding (Important 2): a failed post-add re-read is a diagnostic
    describing an add that already succeeded, and must not sink it. Without
    a guard, list_groups raising here escapes add_members_group even though
    both members were just added -- and the caller never reaches
    change_group_owner or send_message_to_group.
    """
    runner = FakeRunner()
    runner.queue(0, "{}")  # the batched updateGroup succeeds
    runner.queue(int(ExitCode.UNEXPECTED), "", "listGroups exploded")

    maker = make_maker(config_file, runner)

    with caplog.at_level("WARNING"):
        with maker.session():
            maker.add_members_group("gid", MEMBERS)  # must not raise

    assert "gid" in caplog.text


def test_add_member_group_logs_pending_not_added(config_file, caplog):
    """
    Review finding (Important 3): without a re-read, a member who lands in
    pendingMembers (profile key unknown) was logged as unconditionally
    "added" -- an actively wrong claim, not mere silence. group_startup adds
    the admin through this path, so a wrongly-"added" admin could be
    followed by a change_group_owner call against someone who never joined.
    """
    runner = FakeRunner()
    runner.queue(0, group_listing())  # pre-add: Alice is not there yet
    runner.queue(0, "{}")  # updateGroup succeeds
    runner.queue(0, group_listing(
        pendingMembers=[{"number": "+15551112222", "uuid": "uuid-alice"}]))

    maker = make_maker(config_file, runner)

    with caplog.at_level("INFO"):
        with maker.session():
            maker.add_member_group("gid", "Alice", "+15551112222")

    logged = caplog.text.lower()
    assert "pending" in logged
    assert "added" not in logged


def test_add_member_group_skips_an_already_present_number(config_file, caplog):
    """
    Review finding (Also): rather than rely on undocumented updateGroup
    behaviour for a number already in the group, skip the call outright.
    group_startup adds the admin after the member batch, so a re-add of the
    same number is not a hypothetical.
    """
    runner = FakeRunner()
    runner.queue(0, group_listing(
        members=[
            {"number": "+15551234567", "uuid": "uuid-self", "isAdmin": True},
            {"number": "+15551112222", "uuid": "uuid-alice", "isAdmin": False},
        ],
    ))

    maker = make_maker(config_file, runner)

    with caplog.at_level("INFO"):
        with maker.session():
            maker.add_member_group("gid", "Alice", "+15551112222")

    assert len(runner.calls) == 1
    assert "listGroups" in runner.calls[0]
    assert "skip" in caplog.text.lower()


def test_add_member_group_takes_the_group_first(config_file):
    """
    The ABC calls this as add_member_group(group, name, number) while declaring
    (self, name, phone_number). The call site wins; see spec section 15.

    add_member_group now re-reads the group before and after the add (see the
    idempotency and pending-status regression tests below), so the updateGroup
    call is no longer necessarily runner.last -- it is located by content
    instead.
    """
    runner = FakeRunner().queue(0, "{}")
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.add_member_group("gid", "Alice", "+15551112222")

    argv = next(c for c in runner.calls if "updateGroup" in c)
    assert argv[argv.index("-g") + 1] == "gid"
    assert argv[argv.index("-m") + 1] == "+15551112222"


def test_change_group_owner_promotes_to_admin(config_file):
    """Signal has no owner. GV2 has a set of admins, so 'owner' means admin."""
    runner = FakeRunner().queue(0, "{}")
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.change_group_owner("gid", "Alice", "+15551112222")

    argv = runner.last
    assert argv[argv.index("--admin") + 1] == "+15551112222"


def test_send_message_to_group(config_file):
    runner = FakeRunner().queue(0, "{}")
    maker = make_maker(config_file, runner)

    with maker.session():
        maker.send_message_to_group("gid", "hello everyone")

    argv = runner.last
    assert argv[argv.index("send"):] == ["send", "-g", "gid", "-m", "hello everyone"]


def test_group_by_id_returns_none_when_absent(config_file):
    runner = FakeRunner().queue(0, group_listing())
    maker = make_maker(config_file, runner)

    with maker.session():
        assert maker.group_by_id("no-such-group") is None


from autogroupchat.makers.automakesignal import PurgeDecision, purge_decision

OLD = datetime.date(2026, 1, 1)
SELF = "+15551234567"


def listing_entry(**overrides) -> dict:
    """One JsonGroup-shaped dict for purge_decision, defaulting to purgeable."""
    entry = {
        "id": "gid",
        "name": "Old Group",
        "description": stamp_description(MESSAGE_ALWAYS_SEND, OLD),
        "isMember": True,
        "members": [{"number": SELF, "uuid": "uuid-self", "isAdmin": True}],
        "pendingMembers": [],
    }
    entry.update(overrides)

    return entry


def test_purge_decision_purges_a_stamped_old_group():
    assert purge_decision(listing_entry(), TODAY, 30) is PurgeDecision.PURGE


def test_purge_decision_keeps_a_young_group():
    entry = listing_entry(
        description=stamp_description(MESSAGE_ALWAYS_SEND,
                                      TODAY - datetime.timedelta(days=5)))

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.TOO_YOUNG


def test_purge_decision_keeps_a_group_exactly_at_the_age_limit():
    """Strictly greater than, so a group is kept on its birthday boundary."""
    entry = listing_entry(
        description=stamp_description(MESSAGE_ALWAYS_SEND,
                                      TODAY - datetime.timedelta(days=30)))

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.TOO_YOUNG


@pytest.mark.parametrize("description", [
    None,
    "",
    MESSAGE_ALWAYS_SEND,
    "a group somebody else made",
    "autogroupchat:1:garbage",
])
def test_purge_decision_keeps_anything_it_cannot_parse(description):
    entry = listing_entry(description=description)

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.NOT_OURS


def test_purge_decision_keeps_an_unknown_stamp_version():
    """A newer format means newer code wrote it; deleting on a guess is wrong."""
    entry = listing_entry(description="autogroupchat:99:2020-01-01 newer format")

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.UNKNOWN_STAMP_VERSION


def test_purge_decision_keeps_a_future_dated_group():
    """A future date is a clock or format bug, not an old group."""
    entry = listing_entry(
        description=stamp_description(MESSAGE_ALWAYS_SEND,
                                      TODAY + datetime.timedelta(days=1)))

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.FUTURE_DATE


def test_purge_decision_skips_groups_we_already_left():
    entry = listing_entry(isMember=False)

    assert purge_decision(entry, TODAY, 30) is PurgeDecision.NOT_A_MEMBER


def test_purge_quits_only_the_purgeable_group(config_file):
    runner = FakeRunner()
    keep = listing_entry(id="keep", description=MESSAGE_ALWAYS_SEND)
    purge = listing_entry(id="purge")
    runner.queue(0, json.dumps([keep, purge]))
    runner.queue(0, "{}")

    maker = make_maker(config_file, runner)

    with maker.session():
        maker.purge_groups(group_delete_age_days=30)

    quits = [c for c in runner.calls if "quitGroup" in c]
    assert len(quits) == 1
    assert quits[0][quits[0].index("-g") + 1] == "purge"
    assert "--delete" in quits[0]


def test_purge_continues_after_one_group_fails(config_file):
    """One un-leavable group must not strand the rest of the cleanup."""
    runner = FakeRunner()
    runner.queue(0, json.dumps([listing_entry(id="first"),
                                listing_entry(id="second")]))
    runner.queue(int(ExitCode.USER_ERROR), "", "cannot leave")
    runner.queue(0, "{}")

    maker = make_maker(config_file, runner)

    with maker.session():
        maker.purge_groups(group_delete_age_days=30)

    quits = [c for c in runner.calls if "quitGroup" in c]
    assert len(quits) == 2


def test_purge_continues_past_a_malformed_entry(config_file):
    """
    Review finding (Important 1): a malformed listing entry -- here a
    non-string description, reproducing signal-cli returning something
    unexpected -- must not abort the sweep for the groups around it.
    Without the parse_stamp guard (and the loop's own catch-all), this
    raised TypeError out of purge_decision and 'second' was never evaluated.
    """
    runner = FakeRunner()
    malformed = listing_entry(id="bad", description=12345)
    runner.queue(0, json.dumps([listing_entry(id="first"), malformed,
                                listing_entry(id="second")]))
    runner.queue(0, "{}")
    runner.queue(0, "{}")

    maker = make_maker(config_file, runner)

    with maker.session():
        maker.purge_groups(group_delete_age_days=30)

    quits = [c for c in runner.calls if "quitGroup" in c]
    quit_ids = [q[q.index("-g") + 1] for q in quits]
    assert quit_ids == ["first", "second"]


def test_purge_skips_listing_entries_with_no_id(config_file, caplog):
    """
    Review finding (Minor): an id-less entry can't be passed to quitGroup at
    all -- signal-cli would see the literal string "None" as -g and reject
    it. Skip rather than attempt it.
    """
    entry = listing_entry()
    del entry["id"]
    runner = FakeRunner().queue(0, json.dumps([entry]))

    maker = make_maker(config_file, runner)

    with caplog.at_level("ERROR"):
        with maker.session():
            maker.purge_groups(group_delete_age_days=30)

    quits = [c for c in runner.calls if "quitGroup" in c]
    assert quits == []
    assert "no id" in caplog.text.lower()


def test_successors_names_a_member_when_we_are_the_only_admin(config_file):
    runner = FakeRunner().queue(0, json.dumps([listing_entry(members=[
        {"number": SELF, "uuid": "uuid-self", "isAdmin": True},
        {"number": "+15559999999", "uuid": "uuid-b", "isAdmin": False},
        {"number": "+15558888888", "uuid": "uuid-a", "isAdmin": False},
    ])]))
    maker = make_maker(config_file, runner)

    with maker.session():
        # Sorted by uuid so the choice is reproducible, not arbitrary.
        assert maker.successors_for_leave("gid") == ["+15558888888"]


def test_successors_is_empty_when_another_admin_remains(config_file):
    runner = FakeRunner().queue(0, json.dumps([listing_entry(members=[
        {"number": SELF, "uuid": "uuid-self", "isAdmin": True},
        {"number": "+15559999999", "uuid": "uuid-b", "isAdmin": True},
    ])]))
    maker = make_maker(config_file, runner)

    with maker.session():
        assert maker.successors_for_leave("gid") == []


def test_successors_is_empty_when_we_are_the_last_member(config_file):
    runner = FakeRunner().queue(0, json.dumps([listing_entry()]))
    maker = make_maker(config_file, runner)

    with maker.session():
        assert maker.successors_for_leave("gid") == []


def test_remove_self_designates_a_successor_admin(config_file):
    runner = FakeRunner()
    runner.queue(0, json.dumps([listing_entry(members=[
        {"number": SELF, "uuid": "uuid-self", "isAdmin": True},
        {"number": "+15559999999", "uuid": "uuid-b", "isAdmin": False},
    ])]))
    runner.queue(0, "{}")

    maker = make_maker(config_file, runner)

    with maker.session():
        maker.remove_self_group("gid")

    quit_argv = [c for c in runner.calls if "quitGroup" in c][0]
    assert quit_argv[quit_argv.index("--admin") + 1] == "+15559999999"
