"""Tests for account-store backends (spec sections 4, 5, 12)."""

import io
import json
import os
import shutil
import stat
import tarfile

import pytest

from autogroupchat.makers.signalaccountstore import (
    GCS_TRANSFER_TIMEOUT_SECONDS,
    AccountStoreError,
    GcsStore,
    LocalStore,
    OnMissing,
    PreconditionFailed,
    build_store,
)
from tests.conftest import FakeGcsBlob, FakeGcsClient


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


def test_local_store_tightens_a_preexisting_loose_data_dir_when_bootstrapping(tmp_path, caplog):
    """
    `link` is about to write a brand-new identity key into this directory, so
    permissions must be tightened before that happens -- regardless of
    whether the directory already existed. This is the realistic case: an
    operator's own `mkdir -p` before linking leaves it at whatever the umask
    allows.
    """
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    store = LocalStore({"type": "local", "data_dir": str(target)})

    with caplog.at_level("WARNING"):
        store.acquire(OnMissing.EMPTY)

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
    assert "755" in caplog.text
    assert "700" in caplog.text


OBJECT = "signal-cli/+15551234567.tar.gz"
LOCK = "signal-cli/+15551234567.lock"


def gcs_config(tmp_path) -> dict:
    """Minimal valid GcsStore config pointed at a scratch dir under tmp_path."""
    return {
        "type": "gcs",
        "bucket": "test-bucket",
        "object": OBJECT,
        "lock_object": LOCK,
        "lock_ttl_seconds": 900,
        "work_dir": str(tmp_path / "work"),
    }


def seed_store(client: FakeGcsClient, files: dict[str, str]) -> None:
    """Put a tar.gz of `files` at OBJECT so acquire() has something to fetch."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    client.objects[OBJECT] = buf.getvalue()
    client.generations[OBJECT] = 7


def test_acquire_extracts_the_store_into_the_work_dir(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "sqlite-bytes"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    data_dir = store.acquire()

    assert (tmp_path / "work" / "account.db").read_text() == "sqlite-bytes"
    assert data_dir == str(tmp_path / "work")


def test_acquire_secures_the_extracted_work_dir(tmp_path):
    """
    The extracted work dir holds the account's identity key and ratchet
    state. /tmp is world-traversable, so a loosely permissioned work dir
    there exposes the account to every local user on the machine.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "sqlite-bytes"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()

    mode = stat.S_IMODE(os.stat(tmp_path / "work").st_mode)
    assert mode == 0o700


def test_acquire_wipes_a_stale_work_dir_from_a_warm_instance(tmp_path):
    """
    A reused Cloud Run instance still has the previous run's /tmp. Reusing it
    would operate on a stale store while GCS holds the true one silently
    diverges the account.
    """
    stale = tmp_path / "work"
    stale.mkdir(parents=True)
    (stale / "stale.db").write_text("from a previous invocation")

    client = FakeGcsClient()
    seed_store(client, {"account.db": "fresh"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()

    assert not (stale / "stale.db").exists()
    assert (stale / "account.db").read_text() == "fresh"


def test_acquire_takes_the_lock_with_a_does_not_exist_precondition(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()

    assert (LOCK, 0) in client.writes


def test_acquire_refuses_when_a_live_lock_is_held(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    client.objects[LOCK] = json.dumps({"expires_at": 5_000.0}).encode()
    client.generations[LOCK] = 1

    store = GcsStore(gcs_config(tmp_path), client=client, clock=lambda: 4_000.0)

    with pytest.raises(AccountStoreError) as exc:
        store.acquire()

    assert "lock" in str(exc.value).lower()


def test_acquire_breaks_an_expired_lock(tmp_path):
    """A crashed run must not wedge the account permanently."""
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    client.objects[LOCK] = json.dumps({"expires_at": 5_000.0}).encode()
    client.generations[LOCK] = 1

    store = GcsStore(gcs_config(tmp_path), client=client, clock=lambda: 9_000.0)

    store.acquire()

    assert LOCK in client.deletes


def test_acquire_breaks_a_lock_with_a_malformed_payload(tmp_path):
    """
    A genuinely corrupt lock payload is a real self-healing path, distinct
    from a transport failure: there is no expiry to trust, so treating it
    as expired is the only sane option.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    client.objects[LOCK] = b"not valid json"
    client.generations[LOCK] = 1

    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()

    assert LOCK in client.deletes


def test_acquire_propagates_a_lock_read_failure_without_breaking_it(tmp_path, monkeypatch):
    """
    A network failure while reading the lock's payload is not the same as
    a malformed payload: we do not know whether the lock is live, so it
    must not be read as expired. Breaking it anyway would be the exact
    concurrency violation the lock exists to prevent.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    client.objects[LOCK] = json.dumps({"expires_at": 5_000.0}).encode()
    client.generations[LOCK] = 1

    def _boom(self) -> bytes:
        raise ConnectionError("simulated transport failure")

    monkeypatch.setattr(FakeGcsBlob, "download_as_bytes", _boom)

    store = GcsStore(gcs_config(tmp_path), client=client, clock=lambda: 9_000.0)

    with pytest.raises(ConnectionError):
        store.acquire()

    assert LOCK not in client.deletes


def test_acquire_errors_when_no_store_exists(tmp_path):
    client = FakeGcsClient()
    store = GcsStore(gcs_config(tmp_path), client=client)

    with pytest.raises(AccountStoreError):
        store.acquire()


def test_acquire_allows_a_missing_store_when_bootstrapping(tmp_path):
    client = FakeGcsClient()
    store = GcsStore(gcs_config(tmp_path), client=client)

    data_dir = store.acquire(OnMissing.EMPTY)

    assert os.path.isdir(data_dir)


def test_release_uploads_with_the_generation_it_downloaded(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()
    store.release(None)

    assert (OBJECT, 7) in client.writes


def test_release_upload_precondition_is_the_real_downloaded_generation(tmp_path):
    """
    Regression: acquire() must read the generation from a single populated
    round trip (bucket.get_blob()), not from an unpopulated blob() handle.

    Against the real client, blob() makes no network call, so its
    .generation is always None. Uploading with if_generation_match=None
    carries no precondition at all -- an unconditional overwrite -- which
    is exactly the failure mode the generation guard exists to prevent.
    FakeGcsBlob.generation being computed from client state regardless of
    how the blob was obtained is what let this pass unnoticed the first
    time; the explicit "not None" assertion below is what catches it.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()
    store.release(None)

    assert (OBJECT, 7) in client.writes
    assert (OBJECT, None) not in client.writes


def test_release_uploads_generation_zero_for_a_fresh_store(tmp_path):
    client = FakeGcsClient()
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire(OnMissing.EMPTY)
    store.release(None)

    assert (OBJECT, 0) in client.writes


def test_uploaded_tarball_round_trips(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "original"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    data_dir = store.acquire()
    (tmp_path / "work" / "account.db").write_text("mutated by signal-cli")
    store.release(None)

    second_dir = tmp_path / "second"
    store2 = GcsStore(
        {**gcs_config(tmp_path), "work_dir": str(second_dir)}, client=client)
    store2.acquire()

    assert (second_dir / "account.db").read_text() == "mutated by signal-cli"


def test_uploaded_tarball_round_trips_nested_directories(tmp_path):
    """
    A real signal-cli store has subdirectories (data/, attachments/,
    avatars/), unlike every flat seed_store() tarball above. Cover that a
    nested path survives acquire -> mutate -> release -> acquire, not just
    a single top-level file.
    """
    client = FakeGcsClient()
    seed_store(client, {"data/account.db": "original", "avatars/group.png": "img"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()
    (tmp_path / "work" / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "data" / "account.db").write_text("mutated by signal-cli")
    store.release(None)

    second_dir = tmp_path / "second"
    store2 = GcsStore(
        {**gcs_config(tmp_path), "work_dir": str(second_dir)}, client=client)
    store2.acquire()

    assert (second_dir / "data" / "account.db").read_text() == "mutated by signal-cli"
    assert (second_dir / "avatars" / "group.png").read_text() == "img"


def test_release_persists_even_when_the_session_failed(tmp_path):
    """
    Messages already sent advanced recipients' ratchet state. Rolling the store
    back would desynchronise it, so an exception must not skip the upload.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()
    store.release(RuntimeError("send failed halfway"))

    assert (OBJECT, 7) in client.writes


def test_release_aborts_on_generation_mismatch_and_keeps_the_local_copy(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)
    store.acquire()

    # Simulate another writer between our download and our upload.
    client.generations[OBJECT] = 99

    with pytest.raises(AccountStoreError):
        store.release(None)

    assert (tmp_path / "work" / "account.db").exists()


def test_release_frees_the_lock_even_when_the_upload_fails(tmp_path):
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)
    store.acquire()
    client.generations[OBJECT] = 99

    with pytest.raises(AccountStoreError):
        store.release(None)

    assert LOCK in client.deletes


def test_gcs_config_requires_bucket_and_object(tmp_path):
    for missing in ("bucket", "object"):
        config = gcs_config(tmp_path)
        del config[missing]

        with pytest.raises(AccountStoreError) as exc:
            GcsStore(config, client=FakeGcsClient())

        assert missing in str(exc.value)


def test_both_store_transfers_carry_an_explicit_timeout(tmp_path):
    """
    The invocation budget reserves 60 s of the platform's 540 s for moving the
    store, but the google-cloud-storage default is its own retry deadline, not
    ours. Without a timeout on each transfer that reserve is enforced nowhere,
    and a stalled download or upload runs until Cloud Run kills the container
    -- for the upload, after messages have already been sent.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)

    store.acquire()
    store.release(None)

    assert client.transfers == [
        ("download", OBJECT, GCS_TRANSFER_TIMEOUT_SECONDS),
        ("upload", OBJECT, GCS_TRANSFER_TIMEOUT_SECONDS),
    ]


def test_transfer_timeout_fits_inside_the_reserve():
    """
    Both transfers must fit in the reserve with room to spare, or the timeout
    is decoration. 2 x 25 = 50 s against a 60 s reserve.
    """
    assert 2 * GCS_TRANSFER_TIMEOUT_SECONDS < 60


def test_release_raises_when_the_work_dir_vanished(tmp_path):
    """
    A vanished work dir means the store was never persisted. Logging and
    returning normally reported success for exactly that -- and by then
    messages may already have been sent, so the recipients' ratchet state has
    advanced while the store has not. That desynchronisation is the failure
    this whole backend exists to avoid, so it must be loud.
    """
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)
    store.acquire()

    shutil.rmtree(tmp_path / "work")

    with pytest.raises(AccountStoreError) as exc:
        store.release(None)

    assert "could not be persisted" in str(exc.value)


def test_release_frees_the_lock_when_the_work_dir_vanished(tmp_path):
    """The store is unpersistable either way; wedging the account too is not."""
    client = FakeGcsClient()
    seed_store(client, {"account.db": "x"})
    store = GcsStore(gcs_config(tmp_path), client=client)
    store.acquire()

    shutil.rmtree(tmp_path / "work")

    with pytest.raises(AccountStoreError):
        store.release(None)

    assert LOCK in client.deletes

