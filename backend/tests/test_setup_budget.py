"""Unit tests for the shared integration-setup wall-clock budget (#425).

These run in the unit suite -- no HAPI, no Docker. The point of extracting the
budget arithmetic out of conftest is precisely that it becomes testable without
standing up infrastructure.
"""

import pytest

from tests.integration._setup_budget import SetupBudget, SetupBudgetExceeded


class _FakeClock:
    """Hand-cranked monotonic clock so budget maths is tested, not slept through."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_remaining_starts_at_the_full_budget():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    assert budget.remaining() == 600


def test_remaining_shrinks_as_the_clock_advances():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(250)
    assert budget.remaining() == 350


def test_remaining_floors_at_zero_rather_than_going_negative():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(900)
    assert budget.remaining() == 0


def test_allot_gives_the_gates_own_cap_when_budget_is_plentiful():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    assert budget.allot("encounter-gate", cap=300) == 300


def test_allot_is_capped_by_what_remains_not_by_the_gates_own_cap():
    """The whole point: a later gate cannot claim a fresh 300s of its own."""
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(450)
    assert budget.allot("condition-gate", cap=300) == 150


def test_allot_raises_once_the_shared_budget_is_gone():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(600)
    with pytest.raises(SetupBudgetExceeded):
        budget.allot("condition-gate", cap=300)


def test_the_exhaustion_error_names_the_gate_and_the_budget():
    """A job-kill tells you nothing; this error has to say what ran out where."""
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(620)
    with pytest.raises(SetupBudgetExceeded) as exc:
        budget.allot("observation-gate", cap=300)
    msg = str(exc.value)
    assert "observation-gate" in msg
    assert "600" in msg
    assert "620" in msg


def test_spent_reports_elapsed_wall_clock():
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    clock.advance(75)
    assert budget.spent() == 75


def test_sum_of_allotments_can_never_exceed_the_total():
    """Property that makes a job kill impossible: gates are additive, not parallel."""
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=600, clock=clock)
    granted = 0.0
    for name, cap in (("cdr", 600), ("encounter", 300), ("observation", 300), ("condition", 300)):
        try:
            allot = budget.allot(name, cap=cap)
        except SetupBudgetExceeded:
            break
        granted += allot
        clock.advance(allot)  # worst case: every gate burns its whole allotment
    assert granted <= 600


# ---------------------------------------------------------------------------
# Gate timing records — these feed the end-of-run summary, which is the only
# form of this output visible in CI (pytest captures stdout; CI has no -s).
# ---------------------------------------------------------------------------


def test_record_gate_accumulates_in_order():
    from tests.integration._setup_budget import GATE_TIMINGS, record_gate, reset_gate_timings

    reset_gate_timings()
    record_gate("cdr-full-reindex", 281.0)
    record_gate("encounter-gate", 0.0)
    assert GATE_TIMINGS == [("cdr-full-reindex", 281.0), ("encounter-gate", 0.0)]
    reset_gate_timings()


def test_exhausting_the_budget_records_the_gate_that_could_not_run():
    """The summary must show WHERE setup ran out, not just that it did."""
    from tests.integration._setup_budget import GATE_TIMINGS, reset_gate_timings

    reset_gate_timings()
    clock = _FakeClock()
    budget = SetupBudget(total_seconds=100, clock=clock)
    clock.advance(150)
    with pytest.raises(SetupBudgetExceeded):
        budget.allot("condition-gate", cap=300)
    assert GATE_TIMINGS == [("condition-gate (BUDGET EXHAUSTED)", 0.0)]
    reset_gate_timings()
