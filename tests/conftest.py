"""Shared test doubles. No test in this suite executes signal-cli or touches GCS."""

from typing import Any


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

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
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
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
