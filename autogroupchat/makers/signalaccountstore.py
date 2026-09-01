"""
Account-store backends for the Signal maker.

A signal-cli credential is not a token but a mutable data directory holding the
account's identity key, prekeys, per-recipient ratchet state and cached group
state, backed by a WAL-mode SQLite database. This module is the only place that
knows the store has a location and a lifecycle.

Design: docs/superpowers/specs/2026-08-31-signal-maker-design.md sections 4, 12
"""

import json
import logging
import os
import shutil
import stat
import tarfile
import tempfile
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable

global logger
logger = logging.getLogger(__name__)

# The data dir holds the account's identity key, prekeys and ratchet state.
# Owner-only: no group or world bits.
_SECURE_DATA_DIR_MODE = 0o700


def _secure_directory(path: str) -> None:
    """
    Chmod `path` to owner-only, logging if that tightens it.

    Shared by LocalStore (bootstrapping a brand-new data dir) and GcsStore
    (the extracted scratch work dir), since both land credential material
    -- the account's identity key and ratchet state -- on disk and neither
    may leave it group- or world-readable.
    """
    try:
        before = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        before = None

    try:
        os.chmod(path, _SECURE_DATA_DIR_MODE)
    except OSError:
        logger.warning(
            "could not set owner-only permissions on directory %s", path)
        return

    if before is not None and before != _SECURE_DATA_DIR_MODE:
        logger.warning(
            "tightened directory %s permissions from %o to %o before "
            "writing credential material",
            path, before, _SECURE_DATA_DIR_MODE)


def _warn_if_loose_permissions(path: str) -> None:
    """
    Log a warning if `path` is group- or world-readable.

    Never chmods it: an operator's (or a warm container's) existing
    directory is left alone, this only reports that the credential
    material inside it is exposed to other local users.
    """
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & ~_SECURE_DATA_DIR_MODE:
        logger.warning(
            "directory %s has mode %o, looser than the recommended %o; it "
            "holds credential material",
            path, mode, _SECURE_DATA_DIR_MODE)


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
            if on_missing is OnMissing.EMPTY:
                # `link` is about to write a brand-new identity key here, so
                # permissions must be tightened before that happens,
                # regardless of whose directory this already was.
                _secure_directory(self.data_dir)
            else:
                # The store already exists and whatever it holds was
                # written under its current permissions already; report,
                # don't rewrite.
                _warn_if_loose_permissions(self.data_dir)

            return self.data_dir

        if on_missing is OnMissing.ERROR:
            raise AccountStoreError(
                f"no signal-cli data dir at {self.data_dir}; "
                f"run the `link` command first")

        # `mode=` on makedirs is masked by the umask, so a directory born from
        # a permissive umask can still come out group- or world-readable.
        os.makedirs(self.data_dir, mode=_SECURE_DATA_DIR_MODE, exist_ok=True)
        _secure_directory(self.data_dir)

        return self.data_dir

    def release(self, error: BaseException | None = None) -> None:
        """No-op: the store never left the persistent filesystem."""
        return None


DEFAULT_WORK_DIR = "/tmp/signal-cli"
DEFAULT_LOCK_TTL_SECONDS = 900

# GCS spells "this object must not already exist" as generation 0.
GENERATION_ABSENT = 0


class GcsStore(AccountStore):
    """
    Snapshots the signal-cli data dir to and from a GCS object.

    Required because a Cloud Run instance keeps nothing between invocations, and
    because the store is a WAL-mode SQLite database that cannot be hosted on
    GCSFuse: WAL needs shared-memory locking GCSFuse does not provide, and the
    failure mode is a corrupted identity store. So the store moves as an opaque
    tarball and only ever runs on a real local filesystem.

    Exclusivity comes from a lock object plus an if_generation_match write. With
    --max-instances=1 neither should ever fire; they exist so that a violation
    is loud instead of silently destroying the credential.
    """

    def __init__(self,
                 config: dict[str, Any],
                 *,
                 client: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        """
        Validate `config` and resolve the bucket, object, lock and work dir.

        `client` is a `google.cloud.storage.Client` (or a fake); when None,
        the real client is imported and constructed lazily on first use, so
        that importing this module never requires google-cloud-storage.
        `clock` lets tests control lock-expiry checks deterministically.
        """
        for required in ("bucket", "object"):
            if not config.get(required):
                raise AccountStoreError(
                    f"account_store.{required} is required for type 'gcs'")

        self.bucket_name = str(config["bucket"])
        self.object_name = str(config["object"])
        self.lock_name = str(
            config.get("lock_object") or f"{self.object_name}.lock")
        self.lock_ttl = int(
            config.get("lock_ttl_seconds", DEFAULT_LOCK_TTL_SECONDS))
        self.work_dir = os.path.expanduser(
            str(config.get("work_dir", DEFAULT_WORK_DIR)))

        self._clock = clock
        self._client = client
        self._generation: int | None = None
        self._locked = False

    def _bucket(self) -> Any:
        """Resolve the storage client lazily so local users need no GCP deps."""
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()

        return self._client.bucket(self.bucket_name)

    def _precondition_types(self) -> tuple[type[BaseException], ...]:
        """Our own error plus the storage client's, when it is installed."""
        types: list[type[BaseException]] = [PreconditionFailed]
        try:
            from google.api_core.exceptions import PreconditionFailed as GoogleFailed

            types.append(GoogleFailed)
        except ImportError:
            pass

        return tuple(types)

    def _take_lock(self) -> None:
        """
        Create the lock object, breaking it only if it has expired.

        An expired lock means a previous run died holding it. Breaking it is
        safe because --concurrency=1 and --max-instances=1 make a genuinely
        concurrent holder impossible in the supported deployment.
        """
        blob = self._bucket().blob(self.lock_name)
        now = self._clock()

        if blob.exists():
            expires_at = self._lock_expiry(blob)
            if expires_at > now:
                raise AccountStoreError(
                    f"account store lock {self.lock_name} is held until "
                    f"{expires_at}; another invocation is running")

            logger.warning(
                "breaking expired account store lock %s (expired at %s, now %s)",
                self.lock_name, expires_at, now)
            blob.delete()

        payload = json.dumps({"expires_at": now + self.lock_ttl})
        try:
            blob.upload_from_string(
                payload, if_generation_match=GENERATION_ABSENT)
        except self._precondition_types() as e:
            raise AccountStoreError(
                f"lost the race for account store lock {self.lock_name}") from e

        self._locked = True

    def _lock_expiry(self, blob: Any) -> float:
        """Read a lock's expiry, treating an unreadable lock as expired."""
        try:
            return float(json.loads(blob.download_as_bytes())["expires_at"])
        except Exception:
            logger.warning(
                "lock %s is unreadable; treating it as expired", self.lock_name)
            return 0.0

    def _free_lock(self) -> None:
        """Delete the lock object, logging rather than raising on failure."""
        if not self._locked:
            return

        try:
            self._bucket().blob(self.lock_name).delete()
        except Exception as e:
            # A leaked lock self-heals after lock_ttl_seconds; do not mask the
            # original failure by raising here.
            logger.error("could not release lock %s: %s", self.lock_name, e)

        self._locked = False

    def acquire(self, on_missing: OnMissing = OnMissing.ERROR) -> str:
        """
        Take the lock, then materialise the store into a clean work dir.

        The work dir is wiped first: a warm Cloud Run instance still holds the
        previous invocation's /tmp, and operating on that stale copy while GCS
        holds the true one would silently diverge the account.
        """
        self._take_lock()

        try:
            if os.path.isdir(self.work_dir):
                shutil.rmtree(self.work_dir)

            # `mode=` on makedirs is masked by the umask, so this is followed
            # by an explicit chmod rather than trusted on its own.
            os.makedirs(self.work_dir, mode=_SECURE_DATA_DIR_MODE, exist_ok=True)
            _secure_directory(self.work_dir)

            blob = self._bucket().blob(self.object_name)
            if not blob.exists():
                if on_missing is OnMissing.ERROR:
                    raise AccountStoreError(
                        f"no account store at gs://{self.bucket_name}/"
                        f"{self.object_name}; run the `link` command first")

                self._generation = GENERATION_ABSENT
                return self.work_dir

            self._generation = blob.generation
            self._extract(blob)

            return self.work_dir
        except Exception:
            self._free_lock()
            raise

    def _extract(self, blob: Any) -> None:
        """Download the tarball and unpack it into the work dir."""
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
            blob.download_to_filename(tmp.name)
            with tarfile.open(tmp.name, mode="r:gz") as tar:
                # filter="data" rejects device files, absolute paths and
                # symlinks escaping work_dir; explicit rather than relying
                # on the interpreter's default so behaviour is identical
                # across 3.12-3.14.
                tar.extractall(self.work_dir, filter="data")

        # The tarball's own permission bits may be looser than what a
        # credential store on shared /tmp requires; re-secure after
        # extraction, before any caller can read the identity key.
        _secure_directory(self.work_dir)

    def release(self, error: BaseException | None = None) -> None:
        """
        Upload the store, then free the lock.

        Uploads even when `error` is set: anything already sent has advanced
        recipients' ratchet state, so rolling the store back would desynchronise
        it. On a generation mismatch the local copy is deliberately left in
        place for manual recovery rather than clobbering another writer.
        """
        try:
            self._upload()
        finally:
            self._free_lock()

    def _upload(self) -> None:
        """Tar the work dir and write it under an if_generation_match guard."""
        if not os.path.isdir(self.work_dir):
            logger.error(
                "work dir %s is gone; nothing to persist", self.work_dir)
            return

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            archive = tmp.name

        try:
            with tarfile.open(archive, mode="w:gz") as tar:
                tar.add(self.work_dir, arcname=".")

            blob = self._bucket().blob(self.object_name)
            try:
                blob.upload_from_filename(
                    archive, if_generation_match=self._generation)
            except self._precondition_types() as e:
                raise AccountStoreError(
                    f"gs://{self.bucket_name}/{self.object_name} changed under "
                    f"us (expected generation {self._generation}); refusing to "
                    f"overwrite. The local copy is preserved at {self.work_dir}"
                ) from e
        finally:
            os.unlink(archive)


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
