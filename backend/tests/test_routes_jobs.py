"""Tests for job endpoints (POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def measure_present():
    """POST /jobs pre-flights the measure against the active MCS (issue #396).

    Default every test in this module to "the measure is there" so the existing
    creation tests keep exercising job creation rather than the pre-flight.
    Tests that care about the pre-flight override `.return_value` /
    `.side_effect` on the yielded mock.
    """
    with patch("app.routes.jobs.measure_exists", new_callable=AsyncMock, return_value=True) as mock:
        yield mock


async def test_create_job_valid(client):
    """POST /jobs with valid payload creates a job with QUEUED status."""
    payload = {
        "measure_id": "measure-1",
        "measure_name": "Test Measure",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["measure_id"] == "measure-1"
    assert data["measure_name"] == "Test Measure"
    assert data["period_start"] == "2024-01-01"
    assert data["period_end"] == "2024-12-31"
    assert data["cdr_url"] == "https://example.com/fhir"
    assert data["status"] == "queued"
    assert data["total_patients"] == 0
    assert data["processed_patients"] == 0
    assert data["failed_patients"] == 0
    assert data["id"] is not None
    assert "cdr_name" in data
    assert "cdr_read_only" in data


async def test_create_job_ssrf_cdr_url_blocked(client):
    """POST /jobs with a private IP cdr_url override returns 400."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://169.254.169.254/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "SSRF protection" in diag


async def test_create_job_missing_fields(client):
    """POST /jobs with missing required fields returns 422."""
    # Missing measure_id, period_start, period_end
    payload = {"measure_name": "Incomplete"}
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_path_bearing_measure_id_rejected(client):
    """F7: measure_id is interpolated into CDR/MCS URL paths (e.g.
    Measure/{measure_id}/$submit-data) — a value that could rewrite that path
    must be rejected with 422, mirroring the existing group_id validator."""
    payload = {
        "measure_id": "../../etc/passwd",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_uses_default_cdr_url(client):
    """POST /jobs without cdr_url falls back to default."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    # Should use the DEFAULT_CDR_URL from settings
    assert data["cdr_url"] is not None
    assert len(data["cdr_url"]) > 0
    assert data["cdr_read_only"] is False


async def test_list_jobs_empty(client):
    """GET /jobs on empty database returns an empty list."""
    resp = await client.get("/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_returns_created_jobs(client):
    """GET /jobs returns all created jobs."""
    # Create two jobs
    for i in range(2):
        await client.post(
            "/jobs",
            json={
                "measure_id": f"measure-{i}",
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "cdr_url": "https://example.com/fhir",
            },
        )

    resp = await client.get("/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Both jobs should be present
    measure_ids = {j["measure_id"] for j in data}
    assert measure_ids == {"measure-0", "measure-1"}


async def test_get_job_with_batches(client):
    """GET /jobs/{id} returns job details including batches list."""
    create_resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "cdr_url": "https://example.com/fhir",
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job_id
    assert data["measure_id"] == "measure-1"
    assert "batches" in data
    assert isinstance(data["batches"], list)


async def test_get_job_not_found(client):
    """GET /jobs/{id} with non-existent ID returns 404."""
    resp = await client.get("/jobs/99999")
    assert resp.status_code == 404
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert data["issue"][0]["code"] == "not-found"


async def test_cancel_job_queued(client):
    """POST /jobs/{id}/cancel cancels a queued job."""
    create_resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "cdr_url": "https://example.com/fhir",
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["completed_at"] is not None


async def test_cancel_job_not_found(client):
    """POST /jobs/{id}/cancel with non-existent ID returns 404."""
    resp = await client.post("/jobs/99999/cancel")
    assert resp.status_code == 404


async def test_cancel_already_complete_job(client, test_session):
    """POST /jobs/{id}/cancel on a completed job returns 409."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://example.com/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    resp = await client.post(f"/jobs/{job.id}/cancel")
    assert resp.status_code == 409
    data = resp.json()["detail"]
    assert data["issue"][0]["code"] == "conflict"


async def test_create_job_with_group_id(client):
    """POST /jobs with group_id stores it on the job."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://example.com/fhir",
        "group_id": "CMS349FHIRHIVScreening",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] == "CMS349FHIRHIVScreening"


async def test_create_job_without_group_id(client):
    """POST /jobs without group_id defaults to null."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] is None


async def test_get_groups_success(client):
    """GET /jobs/groups returns list of groups from CDR."""
    from unittest.mock import AsyncMock, patch

    mock_groups = [
        {"id": "CMS122FHIRDiabetes", "name": "CMS122 Diabetes", "type": "person", "member_count": 20},
        {"id": "CMS349FHIRHIVScreening", "name": "CMS349 HIV Screening", "type": "person", "member_count": 36},
    ]
    with (
        patch("app.routes.jobs.list_groups", new=AsyncMock(return_value=mock_groups)) as mock_lg,
        patch("app.routes.jobs._build_auth_headers", new=AsyncMock(return_value={})) as mock_auth,
    ):
        resp = await client.get("/jobs/groups")

    assert resp.status_code == 200
    mock_auth.assert_called_once()
    mock_lg.assert_called_once()
    data = resp.json()
    assert "groups" in data
    assert len(data["groups"]) == 2
    assert data["groups"][0]["id"] == "CMS122FHIRDiabetes"


async def test_get_groups_cdr_unreachable(client):
    """GET /jobs/groups returns 502 when CDR is unreachable."""
    from unittest.mock import AsyncMock, patch

    import httpx

    with patch("app.routes.jobs.list_groups", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = await client.get("/jobs/groups")
    assert resp.status_code == 502
    assert "CDR" in resp.json()["detail"]


async def test_create_job_stamps_active_cdr_metadata(client, test_session):
    """POST /jobs stamps cdr_name and cdr_read_only from the active CDR config."""
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    # Deactivate any existing active CDR rows first
    await test_session.execute(sa_update(CDRConfig).values(is_active=False))
    await test_session.commit()

    cdr = CDRConfig(
        cdr_url="http://prod-cdr.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Production CDR",
        is_default=False,
        is_read_only=True,
    )
    test_session.add(cdr)
    await test_session.commit()

    resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["cdr_url"] == "http://prod-cdr.example.com/fhir"
    assert data["cdr_name"] == "Production CDR"
    assert data["cdr_read_only"] is True


async def test_create_job_stamps_cdr_auth_type_as_string_value(client, test_session):
    """POST /jobs stamps cdr_auth_type as the raw string value (e.g. 'bearer'), not 'AuthType.bearer'."""
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    # Deactivate any existing active CDR rows first
    await test_session.execute(sa_update(CDRConfig).values(is_active=False))
    await test_session.commit()

    cdr = CDRConfig(
        cdr_url="http://auth-cdr.example.com/fhir",
        auth_type=AuthType.bearer,
        is_active=True,
        name="Auth CDR",
        is_default=False,
        is_read_only=False,
        auth_credentials={"token": "my-token"},
    )
    test_session.add(cdr)
    await test_session.commit()
    await test_session.refresh(cdr)

    resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
        },
    )
    assert resp.status_code == 201

    # Verify the stamped cdr_auth_type is the plain string "bearer"
    # and cdr_id FK points at the active CDR config
    from sqlalchemy import select

    from app.models.job import Job

    result = await test_session.execute(select(Job).order_by(Job.id.desc()).limit(1))
    job = result.scalar_one()
    assert job.cdr_auth_type == "bearer"
    assert job.cdr_id == cdr.id


async def test_list_jobs_includes_batch_counts(client, test_session):
    """GET /jobs includes total_batches and batches_completed counts."""
    from app.models.job import Batch, BatchStatus, Job, JobStatus

    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="https://example.com/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.flush()
    test_session.add(Batch(job_id=job.id, batch_number=1, patient_ids=["p1"], status=BatchStatus.complete))
    test_session.add(Batch(job_id=job.id, batch_number=2, patient_ids=["p2"], status=BatchStatus.complete))
    test_session.add(Batch(job_id=job.id, batch_number=3, patient_ids=["p3"], status=BatchStatus.pending))
    await test_session.commit()

    resp = await client.get("/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["total_batches"] == 3
    assert data[0]["batches_completed"] == 2


async def test_list_jobs_includes_delete_requested(client, test_session):
    """GET /jobs includes delete_requested so the UI can reflect pending deletion."""
    from app.models.job import Job

    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="https://example.com/fhir",
        delete_requested=True,
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.get("/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["delete_requested"] is True


async def test_delete_terminal_job_removes_results_and_batches(client, test_session):
    """DELETE /jobs/{id} immediately removes terminal jobs and cascades dependents."""
    from app.models.job import Batch, BatchStatus, Job, JobStatus, MeasureResult

    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="https://example.com/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.flush()
    batch = Batch(job_id=job.id, batch_number=1, patient_ids=["p1"], status=BatchStatus.complete)
    result = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        patient_name="Patient One",
        measure_report={"resourceType": "MeasureReport"},
        populations={"initial_population": True},
    )
    test_session.add_all([batch, result])
    await test_session.commit()
    job_id = job.id
    batch_id = batch.id
    result_id = result.id

    resp = await client.delete(f"/jobs/{job_id}")
    assert resp.status_code == 204
    test_session.expire_all()
    assert await test_session.get(Job, job_id) is None
    assert await test_session.get(Batch, batch_id) is None
    assert await test_session.get(MeasureResult, result_id) is None


async def test_delete_queued_job_marks_for_delete_and_cleanup_removes_it(client, test_session):
    """DELETE /jobs/{id} on a queued job returns 202 and worker cleanup removes it."""
    from app.models.job import Job
    from app.services.worker import _cleanup_delete_requested_jobs

    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="https://example.com/fhir",
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.delete(f"/jobs/{job.id}")
    assert resp.status_code == 202
    data = resp.json()
    assert data["delete_requested"] is True

    await test_session.refresh(job)
    assert job.delete_requested is True

    deleted = await _cleanup_delete_requested_jobs(test_session)
    assert deleted == 1
    assert await test_session.get(Job, job.id) is None


async def test_delete_running_job_sets_delete_requested(client, test_session):
    """DELETE /jobs/{id} on a running job returns 202 and leaves the row pending deletion."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="https://example.com/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.delete(f"/jobs/{job.id}")
    assert resp.status_code == 202
    await test_session.refresh(job)
    assert job.delete_requested is True


# ---------------------------------------------------------------------------
# GET /jobs/{id}/measure-report
# ---------------------------------------------------------------------------


async def test_get_job_measure_report_success(client, test_session):
    """Bundle contains only successful patients; error patients are excluded."""
    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.flush()

    report = {"resourceType": "MeasureReport", "type": "individual", "group": []}

    # 3 successful patients
    for pid in ("p1", "p2", "p3"):
        test_session.add(
            MeasureResult(
                job_id=job.id,
                patient_id=pid,
                measure_report=report,
                populations={"initial_population": True, "denominator": True, "numerator": False},
            )
        )

    # 1 error patient — should be excluded
    test_session.add(
        MeasureResult(
            job_id=job.id,
            patient_id="p-err",
            measure_report={"resourceType": "OperationOutcome"},
            populations={"error": True, "error_message": "gather failed", "error_phase": "gather"},
            error_phase="gather",
        )
    )
    await test_session.commit()

    resp = await client.get(f"/jobs/{job.id}/measure-report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resourceType"] == "Bundle"
    assert data["type"] == "collection"
    assert data["total"] == 3
    assert len(data["entry"]) == 3
    assert all(e["resource"]["resourceType"] == "MeasureReport" for e in data["entry"])
    assert "timestamp" in data


async def test_get_job_measure_report_includes_gather_partial(client, test_session):
    """gather_partial patients have real MeasureReports and no populations['error'] key — included."""
    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.flush()

    report = {"resourceType": "MeasureReport", "type": "individual", "group": []}
    test_session.add(
        MeasureResult(
            job_id=job.id,
            patient_id="p-partial",
            measure_report=report,
            populations={"initial_population": True, "denominator": False, "numerator": False},
            error_phase="gather_partial",
            error_details={"operation": "gather", "failed_types": ["Observation"]},
        )
    )
    await test_session.commit()

    resp = await client.get(f"/jobs/{job.id}/measure-report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["entry"][0]["resource"]["resourceType"] == "MeasureReport"


async def test_get_job_measure_report_job_not_found(client):
    """Returns 404 OperationOutcome when job does not exist."""
    resp = await client.get("/jobs/99999/measure-report")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert detail["issue"][0]["code"] == "not-found"


async def test_get_job_measure_report_no_results(client, test_session):
    """Returns 404 when job exists but has no MeasureResult rows."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS124",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.get(f"/jobs/{job.id}/measure-report")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert detail["issue"][0]["code"] == "not-found"


async def test_get_job_measure_report_in_progress_returns_partial_bundle(client, test_session):
    """In-progress jobs return a partial bundle — no status gate is applied."""
    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.flush()

    report = {"resourceType": "MeasureReport", "type": "individual", "group": []}
    test_session.add(
        MeasureResult(
            job_id=job.id,
            patient_id="p1",
            measure_report=report,
            populations={"initial_population": True},
        )
    )
    await test_session.commit()

    resp = await client.get(f"/jobs/{job.id}/measure-report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1


async def test_get_job_measure_report_all_errors_returns_empty_bundle(client, test_session):
    """When all results are errors, returns 200 with an empty bundle (not 404)."""
    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.flush()

    test_session.add(
        MeasureResult(
            job_id=job.id,
            patient_id="p-err",
            measure_report={"resourceType": "OperationOutcome"},
            populations={"error": True, "error_message": "gather failed", "error_phase": "gather"},
            error_phase="gather",
        )
    )
    await test_session.commit()

    resp = await client.get(f"/jobs/{job.id}/measure-report")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resourceType"] == "Bundle"
    assert data["total"] == 0
    assert data["entry"] == []


# ---------------------------------------------------------------------------
# GET /jobs/{id}/comparison
# ---------------------------------------------------------------------------


async def test_get_comparison_unreachable_mcs_returns_502(client, test_session):
    """An unreachable measure engine is reported, not disguised as missing data.

    Contract change (issue #397). This previously returned 200 with
    has_expected=False, which the UI renders as "No expected results available for
    this measure and period. Load a connectathon bundle via Settings" — telling the
    user to load data they already have, when the real problem is that Lenny could
    not reach the server. ComparisonView already renders "Comparison unavailable:
    {error}" on a rejected fetch; it just never received one.
    """
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={"resourceType": "MeasureReport", "group": []},
        populations={"initial_population": True},
    )
    test_session.add(mr)
    await test_session.commit()

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_httpx.ConnectError("unreachable"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["detail"]["resourceType"] == "OperationOutcome"


async def test_get_comparison_measure_absent_still_returns_200_empty(client, test_session):
    """A 404 from the engine means "no such measure", which is not an outage.

    The distinction that matters for #397: "I could not ask the server" is an error;
    "the server answered and the measure is not there" is a legitimate empty result.
    Collapsing both into 502 would replace one misleading message with another.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS-nonexistent",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=httpx.Response(404, json={}, request=httpx.Request("GET", "http://x")))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200, resp.text
    assert resp.json()["has_expected"] is False


async def test_get_comparison_resolves_measure_against_the_jobs_mcs(client, test_session):
    """The lookup targets job.mcs_url with that job's credentials (issue #397).

    A historical job's comparison must resolve against the server it actually ran
    on, not settings.MEASURE_ENGINE_URL and not whatever MCS happens to be active
    now. Sending no credentials also meant an authenticated remote MCS 401'd, the
    bare except swallowed it, and the view silently degraded.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.connection_base import AuthType
    from app.models.job import Job, JobStatus
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-cmp"},
        is_active=True,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
        mcs_url=cfg.mcs_url,
        mcs_id=cfg.id,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"url": "http://cms.gov/Measure/CMS124"}, request=httpx.Request("GET", url))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200, resp.text
    assert seen["url"].startswith("https://mcs.example.org/fhir/Measure/CMS124"), seen["url"]
    assert seen["headers"] == {"Authorization": "Bearer tok-cmp"}, seen["headers"]


async def test_get_comparison_legacy_job_without_mcs_url_still_works(client, test_session):
    """A job predating the mcs_url snapshot falls back to the env-var engine.

    Same back-compat rule as orchestrator._get_mcs_url. Without it, every
    historical job's comparison view would break on upgrade.
    """
    from unittest.mock import AsyncMock, patch

    from app.config import settings as app_settings
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
        mcs_url=None,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        return httpx.Response(200, json={"url": "http://cms.gov/Measure/CMS124"}, request=httpx.Request("GET", url))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200, resp.text
    assert seen["url"].startswith(app_settings.MEASURE_ENGINE_URL), seen["url"]


async def test_get_comparison_patient_not_in_expected_skipped(client, test_session):
    """Actual patients with no matching ExpectedResult are counted but excluded from comparison rows."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    # p1 has expected results; p2 does not
    for pid in ("p1", "p2"):
        test_session.add(
            MeasureResult(
                job_id=job.id,
                patient_id=pid,
                measure_report={
                    "resourceType": "MeasureReport",
                    "group": [
                        {
                            "population": [
                                {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            ]
                        }
                    ],
                },
                populations={"initial_population": True},
            )
        )

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    # Only p1 appears — p2 had no expected result and was skipped
    assert data["total"] == 1
    assert data["actual_total"] == 2
    assert data["unexpected_results"] == 1
    assert data["patients"][0]["subject_reference"] == "Patient/p1"


async def test_get_comparison_reports_missing_expected_patient_results(client, test_session):
    """Expected patients without MeasureResult rows fail comparison instead of being hidden."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    test_session.add(
        ExpectedResult(
            measure_url="https://example.com/Measure/CMS124",
            patient_ref="p1",
            expected_populations={"initial-population": 0, "denominator": 0, "numerator": 0},
            period_start="2019-01-01",
            period_end="2019-12-31",
            source_bundle="test",
        )
    )
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    assert data["matched"] == 0
    assert data["total"] == 1
    assert data["expected_total"] == 1
    assert data["actual_total"] == 0
    assert data["missing_results"] == 1
    assert data["patients"][0]["match"] is False
    assert data["patients"][0]["mismatches"] == ["missing-result"]


async def test_get_comparison_with_mismatch(client, test_session):
    """Returns match=False and mismatches list when actual populations differ from expected."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    # Actual: numerator=0; Expected: numerator=1 → mismatch
    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={
            "resourceType": "MeasureReport",
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
        populations={"initial_population": True, "denominator": True, "numerator": False},
    )
    test_session.add(mr)

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1, "denominator": 1, "numerator": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    assert data["matched"] == 0
    assert data["total"] == 1
    patient = data["patients"][0]
    assert patient["match"] is False
    assert len(patient["mismatches"]) > 0


async def test_get_comparison_no_job(client):
    """Returns 404 when job does not exist."""
    resp = await client.get("/jobs/999/comparison")
    assert resp.status_code == 404


async def test_get_comparison_no_results(client, test_session):
    """Returns has_expected=False when no MeasureResults exist for job.

    The measure lookup is now mocked to SUCCEED. Previously this test made a real
    HTTP call to the docker-internal engine hostname, which failed, and the bare
    `except` turned that into the same 200-with-empty this asserts — so it passed
    for the wrong reason. With the lookup succeeding, it exercises what its name
    says: a job with no expected results and no MeasureResults.
    """
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            return_value=httpx.Response(
                200, json={"url": "http://cms.gov/Measure/CMS124"}, request=httpx.Request("GET", "http://x")
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is False
    assert data["patients"] == []


async def test_get_comparison_no_expected_in_db(client, test_session):
    """Returns has_expected=False when MeasureResults exist but no ExpectedResult in DB."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={"resourceType": "MeasureReport", "group": []},
        populations={"initial_population": True},
    )
    test_session.add(mr)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is False


async def test_get_comparison_with_match(client, test_session):
    """Returns comparison data when expected results exist and populations match."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={
            "resourceType": "MeasureReport",
            "group": [
                {
                    "population": [
                        {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                        {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                        {"code": {"coding": [{"code": "numerator"}]}, "count": 1},
                    ]
                }
            ],
        },
        populations={"initial_population": True, "denominator": True, "numerator": True},
    )
    test_session.add(mr)

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1, "denominator": 1, "numerator": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    assert data["matched"] == 1
    assert data["total"] == 1
    assert data["patients"][0]["match"] is True
    assert data["patients"][0]["subject_reference"] == "Patient/p1"


# ---------------------------------------------------------------------------
# _job_to_response serialization of started_at
# ---------------------------------------------------------------------------


def test_job_to_response_serializes_started_at():
    """_job_to_response renders started_at as an ISO string when set, None when absent."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from app.models.job import JobStatus
    from app.routes.jobs import _job_to_response

    known_dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Case 1: started_at is set
    job_with = MagicMock()
    job_with.started_at = known_dt
    job_with.completed_at = None
    job_with.created_at = known_dt
    job_with.status = JobStatus.running
    job_with.batches = []
    job_with.id = 1
    job_with.measure_id = "m-1"
    job_with.measure_name = None
    job_with.period_start = "2024-01-01"
    job_with.period_end = "2024-12-31"
    job_with.cdr_url = "http://cdr/fhir"
    job_with.cdr_name = None
    job_with.cdr_read_only = False
    job_with.group_id = None
    job_with.total_patients = 0
    job_with.processed_patients = 0
    job_with.failed_patients = 0
    job_with.delete_requested = False
    job_with.error_message = None

    result_with = _job_to_response(job_with)
    assert result_with["started_at"] == known_dt.isoformat()

    # Case 2: started_at is None (job hasn't started yet)
    job_without = MagicMock()
    job_without.started_at = None
    job_without.completed_at = None
    job_without.created_at = known_dt
    job_without.status = JobStatus.queued
    job_without.batches = []
    job_without.id = 2
    job_without.measure_id = "m-2"
    job_without.measure_name = None
    job_without.period_start = "2024-01-01"
    job_without.period_end = "2024-12-31"
    job_without.cdr_url = "http://cdr/fhir"
    job_without.cdr_name = None
    job_without.cdr_read_only = False
    job_without.group_id = None
    job_without.total_patients = 0
    job_without.processed_patients = 0
    job_without.failed_patients = 0
    job_without.delete_requested = False
    job_without.error_message = None

    result_without = _job_to_response(job_without)
    assert result_without["started_at"] is None


# ---------------------------------------------------------------------------
# POST /jobs measure pre-flight against the active MCS (issue #396)
# ---------------------------------------------------------------------------


async def _make_active_mcs(test_session):
    """Activate a named MCS row and return it."""
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Attendee MCS",
        mcs_url="https://attendee-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        request_timeout_seconds=45,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


_JOB_PAYLOAD = {
    "measure_id": "CMS122",
    "period_start": "2024-01-01",
    "period_end": "2024-12-31",
    "cdr_url": "https://example.com/fhir",
}


async def test_create_job_checks_measure_against_active_mcs(client, test_session, measure_present):
    """The pre-flight targets the active MCS."""
    await _make_active_mcs(test_session)

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 201
    assert measure_present.await_args.args[0] == "CMS122"
    assert measure_present.await_args.args[1] == "https://attendee-mcs.example.com/fhir"


async def test_create_job_snapshots_wipe_before_job_from_active_mcs(client, test_session, measure_present):
    """POST /jobs records the active MCS's wipe mode on the job (issue #392).

    Without this assertion the snapshot could be dropped from the route and
    nothing would fail: every job would silently fall back to the scoped wipe,
    including against a connection whose owner deliberately opted into the full
    wipe. The orchestrator tests build Job rows directly, so they cannot catch it.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.job import Job
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Dedicated Engine",
        mcs_url="https://dedicated.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        wipe_before_job=True,
    )
    test_session.add(cfg)
    await test_session.commit()

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)
    assert resp.status_code == 201

    job = (await test_session.execute(sa_select(Job).where(Job.id == resp.json()["id"]))).scalar_one()
    assert job.mcs_wipe_before_job is True


async def test_create_job_defaults_wipe_before_job_to_scoped(client, test_session, measure_present):
    """A job against a connection that did not opt in snapshots the safe mode."""
    from sqlalchemy import select as sa_select

    from app.models.job import Job

    await _make_active_mcs(test_session)  # created without wipe_before_job

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)
    assert resp.status_code == 201

    job = (await test_session.execute(sa_select(Job).where(Job.id == resp.json()["id"]))).scalar_one()
    assert job.mcs_wipe_before_job is False


async def test_create_job_preflight_timeout_is_bounded(client, test_session, measure_present):
    """The pre-flight is capped at 10s regardless of the connection's timeout.

    `request_timeout_seconds` is sized for measure evaluation (cap 1800s). Using
    it here would let one hung MCS hang an interactive POST /jobs for half an
    hour. The pre-flight is a `_summary=count` lookup; 10s is generous.
    """
    from app.routes.jobs import _PREFLIGHT_TIMEOUT_SECONDS

    assert _PREFLIGHT_TIMEOUT_SECONDS == 10

    await _make_active_mcs(test_session)  # request_timeout_seconds=45

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 201
    assert measure_present.await_args.kwargs["timeout"] == 10.0


async def test_create_job_preflight_honors_shorter_connection_timeout(client, test_session, measure_present):
    """A connection configured tighter than the ceiling still wins (it's a min())."""
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    test_session.add(
        MCSConfig(
            name="Impatient MCS",
            mcs_url="https://impatient-mcs.example.com/fhir",
            auth_type=AuthType.none,
            is_active=True,
            is_default=False,
            request_timeout_seconds=3,
        )
    )
    await test_session.commit()

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 201
    assert measure_present.await_args.kwargs["timeout"] == 3.0


async def test_create_job_measure_absent_returns_400(client, test_session, measure_present):
    """Measure missing on the active MCS → 400 naming the measure and the MCS."""
    from sqlalchemy import select

    from app.models.job import Job

    await _make_active_mcs(test_session)
    measure_present.return_value = False

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert "CMS122" in detail["issue"][0]["diagnostics"]
    assert "Attendee MCS" in detail["issue"][0]["diagnostics"]
    # No Job row was inserted.
    rows = (await test_session.execute(select(Job))).scalars().all()
    assert rows == []


async def test_create_job_mcs_unreachable_returns_502(client, test_session, measure_present):
    """Transport failure on the pre-flight → 502, NOT 400.

    A 400 would tell the user their measure doesn't exist when the real
    problem is that their MCS is down.
    """
    import httpx
    from sqlalchemy import select

    from app.models.job import Job

    await _make_active_mcs(test_session)
    measure_present.side_effect = httpx.ConnectError("refused")

    resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert "Attendee MCS" in detail["issue"][0]["diagnostics"]
    rows = (await test_session.execute(select(Job))).scalars().all()
    assert rows == []


async def test_create_job_preflight_runs_after_ssrf_rejection(client, measure_present):
    """An SSRF-rejected cdr_url still 400s without ever touching the MCS."""
    resp = await client.post(
        "/jobs",
        json={
            "measure_id": "CMS122",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "cdr_url": "https://169.254.169.254/fhir",
        },
    )
    assert resp.status_code == 400
    assert "SSRF protection" in resp.json()["detail"]["issue"][0]["diagnostics"]
    measure_present.assert_not_awaited()


async def test_create_job_credential_failure_returns_502(client, test_session, measure_present):
    """A SMART token failure on the pre-flight is a 502 naming the MCS, not a 500."""
    from sqlalchemy import select

    from app.models.job import Job

    await _make_active_mcs(test_session)

    with patch(
        "app.routes.jobs._build_auth_headers",
        new_callable=AsyncMock,
        side_effect=ValueError("token endpoint returned no access_token"),
    ):
        resp = await client.post("/jobs", json=_JOB_PAYLOAD)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert "Attendee MCS" in detail["issue"][0]["diagnostics"]
    measure_present.assert_not_awaited()
    rows = (await test_session.execute(select(Job))).scalars().all()
    assert rows == []


async def test_get_comparison_auth_failure_names_credentials(client, test_session):
    """A 401 from the engine says "rejected", not "could not reach" (issue #397).

    The engine answered — it refused. Calling that unreachable would hand the user
    another wrong diagnosis, which is the exact failure this endpoint's fix is about.
    """
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=httpx.Response(401, json={}, request=httpx.Request("GET", "http://x")))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 502, resp.text
    diagnostics = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "rejected" in diagnostics
    assert "credentials" in diagnostics


class TestJobWorkflowSelection:
    """POST /jobs workflow selection + $submit-data capability snapshot (spec:
    2026-08-21-deqm-submit-data-workflow)."""

    async def test_create_job_defaults_to_direct_load(self, client, measure_present):
        resp = await client.post(
            "/jobs",
            json={
                "measure_id": "M1",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["workflow"] == "direct_load"
        assert body["submit_data_mode"] is None

    async def test_create_job_rejects_unknown_workflow(self, client, measure_present):
        resp = await client.post(
            "/jobs",
            json={
                "measure_id": "M1",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "workflow": "carrier-pigeon",
            },
        )
        assert resp.status_code == 422

    async def test_deqm_job_records_probe_result(self, client, measure_present):
        with patch("app.routes.jobs.detect_submit_data_mode", new=AsyncMock(return_value="base-fallback")) as probe:
            resp = await client.post(
                "/jobs",
                json={
                    "measure_id": "M1",
                    "period_start": "2025-01-01",
                    "period_end": "2025-12-31",
                    "workflow": "deqm_submit_data",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["workflow"] == "deqm_submit_data"
        assert body["submit_data_mode"] == "base-fallback"
        probe.assert_awaited_once()

    async def test_direct_load_job_skips_probe(self, client, measure_present):
        with patch("app.routes.jobs.detect_submit_data_mode", new=AsyncMock()) as probe:
            resp = await client.post(
                "/jobs",
                json={
                    "measure_id": "M1",
                    "period_start": "2025-01-01",
                    "period_end": "2025-12-31",
                    "workflow": "direct_load",
                },
            )
        assert resp.status_code == 201
        probe.assert_not_awaited()
