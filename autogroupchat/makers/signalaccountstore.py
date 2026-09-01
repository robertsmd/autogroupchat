"""
Account-store backends for the Signal maker.

A signal-cli credential is not a token but a mutable data directory holding the
account's identity key, prekeys, per-recipient ratchet state and cached group
state, backed by a WAL-mode SQLite database. This module is the only place that
knows the store has a location and a lifecycle.

Design: docs/superpowers/specs/2026-08-31-signal-maker-design.md sections 4, 12
"""

import logging
import os
import stat
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

global logger
logger = logging.getLogger(__name__)

# The data dir holds the account's identity key, prekeys and ratchet state.
# Owner-only: no group or world bits.
_SECURE_DATA_DIR_MODE = 0o700


class AccountStoreError(Exception):
    """The account store could not be acquired, or could not be persisted."""


class PreconditionFailed(AccountStoreError):
    """
    A conditional write was rejected because the remote object changed.

    Raised by fakes in tests and mapped from the storage client's own
    precondition error in GcsStore.
    """


class OnMissing(Enum):
    """What to do when no store exists yet at the configured location."""

    # Normal operation: absence means the account was never linked.
    ERROR = "error"
    # Bootstrap only (`link`, `register`): absence is expected.
    EMPTY = "empty"


class AccountStore(ABC):
    """
    Makes a signal-cli data directory available locally, then persists changes.

    Contract: `acquire` returns a path a signal-cli process may read and write.
    `release` must be called exactly once per successful `acquire`, from a
    `finally` block, and is responsible for persisting whatever changed.
    """

    @abstractmethod
    def acquire(self, on_missing: OnMissing = OnMissing.ERROR) -> str:
        """Return a local path holding the account's signal-cli data dir."""

    @abstractmethod
    def release(self, error: BaseException | None = None) -> None:
        """
        Persist the store and drop any exclusivity held over it.

        `error` is the exception that ended the session, or None. Backends still
        persist on error: work already sent must not be rolled back.
        """


class LocalStore(AccountStore):
    """
    A data directory that already lives on a persistent filesystem.

    Nothing is copied and nothing is locked, so this backend is only safe where
    a single operator runs one command at a time.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Resolve `config["data_dir"]` to an absolute, user-expanded path."""
        data_dir = config.get("data_dir")
        if not data_dir:
            raise AccountStoreError(
                "account_store.data_dir is required for type 'local'")

        self.data_dir = os.path.expanduser(str(data_dir))

    def acquire(self, on_missing: OnMissing = OnMissing.ERROR) -> str:
        """Verify the data dir exists, creating it only when bootstrapping."""
        if os.path.isdir(self.data_dir):
            self._warn_if_loose_permissions()
            return self.data_dir

        if on_missing is OnMissing.ERROR:
            raise AccountStoreError(
                f"no signal-cli data dir at {self.data_dir}; "
                f"run the `link` command first")

        # `mode=` on makedirs is masked by the umask, so a directory born from
        # a permissive umask can still come out group- or world-readable.
        # exist_ok=True also means makedirs never re-chmods a dir that
        # already exists, so chmod explicitly to be sure.
        os.makedirs(self.data_dir, mode=_SECURE_DATA_DIR_MODE, exist_ok=True)
        try:
            os.chmod(self.data_dir, _SECURE_DATA_DIR_MODE)
        except OSError:
            logger.warning(
                "could not set owner-only permissions on new data dir %s",
                self.data_dir)

        return self.data_dir

    def _warn_if_loose_permissions(self) -> None:
        """
        Log a warning if an existing data dir is group- or world-readable.

        Never chmods it: an operator's existing directory is theirs, this
        only tells them the identity key and ratchet state inside it are
        exposed to other local users.
        """
        mode = stat.S_IMODE(os.stat(self.data_dir).st_mode)
        if mode & ~_SECURE_DATA_DIR_MODE:
            logger.warning(
                "signal-cli data dir %s has mode %o, looser than the "
                "recommended %o; it holds the account's identity key and "
                "ratchet state",
                self.data_dir, mode, _SECURE_DATA_DIR_MODE)

    def release(self, error: BaseException | None = None) -> None:
        """No-op: the store never left the persistent filesystem."""
        return None


def build_store(config: dict[str, Any], **kwargs: Any) -> AccountStore:
    """
    Construct the backend named by `config["type"]`.

    Extra keyword arguments are forwarded to the backend, which is how tests
    inject a fake storage client.
    """
    store_type = config.get("type")

    if store_type == "local":
        return LocalStore(config)

    if store_type == "gcs":
        return GcsStore(config, **kwargs)

    raise AccountStoreError(
        f"unknown account_store.type: {store_type!r} (expected 'local' or 'gcs')")
