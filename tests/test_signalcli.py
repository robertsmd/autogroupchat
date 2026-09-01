"""Tests for the SignalCli driver (spec sections 7, 10)."""

import pytest

from autogroupchat.makers.automakesignal import (
    BACKOFF_RATE_LIMIT,
    BACKOFF_SERVER_OR_IO,
    BudgetExhausted,
    ExitCode,
    Retry,
    SignalCli,
    SignalCliError,
)
from tests.conftest import FakeClock, FakeRunner, FakeSleeper

NUMBER = "+15551234567"
DATA_DIR = "/tmp/signal-cli"


def make_cli(runner: FakeRunner | None = None, **kwargs) -> SignalCli:
    return SignalCli(
        NUMBER, DATA_DIR,
        binary="/opt/signal-cli/signal-cli",
        runner=runner or FakeRunner(),
        **kwargs,
    )


def test_globals_precede_the_subcommand():
    """
    The -a collision is the reason this test exists: global -a is --account,
    but updateGroup's -a is --avatar and send's -a is --attachment. If a global
    ever lands after the subcommand, signal-cli silently reinterprets it.
    """
    cli = make_cli()

    argv = cli.argv("updateGroup", "-n", "Test Group")

    assert argv[0] == "/opt/signal-cli/signal-cli"
    subcommand_at = argv.index("updateGroup")
    globals_part = argv[1:subcommand_at]

    assert "-a" in globals_part
    assert globals_part[globals_part.index("-a") + 1] == NUMBER
    assert argv[subcommand_at:] == ["updateGroup", "-n", "Test Group"]


def test_globals_include_json_output_data_dir_and_trust_mode():
    cli = make_cli(trust_new_identities="always")

    argv = cli.argv("listGroups")
    globals_part = argv[1:argv.index("listGroups")]

    assert "--output=json" in globals_part
    assert globals_part[globals_part.index("--data-dir") + 1] == DATA_DIR
    assert globals_part[globals_part.index("--trust-new-identities") + 1] == "always"


def test_data_dir_is_always_explicit():
    """
    Relying on $XDG_DATA_HOME would silently operate on an empty account in the
    cloud, where the store is materialised somewhere else entirely.
    """
    cli = make_cli()

    assert "--data-dir" in cli.argv("listGroups")


def test_argv_rejects_a_subcommand_that_looks_like_a_flag():
    cli = make_cli()

    with pytest.raises(ValueError):
        cli.argv("-a")


def test_success_returns_stdout():
    runner = FakeRunner().queue(0, '{"ok": true}')
    cli = make_cli(runner)

    assert cli.run("listGroups") == '{"ok": true}'


@pytest.mark.parametrize("code", [
    ExitCode.USER_ERROR,
    ExitCode.UNEXPECTED,
    ExitCode.UNTRUSTED_KEY,
    ExitCode.CAPTCHA_REJECTED,
])
def test_non_retryable_codes_raise_on_first_attempt(code):
    runner = FakeRunner().queue(int(code), "", "boom")
    cli = make_cli(runner, sleep=FakeSleeper())

    with pytest.raises(SignalCliError) as exc:
        cli.run("listGroups")

    assert exc.value.exit_code == int(code)
    assert len(runner.calls) == 1


def test_server_or_io_error_retries_with_documented_backoff():
    runner = FakeRunner()
    for _ in range(3):
        runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(0, "recovered")

    sleeper = FakeSleeper()
    cli = make_cli(runner, sleep=sleeper)

    assert cli.run("listGroups") == "recovered"
    assert len(runner.calls) == 4
    assert sleeper.slept == list(BACKOFF_SERVER_OR_IO[:3])


def test_rate_limit_uses_its_own_longer_backoff():
    runner = FakeRunner()
    runner.queue(int(ExitCode.RATE_LIMIT), "", "slow down")
    runner.queue(0, "ok")

    sleeper = FakeSleeper()
    cli = make_cli(runner, sleep=sleeper)

    assert cli.run("listGroups") == "ok"
    assert sleeper.slept == [BACKOFF_RATE_LIMIT[0]]


def test_retries_are_exhausted_then_raise():
    runner = FakeRunner()
    for _ in range(len(BACKOFF_SERVER_OR_IO) + 1):
        runner.queue(int(ExitCode.SERVER_OR_IO), "", "still sad")

    cli = make_cli(runner, sleep=FakeSleeper())

    with pytest.raises(SignalCliError):
        cli.run("listGroups")

    assert len(runner.calls) == len(BACKOFF_SERVER_OR_IO) + 1


def test_retry_disabled_never_retries_a_retryable_code():
    """
    create_group depends on this: a retried updateGroup with no -g creates a
    second group, so a transport error must surface rather than duplicate.
    """
    runner = FakeRunner()
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(0, "would-have-been-a-duplicate")

    cli = make_cli(runner, sleep=FakeSleeper())

    with pytest.raises(SignalCliError):
        cli.run("updateGroup", "-n", "Test", retry=Retry.DISABLED)

    assert len(runner.calls) == 1


def test_exhausted_budget_launches_no_subprocess_at_all():
    runner = FakeRunner()
    clock = FakeClock(now=100.0)
    cli = make_cli(runner, deadline=100.0, clock=clock, sleep=FakeSleeper())

    with pytest.raises(BudgetExhausted):
        cli.run("listGroups")

    assert runner.calls == []


def test_budget_consumed_mid_retry_raises_instead_of_sleeping_past_it():
    runner = FakeRunner()
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(0, "never reached")

    clock = FakeClock(now=0.0)
    sleeper = FakeSleeper()

    def advancing_sleep(seconds: float) -> None:
        sleeper(seconds)
        clock.advance(seconds)

    # Deadline allows the first attempt but not the backoff that follows it.
    cli = make_cli(runner, deadline=0.5, clock=clock, sleep=advancing_sleep)

    with pytest.raises(BudgetExhausted):
        cli.run("listGroups")

    assert len(runner.calls) == 1
    assert sleeper.slept == []


def test_timeout_handed_to_runner_never_exceeds_remaining_budget():
    runner = FakeRunner().queue(0, "ok")
    clock = FakeClock(now=0.0)
    cli = make_cli(runner, deadline=30.0, clock=clock)

    cli.run("listGroups")

    assert runner.timeouts == [30.0]


def test_backoff_index_is_per_exit_code_not_shared():
    """
    A SERVER_OR_IO failure followed by a RATE_LIMIT failure must give the
    rate limit its own first backoff (60s), not whatever index the server
    error left behind in a shared counter. A shared counter would hand this
    rate-limit failure BACKOFF_RATE_LIMIT[1] (120s) instead of [0] (60s).
    """
    runner = FakeRunner()
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(int(ExitCode.RATE_LIMIT), "", "slow down")
    runner.queue(0, "ok")

    sleeper = FakeSleeper()
    cli = make_cli(runner, sleep=sleeper)

    assert cli.run("listGroups") == "ok"
    assert sleeper.slept == [BACKOFF_SERVER_OR_IO[0], BACKOFF_RATE_LIMIT[0]]


def test_alternating_exit_codes_each_get_their_own_full_backoff():
    """
    Two SERVER_OR_IO failures followed by a RATE_LIMIT failure must still
    attempt the rate-limit backoff rather than raise: with unlimited budget,
    nothing should terminate a retryable call except its own backoff running
    out. A shared attempt counter would see attempt index 2 on the third
    call, find it >= len(BACKOFF_RATE_LIMIT) (2), and raise immediately
    without ever sleeping the rate-limit backoff.
    """
    runner = FakeRunner()
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(int(ExitCode.RATE_LIMIT), "", "slow down")
    runner.queue(0, "recovered")

    sleeper = FakeSleeper()
    cli = make_cli(runner, sleep=sleeper)

    assert cli.run("listGroups") == "recovered"
    assert len(runner.calls) == 4
    assert sleeper.slept == [
        BACKOFF_SERVER_OR_IO[0], BACKOFF_SERVER_OR_IO[1], BACKOFF_RATE_LIMIT[0],
    ]


import json

from autogroupchat.makers.automakesignal import Delete

# Shape from UpdateGroupCommand.java:206-216. groupId is present ONLY when the
# call created a new group, which is exactly the idempotency signal we need.
CREATED_GROUP_JSON = json.dumps({
    "groupId": "dGVzdC1ncm91cC1pZA==",
    "timestamp": 1756600000000,
    "results": [],
})

UPDATED_GROUP_JSON = json.dumps({"timestamp": 1756600000001, "results": []})

# Shape from ListGroupsCommand.java:149-174. Note the key is "id", not
# "groupId", and "admins" is @Deprecated in favour of members[].isAdmin.
LIST_GROUPS_JSON = json.dumps([
    {
        "id": "dGVzdC1ncm91cC1pZA==",
        "name": "Test Group",
        "description": "autogroupchat:1:2026-01-01 - marker",
        "isMember": True,
        "isBlocked": False,
        "messageExpirationTime": 0,
        "members": [
            {"number": "+15551234567", "uuid": "uuid-self", "isAdmin": True},
            {"number": "+15559999999", "uuid": "uuid-other", "isAdmin": False},
        ],
        "pendingMembers": [{"number": "+15558888888", "uuid": "uuid-pending"}],
        "requestingMembers": [],
        "admins": [{"number": "+15551234567", "uuid": "uuid-self"}],
        "banned": [],
        "permissionAddMember": "EVERY_MEMBER",
        "permissionEditDetails": "EVERY_MEMBER",
        "permissionSendMessage": "EVERY_MEMBER",
        "groupInviteLink": None,
    },
])


def test_create_group_returns_the_new_group_id():
    runner = FakeRunner().queue(0, CREATED_GROUP_JSON)
    cli = make_cli(runner)

    group_id = cli.create_group("Test Group", "a description")

    assert group_id == "dGVzdC1ncm91cC1pZA=="


def test_create_group_omits_group_id_flag_so_signal_cli_creates_one():
    runner = FakeRunner().queue(0, CREATED_GROUP_JSON)
    cli = make_cli(runner)

    cli.create_group("Test Group", "a description")

    assert "-g" not in runner.last
    assert runner.last[-4:] == ["-n", "Test Group", "-d", "a description"]


def test_create_group_passes_avatar_after_the_subcommand():
    """updateGroup's -a is --avatar; the global -a is --account. Both appear."""
    runner = FakeRunner().queue(0, CREATED_GROUP_JSON)
    cli = make_cli(runner)

    cli.create_group("Test Group", "desc", avatar="/tmp/pic.png")

    argv = runner.last
    after = argv[argv.index("updateGroup"):]

    assert after[after.index("-a") + 1] == "/tmp/pic.png"


def test_create_group_is_never_retried():
    runner = FakeRunner()
    runner.queue(int(ExitCode.SERVER_OR_IO), "", "server sad")
    runner.queue(0, CREATED_GROUP_JSON)

    cli = make_cli(runner, sleep=FakeSleeper())

    with pytest.raises(SignalCliError):
        cli.create_group("Test Group", "desc")

    assert len(runner.calls) == 1


def test_create_group_raises_when_no_group_id_came_back():
    """
    A response without groupId means no group was created. Returning None here
    would let the caller go on to add members to nothing.
    """
    runner = FakeRunner().queue(0, UPDATED_GROUP_JSON)
    cli = make_cli(runner)

    with pytest.raises(SignalCliError):
        cli.create_group("Test Group", "desc")


def test_update_group_sends_all_members_in_one_call():
    runner = FakeRunner().queue(0, UPDATED_GROUP_JSON)
    cli = make_cli(runner)

    cli.update_group("gid", members=["+15551112222", "+15553334444"])

    argv = runner.last
    assert argv[argv.index("-g") + 1] == "gid"
    assert argv[argv.index("-m") + 1:] == ["+15551112222", "+15553334444"]


def test_update_group_promotes_admins():
    runner = FakeRunner().queue(0, UPDATED_GROUP_JSON)
    cli = make_cli(runner)

    cli.update_group("gid", admins=["+15551112222"])

    argv = runner.last
    assert argv[argv.index("--admin") + 1] == "+15551112222"


def test_update_group_with_nothing_to_change_makes_no_call():
    """Guards against an argv of just `updateGroup -g ID`, which is a no-op
    round trip that still costs a network call and a rate-limit budget."""
    runner = FakeRunner()
    cli = make_cli(runner)

    cli.update_group("gid")

    assert runner.calls == []


def test_list_groups_parses_the_id_key_not_group_id():
    runner = FakeRunner().queue(0, LIST_GROUPS_JSON)
    cli = make_cli(runner)

    groups = cli.list_groups()

    assert len(groups) == 1
    assert groups[0]["id"] == "dGVzdC1ncm91cC1pZA=="
    assert "-d" in runner.last


def test_list_groups_on_empty_output_returns_empty_list():
    """A brand-new account has no groups and signal-cli prints nothing."""
    runner = FakeRunner().queue(0, "")
    cli = make_cli(runner)

    assert cli.list_groups() == []


def test_send_group_posts_the_message():
    runner = FakeRunner().queue(0, "{}")
    cli = make_cli(runner)

    cli.send_group("gid", "hello")

    argv = runner.last
    assert argv[argv.index("send"):] == ["send", "-g", "gid", "-m", "hello"]


def test_quit_group_deletes_local_data_by_default():
    runner = FakeRunner().queue(0, "{}")
    cli = make_cli(runner)

    cli.quit_group("gid")

    assert "--delete" in runner.last


def test_quit_group_can_keep_local_data():
    runner = FakeRunner().queue(0, "{}")
    cli = make_cli(runner)

    cli.quit_group("gid", delete=Delete.KEEP_LOCAL_DATA)

    assert "--delete" not in runner.last


def test_quit_group_designates_successor_admins():
    """signal-cli: --admin is 'required if you're currently the only admin'."""
    runner = FakeRunner().queue(0, "{}")
    cli = make_cli(runner)

    cli.quit_group("gid", new_admins=["+15559999999"])

    argv = runner.last
    assert argv[argv.index("--admin") + 1] == "+15559999999"


def test_run_json_raises_a_signal_cli_error_on_unparseable_output():
    runner = FakeRunner().queue(0, "not json at all")
    cli = make_cli(runner)

    with pytest.raises(SignalCliError):
        cli.run_json("listGroups")


def test_version_parses_the_reported_version():
    runner = FakeRunner().queue(0, "signal-cli 0.14.7\n")
    cli = make_cli(runner)

    assert cli.version() == (0, 14, 7)
