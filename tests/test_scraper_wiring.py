"""
The scraper resolves backends by name from its own module namespace, so a
backend that is not imported there is invisible no matter how correct it is.
"""

import sys

from autogroupchat.makers.automakegroupchat import AutoMakeGroupChat


def test_signal_backend_is_resolvable_by_name():
    import autogroupchat.scrapers.autoscrapegooglesheets  # noqa: F401

    module = sys.modules["autogroupchat.scrapers.autoscrapegooglesheets"]
    resolved = getattr(module, "AutoMakeSignal", None)

    assert resolved is not None, (
        "AutoMakeSignal must be imported into autoscrapegooglesheets, because "
        "scrape_using_dict resolves group_creation_class with "
        "getattr(sys.modules[__name__], ...)")
    assert issubclass(resolved, AutoMakeGroupChat)


def test_groupme_backend_is_still_resolvable():
    """Guards against the new import displacing the existing one."""
    import autogroupchat.scrapers.autoscrapegooglesheets  # noqa: F401

    module = sys.modules["autogroupchat.scrapers.autoscrapegooglesheets"]

    assert getattr(module, "AutoMakeGroupMe", None) is not None


def load_cloud_run_entrypoint():
    """
    Import deploy/signal/main.py by path.

    It is a deployment entry point, not part of the installed package, so it
    has no importable module name of its own.
    """
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "deploy", "signal", "main.py")

    spec = importlib.util.spec_from_file_location("signal_cloud_run_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def test_handler_rearms_the_invocation_deadline(tmp_path, monkeypatch):
    """
    Cloud Run reuses a warm container across invocations, so the shared
    deadline anchor survives from one to the next. Without a reset at the
    invocation boundary, invocation 2 would compute its deadline from
    invocation 1's start -- minutes stale -- and every session in it would
    raise BudgetExhausted before running anything.
    """
    import base64
    import json

    from autogroupchat.makers.automakesignal import INVOCATION_DEADLINE

    main = load_cloud_run_entrypoint()

    config = tmp_path / "scraper.json"
    config.write_text(json.dumps({"verbose": False}))
    monkeypatch.setenv("AUTOGROUPCHAT_CONFIG", str(config))
    monkeypatch.setattr(main, "scrape_using_dict", lambda config: None)

    # Anchor as a previous invocation on the same warm container would have.
    stale = INVOCATION_DEADLINE.at(480, lambda: 1_000.0)

    main.autogroupchat_pubsub({"data": base64.b64encode(b"go")}, None)

    assert stale == 1_480.0
    assert INVOCATION_DEADLINE.at(480, lambda: 9_000.0) == 9_480.0
