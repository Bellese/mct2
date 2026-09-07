"""Tests for the orchestrator service (run_job and helpers)."""

import contextlib
from unittest.mock import DEFAULT, AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.config import AuthType
from app.models.job import Job, JobStatus, MeasureResult
from app.models.mcs_config import MCSConfig
from app.services.fhir_client import BatchQueryStrategy, FailedResourceFetch, GatherResult
from app.services.orchestrator import (
    _error_measure_report,
    _extract_patient_name,
    _extract_populations,
    _get_cdr_auth_headers,
    _get_mcs_auth_headers,
    run_job,
)
from app.services.workflows import SubmissionWorkflow, TransferPhaseError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _patch_snapshot_evaluated_resources():
    """Default-patch the snapshot helper so orchestrator tests don't make real HTTP calls.
    Individual tests can layer their own patch on top to assert specific behavior."""
    with patch(
        "app.services.orchestrator.snapshot_evaluated_resources",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------


class TestExtractPopulations:
    def test_all_positive(self, mock_measure_report):
        pops = _extract_populations(mock_measure_report)
        assert pops["initial_population"] is True
        assert pops["denominator"] is True
        assert pops["numerator"] is True
        assert pops["denominator_exclusion"] is False
        assert pops["numerator_exclusion"] is False

    def test_empty_report(self):
        pops = _extract_populations({})
        assert all(v is False for v in pops.values())

    def test_zero_counts(self):
        report = {
            "group": [
                {
                    "population": [
                        {
                            "code": {"coding": [{"code": "initial-population"}]},
                            "count": 0,
                        },
                        {
                            "code": {"coding": [{"code": "denominator"}]},
                            "count": 0,
                        },
                    ]
                }
            ]
        }
        pops = _extract_populations(report)
        assert pops["initial_population"] is False
        assert pops["denominator"] is False


class TestExtractPatientName:
    def test_full_name(self):
        patient = {"name": [{"given": ["John", "Q"], "family": "Doe"}]}
        assert _extract_patient_name(patient) == "John Q Doe"

    def test_family_only(self):
        patient = {"name": [{"family": "Smith"}]}
        assert _extract_patient_name(patient) == "Smith"

    def test_given_only(self):
        patient = {"name": [{"given": ["Jane"]}]}
        assert _extract_patient_name(patient) == "Jane"

    def test_no_name(self):
        assert _extract_patient_name({}) is None
        assert _extract_patient_name({"name": []}) is None


def test_error_measure_report_sanitizes_internal_urls():
    report = _error_measure_report("p1", Exception("HTTP 400 at http://hapi-fhir-measure:8080/fhir"))

    assert report["resourceType"] == "OperationOutcome"
    assert report["subject"]["reference"] == "Patient/p1"
    diagnostics = report["issue"][0]["diagnostics"]
    assert "hapi-fhir-measure" not in diagnostics
    assert "8080" not in diagnostics


def test_error_measure_report_preserves_upstream_outcome_via_extension():
    """When upstream OO is provided, it is embedded with a FHIR Extension (not synthetic)."""
    from app.services.orchestrator import LENNY_ERROR_EXT

    upstream = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Measure not found"}],
    }
    report = _error_measure_report("p2", Exception("evaluate failed"), upstream_outcome=upstream)

    assert report["resourceType"] == "OperationOutcome"
    assert report["subject"]["reference"] == "Patient/p2"
    # Original issue preserved
    assert report["issue"][0]["diagnostics"] == "Measure not found"
    # Extension added with sanitized error string
    extensions = report.get("extension", [])
    assert any(e["url"] == LENNY_ERROR_EXT for e in extensions)


def test_error_measure_report_deep_copies_upstream_outcome():
    """Two patients with the same upstream OO must produce independent dicts (no mutation)."""
    upstream = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": "shared error"}],
    }
    report_p1 = _error_measure_report("p1", Exception("fail"), upstream_outcome=upstream)
    report_p2 = _error_measure_report("p2", Exception("fail"), upstream_outcome=upstream)

    # Mutating one report must not affect the other
    report_p1["issue"][0]["diagnostics"] = "mutated"
    assert report_p2["issue"][0]["diagnostics"] == "shared error"
    assert report_p1 is not report_p2


def test_error_measure_report_falls_back_to_synthetic_without_upstream():
    """Without upstream OO, a synthetic OperationOutcome is produced."""
    report = _error_measure_report("p3", Exception("connection refused"))

    assert report["resourceType"] == "OperationOutcome"
    assert "extension" not in report
    assert report["issue"][0]["code"] == "processing"


# ---------------------------------------------------------------------------
# Integration tests for run_job
# ---------------------------------------------------------------------------


async def _setup_job(session: AsyncSession) -> int:
    """Insert a queued job and return its ID."""
    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job.id


def _make_session_factory_patch(session_factory):
    """Create a patch for async_session that uses our test session factory."""
    return patch("app.services.orchestrator.async_session", session_factory)


async def test_run_job_happy_path(test_session, session_factory, mock_measure_report):
    """run_job: happy path gathers patients, pushes data, evaluates, stores results."""
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock) as mock_wipe,
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped_wipe,
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr.example.com/fhir"
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(
                resources=[
                    {"resourceType": "Patient", "id": "p1"},
                    {"resourceType": "Condition", "id": "c1"},
                ]
            ),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
        patch(
            "app.services.orchestrator.snapshot_evaluated_resources",
            new_callable=AsyncMock,
            return_value=[
                {"resourceType": "Patient", "id": "patient-1"},
                {"resourceType": "Condition", "id": "cond-1"},
            ],
        ),
    ):
        await run_job(job_id)

    # Verify job completed
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        assert job.total_patients == 1
        assert job.processed_patients == 1
        assert job.failed_patients == 0
        assert job.completed_at is not None

        # Verify result was stored, including the evaluated_resources snapshot
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        assert results[0].patient_id == "p1"
        assert results[0].patient_name == "Alice Test"
        assert results[0].evaluated_resources == [
            {"resourceType": "Patient", "id": "patient-1"},
            {"resourceType": "Condition", "id": "cond-1"},
        ]

    # Job has no mcs_url, so the wipe falls back to the env-var engine with no credentials.
    # Issue #392: a job whose snapshot has no explicit opt-in (here, a legacy row
    # with mcs_wipe_before_job unset) takes the scoped wipe. The full wipe is
    # reserved for connections that asked for it.
    mock_wipe.assert_not_awaited()
    mock_scoped_wipe.assert_awaited_once_with(base_url=settings.MEASURE_ENGINE_URL, patient_ids=["p1"], auth_headers={})


async def test_run_job_stores_empty_list_when_snapshot_helper_returns_none(
    test_session, session_factory, mock_measure_report
):
    """When the snapshot helper returns None (no refs to resolve), the orchestrator
    stores [] not None — so the column distinguishes legacy rows (NULL) from new
    rows that were snapshotted but had no refs ([])."""
    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
        patch(
            "app.services.orchestrator.snapshot_evaluated_resources",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].evaluated_resources == [], (
            "Expected [] (snapshotted, no refs) — None would conflate with legacy rows"
        )


async def test_run_job_stores_none_when_snapshot_helper_raises(test_session, session_factory, mock_measure_report):
    """When the snapshot helper raises (genuine failure), the orchestrator stores
    None — the row falls back to live resolution at read time."""
    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
        patch(
            "app.services.orchestrator.snapshot_evaluated_resources",
            new_callable=AsyncMock,
            side_effect=RuntimeError("HAPI unreachable"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].evaluated_resources is None, (
            "Expected None (snapshot failed) — read path falls back to live resolution"
        )


async def test_run_job_no_patients(test_session, session_factory):
    """run_job: when no patients found, job completes with zero counts."""
    job_id = await _setup_job(test_session)

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock) as mock_wipe,
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped_wipe,
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        assert job.total_patients == 0

    # Issue #392 moved the wipe after the gather, so a zero-patient job returns
    # before wiping anything. Asserted rather than left implicit: it is the one
    # user-visible behavior change of the move, and the safe direction — a job
    # that evaluates nothing must not delete anything either.
    mock_wipe.assert_not_awaited()
    mock_scoped_wipe.assert_not_awaited()


async def test_run_job_wipe_failure(test_session, session_factory):
    """run_job: a failing wipe fails the job rather than evaluating stale data.

    Issue #392 moved the wipe from step 1 to just after the patient gather, so
    this test has to get past the gather before the wipe can fail. It patches the
    scoped wipe because that is now the default mode; the full-wipe equivalent is
    covered by test_run_job_full_wipe_failure_fails_the_job.
    """
    job_id = await _setup_job(test_session)
    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]}]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url",
            new_callable=AsyncMock,
            return_value="http://cdr.example.com/fhir",
        ),
        patch.object(BatchQueryStrategy, "gather_patients", new_callable=AsyncMock, return_value=patients),
        patch(
            "app.services.orchestrator.wipe_patients_by_id",
            new_callable=AsyncMock,
            side_effect=Exception("Measure engine down"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert "Measure engine down" in job.error_message


async def test_run_job_full_wipe_failure_fails_the_job(test_session, session_factory):
    """The opt-in full-wipe path must fail the job just as loudly."""
    cfg = MCSConfig(
        name="Dedicated MCS",
        mcs_url="https://dedicated.example.org/fhir",
        auth_type=AuthType.none,
        wipe_before_job=True,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
        mcs_wipe_before_job=True,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]}]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url",
            new_callable=AsyncMock,
            return_value="http://cdr.example.com/fhir",
        ),
        patch.object(BatchQueryStrategy, "gather_patients", new_callable=AsyncMock, return_value=patients),
        patch(
            "app.services.orchestrator.wipe_patient_data",
            new_callable=AsyncMock,
            side_effect=Exception("Measure engine down"),
        ),
    ):
        await run_job(job.id)

    async with session_factory() as session:
        refreshed = await session.get(Job, job.id)
        assert refreshed.status == JobStatus.failed
        assert "Measure engine down" in refreshed.error_message


# ---------------------------------------------------------------------------
# Wipe mode selection (issue #392)
# ---------------------------------------------------------------------------


async def _setup_job_with_wipe_mode(session, *, wipe_before_job: bool) -> int:
    cfg = MCSConfig(
        name=f"MCS wipe={wipe_before_job}",
        mcs_url="https://mcs-392.example.org/fhir",
        auth_type=AuthType.none,
        wipe_before_job=wipe_before_job,
    )
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
        mcs_wipe_before_job=wipe_before_job,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job.id


def _wipe_mode_patches(session_factory, patients, mock_measure_report):
    return (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url",
            new_callable=AsyncMock,
            return_value="http://cdr.example.com/fhir",
        ),
        patch.object(BatchQueryStrategy, "gather_patients", new_callable=AsyncMock, return_value=patients),
        patch.object(
            BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
    )


async def test_scoped_wipe_is_used_when_connection_did_not_opt_in(test_session, session_factory, mock_measure_report):
    """The default path: only the gathered patients are deleted.

    This is the acceptance criterion for #392 — a job against a shared MCS must
    not touch unrelated patient data.
    """
    job_id = await _setup_job_with_wipe_mode(test_session, wipe_before_job=False)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]},
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["C"], "family": "D"}]},
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _wipe_mode_patches(session_factory, patients, mock_measure_report)]
        full_wipe, scoped_wipe = mocks[1], mocks[2]
        await run_job(job_id)

    full_wipe.assert_not_awaited()
    scoped_wipe.assert_awaited_once_with(
        base_url="https://mcs-392.example.org/fhir",
        patient_ids=["p1", "p2"],
        auth_headers={},
    )


async def test_full_wipe_is_used_when_connection_opted_in(test_session, session_factory, mock_measure_report):
    """The explicit opt-in restores the historical destructive behavior."""
    job_id = await _setup_job_with_wipe_mode(test_session, wipe_before_job=True)
    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]}]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _wipe_mode_patches(session_factory, patients, mock_measure_report)]
        full_wipe, scoped_wipe = mocks[1], mocks[2]
        await run_job(job_id)

    scoped_wipe.assert_not_awaited()
    full_wipe.assert_awaited_once_with(base_url="https://mcs-392.example.org/fhir", strict=False, auth_headers={})


async def test_wipe_happens_before_the_push(test_session, session_factory, mock_measure_report):
    """Ordering guard: wiping after the push would delete this job's own data.

    The scoped wipe has to run after the gather (it needs the patient IDs) but
    before the push. Getting that backwards makes every job evaluate an empty
    server, which is why this is asserted explicitly rather than left to review.
    """
    job_id = await _setup_job_with_wipe_mode(test_session, wipe_before_job=False)
    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]}]

    call_order: list[str] = []

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _wipe_mode_patches(session_factory, patients, mock_measure_report)]
        scoped_wipe, push = mocks[2], mocks[3]

        # Return DEFAULT so the mocks still hand back their normal return_value —
        # push_resources' result is consumed downstream, and returning None from
        # the side effect would break it.
        def _record(name):
            def _side_effect(*args, **kwargs):
                call_order.append(name)
                return DEFAULT

            return _side_effect

        scoped_wipe.side_effect = _record("wipe")
        push.side_effect = _record("push")
        await run_job(job_id)

    assert call_order[:2] == ["wipe", "push"], f"wipe must precede push, got {call_order}"


async def test_run_job_cdr_unreachable(test_session, session_factory):
    """run_job: CDR unreachable when gathering patients fails the job."""
    job_id = await _setup_job(test_session)

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            side_effect=ConnectionError("CDR unreachable"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert "CDR unreachable" in job.error_message


async def test_run_job_partial_patient_failure(test_session, session_factory, mock_measure_report):
    """run_job: if evaluate fails for one patient, results for others are preserved.

    The 2-phase approach pushes all patients first (Phase 1), then evaluates
    all patients (Phase 2).  A failure during evaluation for one patient
    should not prevent other patients from being evaluated.
    """
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Good"}]},
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Bob"], "family": "Bad"}]},
    ]

    async def mock_evaluate(
        measure_id, patient_id, period_start, period_end, measure_engine_url=None, auth_headers=None
    ):
        if patient_id == "p2":
            raise Exception("Evaluation failed for p2")
        return mock_measure_report

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=mock_evaluate,
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        # One processed, one failed
        assert job.processed_patients == 1
        assert job.failed_patients == 1

        # One successful result and one per-patient error result should be stored.
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 2
        by_patient = {r.patient_id: r for r in results}
        assert by_patient["p1"].populations.get("error") is None
        assert by_patient["p2"].populations["error"] is True
        assert "Evaluation failed for p2" in by_patient["p2"].populations["error_message"]


async def test_run_job_all_patient_failures_marks_job_failed(test_session, session_factory):
    """run_job: if every patient evaluation fails, the job must not look successful."""
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Bad"}]},
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Bob"], "family": "Bad"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=Exception("HAPI returned 400"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.processed_patients == 0
        assert job.failed_patients == 2
        assert job.error_message == "All 2 patient evaluations failed"

        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 2
        assert all(r.populations["error"] is True for r in results)


async def test_run_job_cancelled_before_batches(test_session, session_factory):
    """run_job: if job is cancelled before processing, it exits early."""
    job_id = await _setup_job(test_session)

    # Cancel the job before run_job processes batches
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.cancelled
        await session.commit()

    with (
        _make_session_factory_patch(session_factory),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        # Should remain cancelled
        assert job.status == JobStatus.cancelled


async def test_run_job_nonexistent(session_factory):
    """run_job: non-existent job_id returns silently."""
    with _make_session_factory_patch(session_factory):
        # Should not raise
        await run_job(99999)


async def test_run_job_all_hapi_2788_produces_valueset_job_message(test_session, session_factory):
    """When ALL patients fail with HAPI-2788 Unknown ValueSet, job.error_message names the ValueSet URL."""
    from app.services.fhir_errors import FhirIssue, FhirOperationError, FhirOperationOutcome

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Patient", "id": "p2"},
    ]

    vs_url_encoded = "http%3A%2F%2Fcts.nlm.nih.gov%2Ffhir%2FValueSet%2F2.16.840.1.113883.3.600.1916"
    vs_url_decoded = "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.600.1916"
    diag = f"HAPI-2788: Unknown ValueSet: {vs_url_encoded}"
    hapi_outcome = FhirOperationOutcome(
        issues=[FhirIssue(severity="error", code="processing", diagnostics=diag)],
        raw={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "processing", "diagnostics": diag}],
        },
    )
    fhir_err = FhirOperationError(
        operation="evaluate-measure",
        url="http://mcs/fhir/Measure/CMS2/$evaluate-measure",
        status_code=200,
        outcome=hapi_outcome,
        latency_ms=10,
    )

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.evaluate_measure", new_callable=AsyncMock, side_effect=fhir_err),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.failed_patients == 2
        # Must name the decoded ValueSet URL, not just "All 2 patient evaluations failed"
        assert vs_url_decoded in job.error_message
        assert "ValueSet" in job.error_message


async def test_get_cdr_auth_headers_reads_live_cdr_config(test_session, session_factory):
    """_get_cdr_auth_headers joins cdr_configs via cdr_id for live credentials."""
    from app.models.config import AuthType, CDRConfig

    cfg = CDRConfig(
        name="Live CDR",
        cdr_url="http://cdr.example.com/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "test-jwt"},
        is_active=False,
        is_default=False,
        is_read_only=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        cdr_auth_type="bearer",
        cdr_id=cfg.id,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with (
        patch("app.services.orchestrator.async_session", session_factory),
        patch(
            "app.services.orchestrator._build_auth_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer test-jwt"},
        ) as mock_auth,
    ):
        headers = await _get_cdr_auth_headers(job.id)

    assert headers == {"Authorization": "Bearer test-jwt"}
    # Called with the live CDR's auth_type and credentials (decrypted by TypeDecorator)
    mock_auth.assert_called_once()
    call_auth_type = mock_auth.call_args[0][0]
    assert call_auth_type == AuthType.bearer


async def test_orchestrator_fails_clearly_when_cdr_deleted(test_session, session_factory):
    """_get_cdr_auth_headers raises RuntimeError when CDR config is gone but auth was needed."""
    job = Job(
        measure_id="m-orphan",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://gone.example.com/fhir",
        status=JobStatus.running,
        cdr_id=None,  # CDR was deleted (FK set NULL by ON DELETE SET NULL)
        cdr_auth_type="basic",  # auth was required — credentials are now unrecoverable
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.services.orchestrator.async_session", session_factory):
        with pytest.raises(RuntimeError, match="has no cdr_id"):
            await _get_cdr_auth_headers(job.id)


async def test_orchestrator_returns_empty_headers_when_no_auth(test_session, session_factory):
    """_get_cdr_auth_headers returns {} when cdr_id=None and no auth type is set."""
    job = Job(
        measure_id="m-noauth",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://direct.example.com/fhir",
        status=JobStatus.running,
        cdr_id=None,  # created without a CDR config (direct URL, unauthenticated)
        cdr_auth_type=None,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.services.orchestrator.async_session", session_factory):
        headers = await _get_cdr_auth_headers(job.id)

    assert headers == {}


async def test_orchestrator_returns_empty_headers_when_auth_type_is_none_string(test_session, session_factory):
    """_get_cdr_auth_headers returns {} when cdr_id=None and cdr_auth_type='none'."""
    job = Job(
        measure_id="m-noauth-str",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://direct2.example.com/fhir",
        status=JobStatus.running,
        cdr_id=None,
        cdr_auth_type="none",  # explicit string "none" from AuthType.none CDR config
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.services.orchestrator.async_session", session_factory):
        headers = await _get_cdr_auth_headers(job.id)

    assert headers == {}


async def test_process_batch_uses_everything_strategy(test_session, session_factory, monkeypatch):
    """DirectLoadWorkflow selects BatchQueryStrategy ($everything) by default;
    _process_single_batch just drives the pre-built workflow it's handed."""
    from unittest.mock import MagicMock

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.PATIENT_DATA_STRATEGY", "batch")

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["p1"],
        status=BatchStatus.pending,
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    patient_map = {"p1": {"resourceType": "Patient", "id": "p1"}}

    from app.services.workflows import DirectLoadWorkflow

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.workflows.BatchQueryStrategy") as mock_strategy_cls,
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [
                    {
                        "population": [
                            {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                            {"code": {"coding": [{"code": "numerator"}]}, "count": 0},
                        ]
                    }
                ],
            },
        ),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}])
        )
        mock_strategy_cls.return_value = mock_strategy

        workflow = DirectLoadWorkflow("CMS999", "http://mcs/fhir", None)
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map=patient_map,
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    mock_strategy_cls.assert_called_once_with()


async def test_process_batch_uses_data_requirements_strategy_when_configured(
    test_session, session_factory, monkeypatch
):
    """DirectLoadWorkflow can be rolled back to DataRequirementsStrategy by env
    config; _process_single_batch just drives the pre-built workflow it's handed."""
    from unittest.mock import MagicMock

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.PATIENT_DATA_STRATEGY", "data_requirements")

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["p1"],
        status=BatchStatus.pending,
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    patient_map = {"p1": {"resourceType": "Patient", "id": "p1"}}

    from app.services.workflows import DirectLoadWorkflow

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.workflows.DataRequirementsStrategy") as mock_strategy_cls,
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [
                    {
                        "population": [
                            {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                            {"code": {"coding": [{"code": "numerator"}]}, "count": 0},
                        ]
                    }
                ],
            },
        ),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}])
        )
        mock_strategy_cls.return_value = mock_strategy

        workflow = DirectLoadWorkflow("CMS999", "http://mcs/fhir", None)
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map=patient_map,
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    # Issue #397: the strategy is told which MCS to ask for $data-requirements.
    # It previously read settings.MEASURE_ENGINE_URL, so a job on a remote MCS asked
    # the local engine what data its measure needs.
    mock_strategy_cls.assert_called_once_with("CMS999", "http://mcs/fhir", None)


# ---------------------------------------------------------------------------
# Gather failure / evaluate skip invariants (PR-2 new behaviors)
# ---------------------------------------------------------------------------


async def test_run_job_gather_failure_prevents_evaluate_call(test_session, session_factory):
    """When gather raises for a patient, evaluate_measure is NOT called for that patient."""

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    evaluate_mock = AsyncMock()

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            side_effect=Exception("CDR connection refused"),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.evaluate_measure", evaluate_mock),
    ):
        await run_job(job_id)

    # evaluate_measure must NOT have been called for the failed-gather patient
    evaluate_mock.assert_not_awaited()

    # A MeasureResult error row must still exist (full exception → gather phase)
    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        assert results[0].populations["error"] is True
        assert results[0].populations["error_message"]  # back-compat field populated
        assert results[0].error_phase == "gather"


async def test_run_job_partial_gather_continues_to_evaluate(test_session, session_factory, mock_measure_report):
    """Partial gather (some resource types failed) proceeds to evaluate — AT-2."""

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]
    partial_result = GatherResult(
        resources=[{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Condition", "id": "c1"}],
        failed_types=[FailedResourceFetch(resource_type="Observation", error="500 Internal Server Error")],
    )

    evaluate_mock = AsyncMock(return_value=mock_measure_report)

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=partial_result,
        ),
        patch(
            "app.services.workflows.push_resources",
            new_callable=AsyncMock,
        ),
        patch("app.services.orchestrator.evaluate_measure", evaluate_mock),
    ):
        await run_job(job_id)

    # evaluate_measure MUST have been called despite partial gather
    evaluate_mock.assert_awaited_once()

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        mr = results[0]
        # populations come from evaluate (real data, not all-False error row)
        assert mr.populations is not None
        assert mr.populations.get("error") is not True
        # partial gather warning annotated on the result
        assert mr.error_phase == "gather_partial"
        assert mr.error_details is not None
        assert "Observation" in mr.error_details["failed_types"]
        assert "Patient" in mr.error_details["succeeded_types"] or "Condition" in mr.error_details["succeeded_types"]


async def test_run_job_evaluate_failure_persists_error_details_and_back_compat(
    test_session, session_factory, mock_measure_report
):
    """Evaluate phase failures persist error_details AND back-compat error_message."""
    from app.services.fhir_errors import FhirOperationError

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    fhir_err = FhirOperationError(
        operation="evaluate-measure",
        url="http://mcs/fhir/Measure/m1/$evaluate-measure",
        status_code=404,
        outcome=None,
        latency_ms=42,
    )

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=fhir_err,
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        mr = results[0]
        assert mr.populations["error"] is True
        # Back-compat: sanitized string still written
        assert mr.populations["error_message"]
        # Structured details written
        assert mr.error_details is not None
        assert mr.error_details["operation"] == "evaluate-measure"
        assert mr.error_details["status_code"] == 404
        # error_phase set to evaluate
        assert mr.error_phase == "evaluate"


async def test_run_job_sets_started_at_on_transition_to_running(test_session, session_factory, mock_measure_report):
    """run_job stamps started_at at the queued→running transition."""
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url",
            new_callable=AsyncMock,
            return_value="http://cdr.example.com/fhir",
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=__import__("app.services.fhir_client", fromlist=["GatherResult"]).GatherResult(
                resources=[{"resourceType": "Patient", "id": "p1"}]
            ),
        ),
        patch("app.services.workflows.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.started_at is not None, "started_at must be set when job transitions to running"
        assert job.status == JobStatus.complete
        # started_at must be before or equal to completed_at
        assert job.started_at <= job.completed_at


# ---------------------------------------------------------------------------
# _get_mcs_auth_headers — MCS credentials must reach the measure engine.
# Regression: remote MCS jobs failed every patient with HTTP 401
# "Authorization header missing Bearer token" because auth was never resolved.
# ---------------------------------------------------------------------------


async def test_get_mcs_auth_headers_builds_from_linked_config(test_session, session_factory):
    """A bearer-authed MCS config yields an Authorization header for $evaluate-measure."""
    cfg = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-123"},
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with (
        patch("app.services.orchestrator.async_session", session_factory),
        patch(
            "app.services.fhir_client._build_auth_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer tok-123"},
        ) as mock_auth,
    ):
        headers = await _get_mcs_auth_headers(job.id)

    assert headers == {"Authorization": "Bearer tok-123"}
    assert mock_auth.call_args[0][0] == AuthType.bearer


async def test_get_mcs_auth_headers_empty_when_no_mcs_linked(test_session, session_factory):
    """Local/legacy jobs with no mcs_id need no credentials."""
    job = Job(
        measure_id="m-local",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_id=None,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.services.orchestrator.async_session", session_factory):
        assert await _get_mcs_auth_headers(job.id) == {}


async def test_get_mcs_auth_headers_raises_when_config_deleted(test_session, session_factory):
    """Deleting the MCS config must fail the job loudly, not run it unauthenticated.

    `Job.mcs_id` is ON DELETE SET NULL, so a deleted config leaves mcs_id NULL —
    never dangling. The snapshotted `mcs_auth_type` is the only thing that
    distinguishes this from a job that never had MCS auth. Without it the job
    would silently wipe and evaluate against the still-snapshotted remote
    `mcs_url` with no credentials.
    """
    cfg = MCSConfig(
        name="Doomed MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-123"},
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-orphan",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
        mcs_auth_type="bearer",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    await test_session.delete(cfg)
    await test_session.commit()
    await test_session.refresh(job)

    # The FK nulled the id rather than leaving it dangling — this is the state
    # production actually reaches, and the one the old test never exercised.
    assert job.mcs_id is None
    assert job.mcs_url == "https://mcs.example.org/fhir"

    with patch("app.services.orchestrator.async_session", session_factory):
        with pytest.raises(RuntimeError, match="deleted after"):
            await _get_mcs_auth_headers(job.id)


async def test_get_mcs_auth_headers_empty_when_deleted_config_had_no_auth(test_session, session_factory):
    """A deleted config that never needed credentials is not an error."""
    cfg = MCSConfig(name="Local", mcs_url="http://local:8080/fhir", auth_type=AuthType.none)
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-local",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
        mcs_auth_type="none",
    )
    test_session.add(job)
    await test_session.commit()

    await test_session.delete(cfg)
    await test_session.commit()

    with patch("app.services.orchestrator.async_session", session_factory):
        assert await _get_mcs_auth_headers(job.id) == {}


async def test_get_mcs_auth_headers_raises_when_config_repointed(test_session, session_factory):
    """Repointing a config must not send the new server's token to the old one.

    `mcs_url` is read from the frozen job snapshot but credentials are read live,
    so without this guard a config edited to a new host would hand that host's
    bearer token to the host the job was created against.
    """
    cfg = MCSConfig(
        name="Vendor A",
        mcs_url="https://vendor-a.example/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "token-a"},
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url="https://vendor-a.example/fhir",
        mcs_id=cfg.id,
        mcs_auth_type="bearer",
    )
    test_session.add(job)
    await test_session.commit()

    cfg.mcs_url = "https://vendor-b.example/fhir"
    cfg.auth_credentials = {"token": "token-b"}
    await test_session.commit()

    with patch("app.services.orchestrator.async_session", session_factory):
        with pytest.raises(RuntimeError, match="different server"):
            await _get_mcs_auth_headers(job.id)


async def test_run_job_targets_job_mcs_with_credentials(test_session, session_factory, mock_measure_report):
    """Wipe, push, and evaluate all target the job's MCS with its credentials.

    Regression: jobs against a remote MCS pushed patient data to the env-var
    engine (so the remote never received it) and evaluated without auth (so every
    patient 401'd). Both had to be true for a remote MCS job to produce results.
    """
    cfg = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-123"},
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    job_id = job.id

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"given": ["A"], "family": "B"}]}]
    expected_auth = {"Authorization": "Bearer tok-123"}

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock) as mock_wipe,
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped_wipe,
        patch("app.services.workflows.push_resources", new_callable=AsyncMock) as mock_push,
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url",
            new_callable=AsyncMock,
            return_value="http://cdr.example.com/fhir",
        ),
        patch.object(BatchQueryStrategy, "gather_patients", new_callable=AsyncMock, return_value=patients),
        patch.object(
            BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ) as mock_eval,
    ):
        await run_job(job_id)

    # Wipe cleans the MCS this job will actually use — not the env-var engine.
    # Since issue #392 a user-created connection defaults to the scoped wipe, so
    # the assertion moved from wipe_patient_data to wipe_patients_by_id. The
    # property under guard is unchanged: the wipe targets the job's MCS, with the
    # job's credentials.
    mock_wipe.assert_not_awaited()
    mock_scoped_wipe.assert_awaited_once_with(
        base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=expected_auth
    )
    # Patient data is pushed to that same MCS, authenticated.
    assert mock_push.await_args.kwargs["target_url"] == "https://mcs.example.org/fhir"
    assert mock_push.await_args.kwargs["auth_headers"] == expected_auth
    # Evaluation carries the credentials that were missing in the 401 regression.
    assert mock_eval.await_args.kwargs["measure_engine_url"] == "https://mcs.example.org/fhir"
    assert mock_eval.await_args.kwargs["auth_headers"] == expected_auth


# ---------------------------------------------------------------------------
# Workflow wiring: TransferPhaseError unwrapping (Task 5)
# ---------------------------------------------------------------------------


class _StubWorkflow(SubmissionWorkflow):
    name = "stub"

    def __init__(self, outcome):
        self._outcome = outcome  # GatherResult | Exception

    async def transfer_patient(self, cdr_url, patient_id, cdr_auth_headers):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _run_job_patches(session_factory, patients, stub_workflow):
    """Common patch set for stubbed-workflow run_job tests."""
    return (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch(
            "app.services.orchestrator.build_submission_workflow",
            new_callable=AsyncMock,
            return_value=stub_workflow,
        ),
    )


async def test_submit_phase_failure_recorded_as_submit(test_session, session_factory):
    """A TransferPhaseError(phase='submit') lands in MeasureResult.error_phase and skips evaluate."""
    job_id = await _setup_job(test_session)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.workflow = "deqm_submit_data"
        await session.commit()

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _StubWorkflow(TransferPhaseError("submit", RuntimeError("MCS rejected the payload")))

    with contextlib.ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        mock_eval = stack.enter_context(patch("app.services.orchestrator.evaluate_measure", new_callable=AsyncMock))
        await run_job(job_id)

    mock_eval.assert_not_awaited()
    async with session_factory() as session:
        row = (await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))).scalar_one()
        assert row.error_phase == "submit"
        assert row.populations["error_phase"] == "submit"
        assert row.populations["error"] is True


async def test_direct_load_gather_failure_still_recorded_as_gather(test_session, session_factory):
    """Regression: phase labeling for direct_load transfer failures is unchanged."""
    job_id = await _setup_job(test_session)
    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _StubWorkflow(TransferPhaseError("gather", RuntimeError("CDR down")))

    with contextlib.ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        stack.enter_context(patch("app.services.orchestrator.evaluate_measure", new_callable=AsyncMock))
        await run_job(job_id)

    async with session_factory() as session:
        row = (await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))).scalar_one()
        assert row.error_phase == "gather"


# ---------------------------------------------------------------------------
# build_submission_workflow wiring (coverage-audit gap fill)
# ---------------------------------------------------------------------------


async def test_run_job_passes_job_fields_to_build_submission_workflow(test_session, session_factory):
    """run_job must forward the job's own snapshot fields (workflow, measure_id,
    mcs_url/auth, submit_data_mode, period) to build_submission_workflow — not
    env defaults or another job's values. Every prior test mocks this call
    without asserting its arguments."""
    job_id = await _setup_job(test_session)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.workflow = "deqm_submit_data"
        job.submit_data_mode = "stu5"
        job.measure_id = "measure-1"
        job.period_start = "2024-01-01"
        job.period_end = "2024-12-31"
        await session.commit()

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _StubWorkflow(GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]))

    with contextlib.ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        build_mock = stack.enter_context(
            patch(
                "app.services.orchestrator.build_submission_workflow",
                new_callable=AsyncMock,
                return_value=stub,
            )
        )
        # Shape the return value: run_job feeds it straight into
        # `measure_report.get("group", [])`. A bare AsyncMock hands back an
        # unconfigured MagicMock there, which emitted "coroutine
        # 'AsyncMockMixin._execute_mock_call' was never awaited" from the
        # population loop. The assertion below only covers
        # build_submission_workflow's arguments, so an empty group list is
        # enough -- it just has to be the shape the real call returns.
        stack.enter_context(
            patch(
                "app.services.orchestrator.evaluate_measure",
                new_callable=AsyncMock,
                return_value={"resourceType": "MeasureReport", "group": []},
            )
        )
        await run_job(job_id)

    build_mock.assert_awaited_once_with(
        workflow="deqm_submit_data",
        job_id=job_id,
        measure_id="measure-1",
        mcs_url=settings.MEASURE_ENGINE_URL,
        mcs_auth_headers={},
        submit_data_mode="stu5",
        period_start="2024-01-01",
        period_end="2024-12-31",
    )


async def test_run_job_build_submission_workflow_failure_skips_wipe_and_fails_job(test_session, session_factory):
    """If build_submission_workflow raises (e.g. a DEQM canonical-fetch failure),
    run_job must fail fast BEFORE the wipe — a canonical-fetch failure must not
    wipe the MCS and then fail anyway (see orchestrator.py comment above the
    build_submission_workflow call)."""
    from app.services.fhir_errors import FhirOperationError

    job_id = await _setup_job(test_session)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.workflow = "deqm_submit_data"
        await session.commit()

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock) as mock_wipe,
        patch("app.services.orchestrator.wipe_patients_by_id", new_callable=AsyncMock) as mock_scoped_wipe,
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(BatchQueryStrategy, "gather_patients", new_callable=AsyncMock, return_value=patients),
        patch(
            "app.services.orchestrator.build_submission_workflow",
            new_callable=AsyncMock,
            side_effect=FhirOperationError(
                operation="read-measure",
                url="http://mcs/Measure/measure-1",
                status_code=404,
                outcome=None,
                latency_ms=1,
            ),
        ),
    ):
        await run_job(job_id)

    mock_wipe.assert_not_awaited()
    mock_scoped_wipe.assert_not_awaited()
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.error_message is not None


class _OrderRecordingStubWorkflow(_StubWorkflow):
    """Stub that records when its post-wipe prerequisite hook runs."""

    name = "order-stub"

    def __init__(self, outcome, calls: list[str]):
        super().__init__(outcome)
        self._calls = calls

    async def ensure_target_prerequisites(self) -> None:
        self._calls.append("prereq")


async def test_run_job_stages_prerequisites_after_the_wipe(test_session, session_factory):
    """Regression: the workflow's prerequisite staging must run AFTER the wipe.

    `wipe_patient_data`'s full-wipe list includes "Organization", and
    `build_submission_workflow` runs BEFORE the wipe (deliberately, so a
    canonical-fetch failure aborts without wiping). Staging the DEQM reporter
    Organization at build time therefore had it deleted moments later, leaving
    every MeasureReport in the job pointing at a reporter that no longer
    existed. Since $submit-data is transaction-backed, a dangling reference
    fails that patient's entire submission.

    Only bites when mcs_wipe_before_job is set, which is why this pins the
    ordering rather than the symptom.
    """
    job_id = await _setup_job(test_session)
    calls: list[str] = []
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.workflow = "deqm_submit_data"
        job.mcs_wipe_before_job = True  # full wipe -> deletes Organization
        await session.commit()

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _OrderRecordingStubWorkflow(GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]), calls)

    with contextlib.ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        # Re-patch the full wipe so it records its own ordering.
        stack.enter_context(
            patch(
                "app.services.orchestrator.wipe_patient_data",
                new=AsyncMock(side_effect=lambda *a, **k: calls.append("wipe")),
            )
        )
        stack.enter_context(
            patch(
                "app.services.orchestrator.evaluate_measure",
                new_callable=AsyncMock,
                return_value={"resourceType": "MeasureReport", "group": []},
            )
        )
        await run_job(job_id)

    assert calls == ["wipe", "prereq"], (
        f"expected the wipe to precede prerequisite staging, got {calls}. "
        "Staging before the wipe means the full wipe deletes the DEQM reporter Organization."
    )
