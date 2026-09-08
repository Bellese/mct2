"""One wall-clock budget shared by every blocking gate in integration setup (#425).

Why this exists
---------------
Integration setup blocks on several HAPI indexing gates. Each used to carry its
own independent timeout, so their worst cases were *additive*: 600s for the CDR
full reindex plus 300s each for the Encounter, Observation and Condition
reference-param gates came to 1500s (25 min) -- larger than the Integration
job's own `timeout-minutes` of 20. A sufficiently slow run therefore could not
fail cleanly: GitHub killed the job before any fixture reached its deadline, and
the only evidence left was `The job has exceeded the maximum execution time`.
That is the same opaque signal that sent #418 looking in the wrong place.

A shared budget makes the gates additive against one ceiling instead of four,
so setup always fails as a *fixture error naming the gate* rather than as a job
kill. The arithmetic lives here, apart from conftest, so it can be tested
without Docker or HAPI.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class SetupBudgetExceeded(RuntimeError):
    """Raised when a setup gate is reached with no shared budget left."""


# Per-gate timings, reported by conftest's pytest_terminal_summary hook.
#
# A plain print() here is invisible in CI: pytest captures stdout and the workflow
# does not pass -s, so the breakdown would only surface on a local `-s` run -- i.e.
# never, on the runs where diagnosing a stalled gate actually matters. The terminal
# summary is written whether or not capture is on.
GATE_TIMINGS: list[tuple[str, float]] = []


class SetupBudget:
    """A single wall-clock allowance drawn down by successive setup gates."""

    def __init__(self, total_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._total = float(total_seconds)
        self._clock = clock
        self._start = clock()

    def spent(self) -> float:
        """Wall-clock seconds since the budget was opened."""
        return self._clock() - self._start

    def remaining(self) -> float:
        """Seconds left, floored at zero so callers never see a negative deadline."""
        return max(0.0, self._total - self.spent())

    def allot(self, name: str, cap: float) -> float:
        """Seconds `name` may wait: its own cap, or what's left, whichever is smaller.

        Raises SetupBudgetExceeded if the budget is already gone -- failing here,
        with the gate named, is the entire point. A gate that silently proceeded
        on a zero-second deadline would hand the suite an unindexed server.
        """
        remaining = self.remaining()
        if remaining <= 0:
            record_gate(f"{name} (BUDGET EXHAUSTED)", 0.0)
            raise SetupBudgetExceeded(
                f"integration setup budget of {self._total:.0f}s exhausted before gate "
                f"'{name}' could run ({self.spent():.0f}s spent). Setup is bounded so it "
                f"fails here, diagnosably, instead of being killed by the job's "
                f"timeout-minutes with no indication of which gate stalled. See #425."
            )
        return min(float(cap), remaining)


def record_gate(name: str, seconds: float) -> None:
    """Record how long a setup gate took, for the end-of-run summary."""
    GATE_TIMINGS.append((name, seconds))


def reset_gate_timings() -> None:
    """Clear recorded timings (test-only; the list is module-level state)."""
    GATE_TIMINGS.clear()
