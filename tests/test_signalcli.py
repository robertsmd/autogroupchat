"""Tests for the SignalCli driver (spec sections 7, 10)."""

import pytest

from autogroupchat.makers.automakesignal import SignalCli
from tests.conftest import FakeRunner

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
