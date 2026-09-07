"""Job management endpoints."""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.config import settings
from app.db import get_session
from app.dependencies import (
    CDRContext,
    ConnectionContext,
    get_active_cdr,
    get_active_mcs,
    resolve_job_mcs_auth_headers,
)
from app.models.job import BatchStatus, Job, JobStatus, MeasureResult
from app.models.validation import ExpectedResult
from app.services.fhir_client import (
    _build_auth_headers,
    _validate_ssrf_url,
    detect_submit_data_mode,
    list_groups,
    measure_exists,
)
from app.services.fhir_errors import sanitize_url
from app.services.validation import _extract_population_counts, compare_populations, sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,256}$")
_MEASURE_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,256}$")

# Ceiling for the `measure_exists` pre-flight on POST /jobs. The connection's own
# `request_timeout_seconds` (up to 1800s) is sized for measure evaluation, not for
# a `_summary=count` lookup on an interactive request. Taken as a min() with the
# connection value so a deliberately short connection timeout still wins.
_PREFLIGHT_TIMEOUT_SECONDS = 10

_VALID_WORKFLOWS = {"direct_load", "deqm_submit_data"}


class JobCreate(BaseModel):
    measure_id: str
    measure_name: Optional[str] = None
    period_start: str
    period_end: str
    cdr_url: Optional[str] = None  # if omitted, use active CDR config or default
    group_id: Optional[str] = None  # if set, only evaluate patients in this FHIR Group
    workflow: str = "direct_load"

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, v: Optional[str]) -> Optional[str]:
        """Reject group_id values that could rewrite the CDR URL path."""
        if v is not None and not _GROUP_ID_RE.match(v):
            raise ValueError("group_id must be alphanumeric with hyphens, underscores, or dots only")
        return v

    @field_validator("measure_id")
    @classmethod
    def validate_measure_id(cls, v: str) -> str:
        """Reject measure_id values that could rewrite the CDR/MCS URL path."""
        if not _MEASURE_ID_RE.match(v):
            raise ValueError("measure_id must be alphanumeric with hyphens, underscores, or dots only")
        return v

    @field_validator("workflow")
    @classmethod
    def validate_workflow(cls, v: str) -> str:
        if v not in _VALID_WORKFLOWS:
            raise ValueError(f"workflow must be one of {sorted(_VALID_WORKFLOWS)}")
        return v


class JobResponse(BaseModel):
    id: int
    measure_id: str
    measure_name: Optional[str]
    period_start: str
    period_end: str
    cdr_url: str
    cdr_name: Optional[str] = None
    cdr_read_only: bool = False
    group_id: Optional[str]
    status: str
    total_patients: int
    processed_patients: int
    failed_patients: int
    total_batches: int = 0
    batches_completed: int = 0
    delete_requested: bool
    created_at: str
    completed_at: Optional[str]
    started_at: Optional[str] = None
    error_message: Optional[str]
    workflow: str = "direct_load"
    submit_data_mode: Optional[str] = None

    model_config = {"from_attributes": True}


class BatchResponse(BaseModel):
    id: int
    batch_number: int
    patient_ids: list[str]
    status: str
    retry_count: int
    error_message: Optional[str]
    created_at: str
    completed_at: Optional[str]

    model_config = {"from_attributes": True}


class JobDetailResponse(JobResponse):
    batches: list[BatchResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_to_response(job: Job) -> dict:
    batches = job.batches if job.batches is not None else []
    return {
        "id": job.id,
        "measure_id": job.measure_id,
        "measure_name": job.measure_name,
        "period_start": job.period_start,
        "period_end": job.period_end,
        "cdr_url": job.cdr_url,
        "cdr_name": job.cdr_name,
        "cdr_read_only": job.cdr_read_only,
        "group_id": job.group_id,
        "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
        "total_patients": job.total_patients,
        "processed_patients": job.processed_patients,
        "failed_patients": job.failed_patients,
        "total_batches": len(batches),
        "batches_completed": sum(1 for b in batches if b.status == BatchStatus.complete),
        "delete_requested": job.delete_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "error_message": job.error_message,
        "workflow": job.workflow,
        "submit_data_mode": job.submit_data_mode,
    }


def _batch_to_response(batch) -> dict:
    return {
        "id": batch.id,
        "batch_number": batch.batch_number,
        "patient_ids": batch.patient_ids,
        "status": batch.status.value if hasattr(batch.status, "value") else batch.status,
        "retry_count": batch.retry_count,
        "error_message": batch.error_message,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
    }


def _empty_comparison_response() -> dict:
    return {
        "has_expected": False,
        "matched": None,
        "total": None,
        "expected_total": 0,
        "actual_total": 0,
        "missing_results": 0,
        "unexpected_results": 0,
        "patients": [],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/groups")
async def get_groups(
    cdr: CDRContext = Depends(get_active_cdr),
) -> dict:
    """List FHIR Group resources from the CDR."""
    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)

    try:
        groups = await list_groups(cdr.cdr_url, auth_headers)
        return {"groups": groups}
    except Exception:
        logger.exception("Failed to fetch groups from CDR")
        raise HTTPException(
            status_code=502,
            detail="Cannot reach CDR to list groups. Check CDR connectivity in Settings.",
        )


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    body: JobCreate,
    session: AsyncSession = Depends(get_session),
    cdr: CDRContext = Depends(get_active_cdr),
    mcs: ConnectionContext = Depends(get_active_mcs),
) -> dict:
    """Create a new measure calculation job.

    The active CDR and MCS connections are snapshotted onto the Job row at
    creation time. Subsequent renders read the snapshot — never the live row
    — so renaming/deleting/deactivating either connection mid-flight or
    afterwards doesn't change what the job shows it ran against.
    """
    if body.cdr_url:
        try:
            _validate_ssrf_url(body.cdr_url, label="cdr_url")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "security", "diagnostics": str(exc)}],
                },
            )
    cdr_url = body.cdr_url or cdr.cdr_url

    # Confirm the measure actually lives on the MCS this job will run against,
    # before a Job row exists. Otherwise the job queues, starts, and fails deep
    # in the worker with an opaque error — the user's real mistake being that
    # they switched to an MCS that doesn't carry this measure.
    #
    # Deliberately NOT mcs.request_timeout_seconds: that is sized for measure
    # evaluation, which legitimately runs for many minutes (cap is 1800s), and
    # this is an interactive request. The pre-flight is a metadata count query;
    # if the MCS can't answer it in 10s, "unreachable → 502" is the right answer.
    #
    # `_build_auth_headers` is inside the `try` on purpose: SMART auth makes a
    # token-endpoint round trip, so credential resolution fails the same ways
    # the count query does and deserves the same 502 OperationOutcome rather
    # than a bare 500. (Unlike the measures routes, nothing in this `except`
    # chain special-cases a status code, so no mis-mapping is possible here.)
    preflight_timeout = float(min(mcs.request_timeout_seconds, _PREFLIGHT_TIMEOUT_SECONDS))
    try:
        mcs_auth_headers = await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
        found = await measure_exists(
            body.measure_id,
            mcs.mcs_url,
            auth_headers=mcs_auth_headers,
            timeout=preflight_timeout,
        )
    except Exception as exc:
        logger.warning(
            "Measure existence check failed",
            extra={"measure_id": body.measure_id, "mcs_id": mcs.id, "mcs_name": mcs.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (
                            f"Cannot reach measure calculation server '{mcs.name}' to verify "
                            f"measure '{body.measure_id}': {sanitize_error(exc)}"
                        ),
                    }
                ],
            },
        ) from exc
    if not found:
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": (
                            f"Measure '{body.measure_id}' does not exist on the active measure "
                            f"calculation server '{mcs.name}'. Upload it there, or switch to an "
                            f"MCS connection that has it."
                        ),
                    }
                ],
            },
        )

    # For DEQM jobs, decide the $submit-data wire format now and snapshot it.
    # The probe never raises (detect_submit_data_mode swallows errors into
    # base-fallback), so it cannot block creation; base-fallback renders as an
    # STU5-compliance warning in the UI from the moment the job appears.
    submit_data_mode: str | None = None
    if body.workflow == "deqm_submit_data":
        submit_data_mode = await detect_submit_data_mode(
            mcs_url=mcs.mcs_url,
            auth_headers=mcs_auth_headers,
            timeout=preflight_timeout,
        )

    job = Job(
        measure_id=body.measure_id,
        measure_name=body.measure_name,
        period_start=body.period_start,
        period_end=body.period_end,
        cdr_url=cdr_url,
        group_id=body.group_id,
        status=JobStatus.queued,
        cdr_name=cdr.name,
        cdr_read_only=cdr.is_read_only,
        cdr_auth_type=cdr.auth_type.value if cdr.auth_type else None,
        cdr_id=cdr.id if cdr.id else None,
        mcs_url=mcs.mcs_url,
        mcs_name=mcs.name,
        mcs_id=mcs.id if mcs.id else None,
        mcs_auth_type=mcs.auth_type.value if mcs.auth_type else None,
        mcs_wipe_before_job=mcs.wipe_before_job,
        workflow=body.workflow,
        submit_data_mode=submit_data_mode,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    logger.info(
        "Job created",
        extra={
            "job_id": job.id,
            "measure_id": job.measure_id,
            "cdr_id": job.cdr_id,
            "mcs_id": job.mcs_id,
            "mcs_url": job.mcs_url,
        },
    )
    return _job_to_response(job)


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all jobs, most recent first."""
    result = await session.execute(select(Job).order_by(Job.created_at.desc()))
    jobs = result.scalars().all()
    return [_job_to_response(j) for j in jobs]


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get job details including batch breakdown."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )
    resp = _job_to_response(job)
    resp["batches"] = [_batch_to_response(b) for b in job.batches]
    return resp


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cancel a running or queued job."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )

    if job.status not in (JobStatus.queued, JobStatus.running):
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": f"Job is already {job.status.value}, cannot cancel",
                    }
                ],
            },
        )

    job.status = JobStatus.cancelled
    job.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(job)

    logger.info("Job cancelled", extra={"job_id": job_id})
    return _job_to_response(job)


@router.delete("/{job_id}")
async def delete_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a job and its dependent batches/results."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )

    if job.status == JobStatus.running:
        job.delete_requested = True
        await session.commit()
        logger.info("Job delete requested", extra={"job_id": job_id})
        return JSONResponse(
            status_code=202,
            content={"id": job_id, "status": "delete_requested", "delete_requested": True},
        )

    if job.status == JobStatus.queued:
        job.status = JobStatus.cancelled
        job.delete_requested = True
        job.completed_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("Queued job delete requested", extra={"job_id": job_id})
        return JSONResponse(
            status_code=202,
            content={"id": job_id, "status": "delete_requested", "delete_requested": True},
        )

    await session.delete(job)
    await session.commit()
    logger.info("Job deleted", extra={"job_id": job_id})
    return Response(status_code=204)


@router.get("/{job_id}/measure-report")
async def get_job_measure_report(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a FHIR Bundle (collection) of individual MeasureReports for a job.

    Includes all patients whose populations["error"] is falsy — i.e. successful
    evaluations AND gather_partial patients (those have real MeasureReports from the
    engine; only their CDR push was partial). Excludes gather-failure and
    evaluate-failure patients whose measure_report is a synthetic OperationOutcome.

    Returns 404 if the job does not exist or has no results yet (consistent with
    the /results endpoint). Direct API calls on an in-progress job receive a
    partial bundle — intentional; no status gate is applied.

    Memory: up to ~500 patients x ~20 KB/report = ~10 MB per query (single load,
    no double-load due to noload() below). Revisit if cohort sizes grow to thousands.
    """
    job = await session.get(Job, job_id, options=[noload(Job.results), noload(Job.batches)])
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Job {job_id} not found"}],
            },
        )

    result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
    results = result.scalars().all()

    if not results:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {"severity": "error", "code": "not-found", "diagnostics": f"No results found for job {job_id}"}
                ],
            },
        )

    # populations is non-nullable (models/job.py) — no `or {}` needed.
    # Filter by populations["error"] (not measure_report resourceType) so that
    # gather_partial patients with real engine-produced reports are included.
    entries = [
        {"resource": mr.measure_report} for mr in results if mr.measure_report and not mr.populations.get("error")
    ]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "total": len(entries),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry": entries,
    }


@router.get("/{job_id}/comparison")
async def get_job_comparison(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Compare actual population counts against expected test case values."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Job {job_id} not found"}],
            },
        )

    # Resolve the measure against the MCS this job actually ran on, with that job's
    # credentials (issue #397). Reading settings.MEASURE_ENGINE_URL asked the local
    # container about a job that ran elsewhere, and sending no credentials meant an
    # authenticated remote MCS 401'd — which the bare `except` then swallowed.
    mcs_url = job.mcs_url or settings.MEASURE_ENGINE_URL
    measure_url = ""
    try:
        # Raises if the linked config now points at a different host than the job
        # snapshotted — refusing to send its credentials to the new one. Runs on the
        # request's session rather than opening its own.
        mcs_auth_headers = await resolve_job_mcs_auth_headers(session, job_id)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{mcs_url}/Measure/{job.measure_id}", headers=mcs_auth_headers)
        if resp.status_code == 200:
            measure_url = resp.json().get("url", "")
        elif resp.status_code == 404:
            # The engine answered and the measure is not there. That is a real
            # empty result, not an outage — see the 502 branch below.
            logger.info(
                "Measure not found on the job's MCS; comparison has no expected results",
                extra={"job_id": job_id, "measure_id": job.measure_id, "mcs_url": sanitize_url(mcs_url)},
            )
            return _empty_comparison_response()
        elif resp.status_code in (401, 403):
            # "Could not reach" would be wrong here — the engine answered and
            # refused. Naming credentials is the difference between a useful
            # message and another wrong diagnosis.
            raise RuntimeError(
                f"the measure engine rejected the request (HTTP {resp.status_code}). "
                "Check the credentials on the MCS connection this job ran against"
            )
        else:
            raise RuntimeError(f"measure lookup returned HTTP {resp.status_code}")
    except Exception as exc:
        # Distinguishing "could not ask" from "asked, nothing there" is the point.
        # This used to return 200 + empty, which the UI renders as "No expected
        # results available — load a connectathon bundle", sending the user to load
        # data they already have when the real fault was auth or connectivity.
        logger.warning(
            "Could not resolve measure URL for comparison",
            extra={
                "job_id": job_id,
                "measure_id": job.measure_id,
                "mcs_url": sanitize_url(mcs_url),
                "error": sanitize_error(exc),
            },
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (
                            f"Could not resolve measure '{job.measure_id}' on the measure engine "
                            f"this job ran against, so comparison is unavailable: {sanitize_error(exc)}"
                        ),
                    }
                ],
            },
        )

    if not measure_url:
        return _empty_comparison_response()

    exp_result = await session.execute(
        select(ExpectedResult).where(
            ExpectedResult.measure_url == measure_url,
            ExpectedResult.period_start == job.period_start,
            ExpectedResult.period_end == job.period_end,
        )
    )
    expected_by_patient = {er.patient_ref: er for er in exp_result.scalars().all()}

    if not expected_by_patient:
        return _empty_comparison_response()

    result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
    actual_by_patient = {mr.patient_id: mr for mr in result.scalars().all()}

    patients_list = []
    matched_count = 0

    for patient_id, expected in sorted(expected_by_patient.items()):
        mr = actual_by_patient.get(patient_id)
        if not mr:
            patients_list.append(
                {
                    "subject_reference": f"Patient/{patient_id}",
                    "match": False,
                    "mismatches": ["missing-result"],
                    "expected": expected.expected_populations,
                    "actual": {},
                }
            )
            continue

        actual_counts = _extract_population_counts(mr.measure_report)
        passed, mismatches = compare_populations(expected.expected_populations, actual_counts)
        if passed:
            matched_count += 1

        patients_list.append(
            {
                "subject_reference": f"Patient/{mr.patient_id}",
                "match": passed,
                "mismatches": mismatches,
                "expected": expected.expected_populations,
                "actual": actual_counts,
            }
        )

    unexpected_result_count = len(set(actual_by_patient) - set(expected_by_patient))

    return {
        "has_expected": True,
        "matched": matched_count,
        "total": len(patients_list),
        "expected_total": len(expected_by_patient),
        "actual_total": len(actual_by_patient),
        "missing_results": len(expected_by_patient) - len(set(expected_by_patient) & set(actual_by_patient)),
        "unexpected_results": unexpected_result_count,
        "patients": patients_list,
    }
