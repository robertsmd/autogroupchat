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
