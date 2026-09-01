"""Tests for account-store backends (spec sections 4, 5, 12)."""

import os
import stat

import pytest

from autogroupchat.makers.signalaccountstore import (
    AccountStoreError,
    LocalStore,
    OnMissing,
    build_store,
)


def test_local_store_returns_the_configured_data_dir(tmp_path):
    store = LocalStore({"type": "local", "data_dir": str(tmp_path)})

    assert store.acquire() == str(tmp_path)


def test_local_store_expands_a_user_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local" / "share" / "signal-cli").mkdir(parents=True)

    store = LocalStore({"type": "local", "data_dir": "~/.local/share/signal-cli"})

    assert store.acquire() == str(tmp_path / ".local" / "share" / "signal-cli")


def test_local_store_release_is_a_no_op(tmp_path):
    """Nothing moves, so release must not touch the directory."""
    store = LocalStore({"type": "local", "data_dir": str(tmp_path)})
    store.acquire()

    store.release(None)

    assert os.path.isdir(tmp_path)


def test_local_store_rejects_a_missing_data_dir(tmp_path):
    """
    A missing data dir means an unlinked account. Creating it silently would
    produce a confusing 'no account' error from signal-cli much later.
    """
    store = LocalStore({"type": "local", "data_dir": str(tmp_path / "absent")})

    with pytest.raises(AccountStoreError):
        store.acquire()


def test_local_store_creates_a_missing_data_dir_when_bootstrapping(tmp_path):
    """`link` runs before any store exists, so it must be allowed to make one."""
    target = tmp_path / "fresh"
    store = LocalStore({"type": "local", "data_dir": str(target)})

    assert store.acquire(OnMissing.EMPTY) == str(target)
    assert target.is_dir()


def test_local_store_creates_a_missing_data_dir_owner_only(tmp_path):
    """
    The data dir holds the account's identity key and ratchet state, so a
    freshly bootstrapped one must not be group- or world-readable.
    """
    target = tmp_path / "fresh"
    store = LocalStore({"type": "local", "data_dir": str(target)})

    store.acquire(OnMissing.EMPTY)

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o700


def test_local_store_warns_about_a_preexisting_loose_data_dir(tmp_path, caplog):
    """
    An operator who linked their account under a loose umask deserves to
    know, but acquire must not chmod a directory out from under them.
    """
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    store = LocalStore({"type": "local", "data_dir": str(target)})

    with caplog.at_level("WARNING"):
        store.acquire()

    assert "755" in caplog.text
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755


def test_local_store_requires_data_dir_in_config():
    with pytest.raises(AccountStoreError) as exc:
        LocalStore({"type": "local"})

    assert "data_dir" in str(exc.value)


def test_build_store_selects_the_local_backend(tmp_path):
    store = build_store({"type": "local", "data_dir": str(tmp_path)})

    assert isinstance(store, LocalStore)


def test_build_store_rejects_an_unknown_backend():
    with pytest.raises(AccountStoreError) as exc:
        build_store({"type": "carrier-pigeon"})

    assert "carrier-pigeon" in str(exc.value)
