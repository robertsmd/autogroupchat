"""Shared test doubles. No test in this suite executes signal-cli or touches GCS."""

from typing import Any

from autogroupchat.makers.signalaccountstore import PreconditionFailed


class FakeRunner:
    """
    Stands in for the subprocess runner SignalCli calls.

    Records every argv it was handed and returns queued results in order. An
    empty queue returns success with empty stdout, which keeps tests that only
    care about argv short.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._results: list[tuple[int, str, str]] = []

    def queue(self, exit_code: int, stdout: str = "", stderr: str = "") -> "FakeRunner":
        """Append one result to be returned by a later call. Chainable."""
        self._results.append((exit_code, stdout, stderr))
        return self

    def __call__(self, argv: list[str], timeout: float | None) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)

        if not self._results:
            return (0, "", "")

        return self._results.pop(0)

    @property
    def last(self) -> list[str]:
        """argv of the most recent call."""
        return self.calls[-1]


class FakeSleeper:
    """Records sleep durations instead of sleeping, so retry tests run instantly."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class FakeClock:
    """Monotonic clock a test can advance by hand."""

    def __init__(self, now: float = 0.0) -> None:
        """Start the fake clock at `now` seconds."""
        self.now = now

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward by `seconds`."""
        self.now += seconds


class FakeGcsBlob:
    """
    One object in FakeGcsClient, enforcing if_generation_match preconditions.

    Mirrors a real distinction in google-cloud-storage: `Bucket.blob()`
    makes no network call and returns a Blob whose metadata -- including
    `.generation` -- is unpopulated (None) until a round trip fills it in.
    Only `Bucket.get_blob()` does that round trip. A blob constructed with
    `populated=True` snapshots its generation at construction time, the
    same way a real `get_blob()` response is a snapshot, not a live view.
    """

    def __init__(self, client: "FakeGcsClient", name: str, *,
                 populated: bool = False) -> None:
        """
        Reference the object named `name` in `client`'s in-memory store.

        `populated` distinguishes a `blob()` handle (metadata not fetched)
        from a `get_blob()` result (metadata fetched once, at construction).
        """
        self._client = client
        self.name = name
        self._populated = populated
        self._generation_snapshot = (
            client.generations.get(name) if populated else None)

    @property
    def generation(self) -> int | None:
        """
        Generation as of the round trip that populated this blob.

        None for a `blob()` handle, exactly as it is against the real
        client: no request was made to learn it.
        """
        if not self._populated:
            return None

        return self._generation_snapshot

    def exists(self) -> bool:
        """Whether this object currently has content."""
        return self.name in self._client.objects

    def reload(self) -> None:
        """Present for API parity; the fake is always current."""
        return None

    def _check(self, if_generation_match: int | None) -> None:
        """Raise PreconditionFailed if the object's generation does not match."""
        if if_generation_match is None:
            return

        current = self._client.generations.get(self.name, 0)
        if current != if_generation_match:
            raise PreconditionFailed(
                f"{self.name}: generation {current} != {if_generation_match}")

    def _write(self, data: bytes, if_generation_match: int | None) -> None:
        """Check the precondition, then store `data` and bump the generation."""
        self._check(if_generation_match)
        self._client.objects[self.name] = data
        self._client.generations[self.name] = (
            self._client.generations.get(self.name, 0) + 1)
        self._client.writes.append(
            (self.name, if_generation_match))

    def upload_from_string(self,
                            data: str | bytes,
                            if_generation_match: int | None = None,
                            **kwargs: Any) -> None:
        """Write string or bytes `data` under the given precondition."""
        payload = data.encode() if isinstance(data, str) else data
        self._write(payload, if_generation_match)

    def upload_from_filename(self,
                              path: str,
                              if_generation_match: int | None = None,
                              **kwargs: Any) -> None:
        """Write the contents of the file at `path` under the given precondition."""
        with open(path, "rb") as f:
            self._write(f.read(), if_generation_match)

    def download_as_bytes(self) -> bytes:
        """Return this object's content, raising if it does not exist."""
        if not self.exists():
            raise FileNotFoundError(self.name)

        return self._client.objects[self.name]

    def download_to_filename(self, path: str) -> None:
        """Write this object's content to the file at `path`."""
        with open(path, "wb") as f:
            f.write(self.download_as_bytes())

    def delete(self, **kwargs: Any) -> None:
        """
        Remove this object and its generation record.

        Real GCS accepts if_generation_match=0 ("must not exist") for an
        object that was just deleted, so a stale generation left behind here
        would make a subsequent create-lock call fail the precondition it
        should pass.
        """
        self._client.objects.pop(self.name, None)
        self._client.generations.pop(self.name, None)
        self._client.deletes.append(self.name)


class FakeGcsBucket:
    """Stands in for google.cloud.storage.Bucket, handing out FakeGcsBlob objects."""

    def __init__(self, client: "FakeGcsClient") -> None:
        """Bind this bucket to the `client` whose objects it exposes."""
        self._client = client

    def blob(self, name: str) -> FakeGcsBlob:
        """
        Return an unpopulated handle to the object named `name`.

        No network call, matching the real client: `.generation` on the
        result is None until something (`get_blob()`, `reload()`) fetches
        metadata.
        """
        return FakeGcsBlob(self._client, name)

    def get_blob(self, name: str) -> FakeGcsBlob | None:
        """
        One round trip: a populated blob, or None if `name` does not exist.

        Mirrors `google.cloud.storage.Bucket.get_blob`, which is the only
        supported way to learn an object's generation without a separate
        `reload()` call.
        """
        if name not in self._client.objects:
            return None

        return FakeGcsBlob(self._client, name, populated=True)


class FakeGcsClient:
    """
    In-memory stand-in for google.cloud.storage.Client.

    Records every conditional write so tests can assert the preconditions that
    protect the credential store, not merely that a write happened.
    """

    def __init__(self) -> None:
        """Start with no objects, no generations and empty write/delete logs."""
        self.objects: dict[str, bytes] = {}
        # Generation 0 means "absent", matching GCS's if_generation_match=0.
        self.generations: dict[str, int] = {}
        self.writes: list[tuple[str, int | None]] = []
        self.deletes: list[str] = []

    def bucket(self, name: str) -> FakeGcsBucket:
        """Return a bucket handle. All bucket names share the same object store."""
        return FakeGcsBucket(self)
