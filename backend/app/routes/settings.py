"""Settings management endpoints — CDR connection routes via the connection
factory, plus admin and measure-engine routes that don't fit the connection
shape.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session, get_session
from app.dependencies import ConnectionContext, get_active_mcs
from app.models.admin_operation import AdminOperation, AdminOperationKind, AdminOperationStatus
from app.models.app_setting import AppSetting
from app.models.config import CDRConfig
from app.models.connection_base import ConnectionKind
from app.models.job import Job, JobStatus
from app.models.mcs_config import MCSConfig
from app.routes.connection_factory import make_connection_router
from app.services.bundle_loader import load_connectathon_bundles
from app.services.fhir_client import (
    _build_auth_headers,
    probe_mcs_data_requirements,
    wipe_measure_definitions,
    wipe_patient_data,
)
from app.services.fhir_errors import (
    HINT_BY_STATUS,
    FhirOperationError,
    build_error_envelope,
    hint_for_network_exception,
    sanitize_url,
)
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])

# Hard cap on `request_timeout_seconds` to prevent timeout-as-DoS-vector. 1800s
# (30 min) covers the slowest legitimate measure-evaluation runs we've seen at
# the connectathon while still bounding worst-case worker hold time.
_MAX_REQUEST_TIMEOUT_SECONDS = 1800


# ---------------------------------------------------------------------------
# CDR Schemas
# ---------------------------------------------------------------------------


class CDRConnectionResponse(BaseModel):
    id: int
    name: str
    cdr_url: str
    auth_type: str
    is_active: bool
    is_default: bool
    is_read_only: bool
    request_timeout_seconds: int
    max_bundle_entries: int | None = None

    model_config = {"from_attributes": True}


class CDRConnectionCreate(BaseModel):
    name: str
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None
    is_read_only: bool = False
    request_timeout_seconds: int = Field(default=30, ge=1, le=_MAX_REQUEST_TIMEOUT_SECONDS)
    max_bundle_entries: int | None = Field(default=None, ge=1)


class TestConnectionRequest(BaseModel):
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


# ---------------------------------------------------------------------------
# MCS Schemas
# ---------------------------------------------------------------------------


class MCSConnectionResponse(BaseModel):
    id: int
    name: str
    mcs_url: str
    auth_type: str
    is_active: bool
    is_default: bool
    is_read_only: bool
    wipe_before_job: bool
    request_timeout_seconds: int

    model_config = {"from_attributes": True}


class MCSConnectionCreate(BaseModel):
    name: str
    mcs_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None
    is_read_only: bool = False
    # Issue #392: defaults to False so adding a shared server and running a job
    # cannot delete other people's patients. Opting in is a deliberate act.
    wipe_before_job: bool = False
    request_timeout_seconds: int = Field(default=30, ge=1, le=_MAX_REQUEST_TIMEOUT_SECONDS)


class MCSTestConnectionRequest(BaseModel):
    mcs_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


# ---------------------------------------------------------------------------
# CDR connection routes — delegated to the factory
# ---------------------------------------------------------------------------

router.include_router(
    make_connection_router(
        model=CDRConfig,
        response_schema=CDRConnectionResponse,
        create_schema=CDRConnectionCreate,
        test_request_schema=TestConnectionRequest,
        prefix="/connections",
        kind=ConnectionKind.cdr,
        url_field="cdr_url",
        default_name="Local CDR",
        job_fk_column=Job.cdr_id,
        audit_logger=logger,
    )
)


# ---------------------------------------------------------------------------
# MCS connection routes — same factory, different model+schemas
# ---------------------------------------------------------------------------

router.include_router(
    make_connection_router(
        model=MCSConfig,
        response_schema=MCSConnectionResponse,
        create_schema=MCSConnectionCreate,
        test_request_schema=MCSTestConnectionRequest,
        prefix="/mcs-connections",
        kind=ConnectionKind.mcs,
        url_field="mcs_url",
        default_name="Local Measure Engine",
        # Blocks deleting an MCS connection while a job still references it.
        # Without this, ON DELETE SET NULL nulls the queued job's mcs_id and the
        # job runs unauthenticated against its snapshotted mcs_url.
        job_fk_column=Job.mcs_id,
        audit_logger=logger,
    )
)


# ---------------------------------------------------------------------------
# MCS deep-probe — Verify with sample evaluate
# ---------------------------------------------------------------------------


@router.post("/mcs-connections/{connection_id}/probe")
async def probe_mcs_connection(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Deep-probe an MCS connection by exercising `$data-requirements`.

    Stricter than test-connection (which only fetches /metadata): this picks
    a measure on the MCS and confirms the engine can resolve its Library +
    ValueSets. Returns success summary on green; raises HTTPException with
    the standard FHIR error envelope on any failure.
    """
    cfg = await session.get(MCSConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="MCS connection not found")

    try:
        return await probe_mcs_data_requirements(
            mcs_url=cfg.mcs_url,
            auth_type=cfg.auth_type,
            auth_credentials=cfg.auth_credentials,
        )
    except FhirOperationError as exc:
        logger.warning(
            "mcs probe failed",
            extra={
                "mcs_url": sanitize_url(cfg.mcs_url),
                "status_code": exc.status_code,
                "error": sanitize_error(exc),
            },
        )
        if exc.status_code is not None:
            hint = HINT_BY_STATUS.get(exc.status_code)
        else:
            hint = hint_for_network_exception(exc.__cause__) if exc.__cause__ else None
        # Empty-MCS path: status 200 with a synthesized OperationOutcome.
        # Surface as 200 + envelope so the frontend can render the warning
        # without treating it as a hard failure.
        if exc.status_code == 200 and exc.outcome is not None:
            return {
                "status": "warning",
                "outcome": exc.outcome.raw,
                "url": sanitize_url(exc.url),
            }
        http_status = exc.status_code if exc.status_code and exc.status_code >= 400 else 502
        raise HTTPException(
            status_code=http_status,
            detail=build_error_envelope(
                operation="probe-data-requirements",
                url=exc.url,
                status_code=exc.status_code,
                outcome=exc.outcome,
                latency_ms=exc.latency_ms,
                hint=hint,
            ),
        )
    except ValueError as exc:
        # SSRF rejection from _validate_ssrf_url
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "security", "diagnostics": sanitize_error(exc)}],
            },
        )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

_ADMIN_DEFAULTS: dict[str, str] = {
    "validation_enabled": "false",
    "comparison_enabled": "false",
    # Gates ONLY `POST /api/groups/<id>/$evaluate`, which is parked with no UI.
    # It no longer gates Group *listing*: since #404 that is the always-on
    # Patients module, because a participant surveying an unfamiliar CDR must
    # not have to find an admin flag before they can see its cohorts.
    "groups_enabled": "false",
}


async def _get_setting(session: AsyncSession, key: str) -> str:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else _ADMIN_DEFAULTS[key]


@router.get("/admin")
async def get_admin_settings(session: AsyncSession = Depends(get_session)) -> dict:
    """Return current admin settings."""
    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
        "comparison_enabled": (await _get_setting(session, "comparison_enabled")) == "true",
        "groups_enabled": (await _get_setting(session, "groups_enabled")) == "true",
    }


class AdminSettingsUpdate(BaseModel):
    validation_enabled: bool | None = None
    comparison_enabled: bool | None = None
    groups_enabled: bool | None = None


@router.put("/admin")
async def update_admin_settings(
    body: AdminSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Persist admin settings."""
    updates: dict[str, str] = {}
    if body.validation_enabled is not None:
        updates["validation_enabled"] = "true" if body.validation_enabled else "false"
    if body.comparison_enabled is not None:
        updates["comparison_enabled"] = "true" if body.comparison_enabled else "false"
    if body.groups_enabled is not None:
        updates["groups_enabled"] = "true" if body.groups_enabled else "false"

    for key, value in updates.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    await session.commit()

    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
        "comparison_enabled": (await _get_setting(session, "comparison_enabled")) == "true",
        "groups_enabled": (await _get_setting(session, "groups_enabled")) == "true",
    }


@router.post("/admin/wipe-measure-engine", status_code=200)
async def wipe_measure_engine(mcs: ConnectionContext = Depends(get_active_mcs)) -> dict:
    """Delete all measure-definition resources (Library, Measure, ValueSet, CodeSystem, ConceptMap)
    from the active MCS connection's measure engine.

    Recovers from JVM/H2 state corruption that causes CQL compilation failures (issue #238).
    The engine is automatically re-seeded on the next job run via bundle_loader.

    Targets the active MCS rather than `settings.MEASURE_ENGINE_URL` (issue #397):
    an admin connected to a remote MCS used to wipe Lenny's local container and get
    a success response, while the server they were looking at was untouched.
    Refuses on a read-only connection — this is the most destructive control in the
    admin UI, and the fix that pointed it at the active MCS is what made a guard
    necessary.
    """
    if mcs.is_read_only:
        raise HTTPException(
            status_code=403,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "forbidden",
                        "diagnostics": (
                            f"The active measure engine connection '{mcs.name}' is read-only. "
                            "Wiping measure definitions would delete data on a server Lenny "
                            "does not own. Switch to a writable connection first."
                        ),
                    }
                ],
            },
        )

    try:
        # Inside the try: resolving credentials can fail on its own (a SMART token
        # endpoint being unreachable, for instance). Outside, that surfaced as an
        # unsanitized 500 instead of the 502 OperationOutcome below.
        auth_headers = await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
        await wipe_measure_definitions(base_url=mcs.mcs_url, auth_headers=auth_headers)
    except Exception as exc:
        logger.error("Measure engine wipe failed", extra={"error": sanitize_error(exc)})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "exception", "diagnostics": sanitize_error(exc)}],
            },
        )
    logger.info("Measure engine definitions wiped via admin endpoint")
    return {"status": "ok", "message": "Measure engine definitions wiped. Engine will re-seed on next job run."}


# ---------------------------------------------------------------------------
# Factory Reset + Reseed
# ---------------------------------------------------------------------------


class FactoryResetRequest(BaseModel):
    include_cdr: bool = True
    include_measure_engine: bool = True
    include_app_db: bool = True


class AdminOperationResponse(BaseModel):
    operation_id: int
    status: str
    kind: str
    scopes: dict | None = None
    steps: list | None = None
    error: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


async def _poll_zero_counts(
    base_url: str,
    resource_types: list[str],
    step_name: str,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """Poll until all given resource types return total=0 (or timeout after 60s)."""
    import httpx as _httpx

    deadline = asyncio.get_event_loop().time() + 60
    async with _httpx.AsyncClient(timeout=10.0) as client:
        for rt in resource_types:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(f"{base_url}/{rt}?_summary=count", headers=auth_headers or {})
                    if resp.status_code == 200:
                        total = resp.json().get("total", 1)
                        if total == 0:
                            break
                except Exception:
                    pass
                await asyncio.sleep(5)
            else:
                logger.warning("Poll-zero timeout for %s on %s (%s)", rt, base_url, step_name)


# Recorded as the step's `error` when a read-only connection is skipped (issue #397).
# Skipping rather than raising is deliberate: factory reset is a multi-step
# background operation, so aborting would leave the remaining steps (notably
# include_app_db) undone and strand the operation in `failed` — a worse outcome
# than declining the one step whose target Lenny does not own.
_READ_ONLY_SKIP = (
    "Skipped: the active {kind} connection '{name}' is read-only. "
    "Lenny will not delete data on a server it does not own. "
    "Switch to a writable connection and re-run if this was intended."
)


async def _run_factory_reset(operation_id: int, request: FactoryResetRequest) -> None:
    """Background task: execute factory reset steps and update the AdminOperation row."""
    steps: list[dict] = []

    async def _record(step: str, status: str, error: str | None = None) -> None:
        steps.append({"step": step, "status": status, "error": error})
        async with async_session() as s:
            op = await s.get(AdminOperation, operation_id)
            if op:
                op.steps_json = list(steps)
                await s.commit()

    async with async_session() as session:
        op = await session.get(AdminOperation, operation_id)
        if op:
            op.status = AdminOperationStatus.running
            await session.commit()

    try:
        if request.include_cdr:
            # Resolve active CDR URL from cdr_configs.is_active=True
            async with async_session() as session:
                result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
                active_cdr = result.scalar_one_or_none()
                active_cdr_url = active_cdr.cdr_url if active_cdr else settings.DEFAULT_CDR_URL
                cdr_read_only = bool(active_cdr.is_read_only) if active_cdr else False
                # The active CDR is routinely a remote authenticated connection.
                # Without credentials the wipe 401s, and wipe_patient_data now
                # refuses to report success on an unauthorized wipe.
                cdr_auth_headers = (
                    await _build_auth_headers(active_cdr.auth_type, active_cdr.auth_credentials) if active_cdr else {}
                )

            if cdr_read_only:
                await _record(
                    "wipe_cdr",
                    "skipped",
                    # `name` is nullable on the connection mixin, so fall back to a
                    # label rather than rendering "connection 'None' is read-only".
                    _READ_ONLY_SKIP.format(kind="CDR", name=active_cdr.name or "the active CDR"),
                )
            else:
                await wipe_patient_data(base_url=active_cdr_url, strict=False, auth_headers=cdr_auth_headers)
                await _poll_zero_counts(active_cdr_url, ["Patient", "Encounter"], "cdr_wipe", cdr_auth_headers)
                await _record("wipe_cdr", "succeeded")

        if request.include_measure_engine:
            # Resolve the active MCS the same way the CDR block above does. A
            # background task cannot use Depends(get_active_mcs), and reading
            # settings.MEASURE_ENGINE_URL here wiped Lenny's local container while
            # the admin was looking at a remote server (issue #397).
            async with async_session() as session:
                mcs_result = await session.execute(select(MCSConfig).where(MCSConfig.is_active.is_(True)).limit(1))
                active_mcs = mcs_result.scalar_one_or_none()
                active_mcs_url = active_mcs.mcs_url if active_mcs else settings.MEASURE_ENGINE_URL
                mcs_read_only = bool(active_mcs.is_read_only) if active_mcs else False
                mcs_auth_headers = (
                    await _build_auth_headers(active_mcs.auth_type, active_mcs.auth_credentials) if active_mcs else {}
                )

            if mcs_read_only:
                await _record(
                    "wipe_measure_engine",
                    "skipped",
                    _READ_ONLY_SKIP.format(kind="measure engine", name=active_mcs.name or "the active measure engine"),
                )
            else:
                await wipe_measure_definitions(base_url=active_mcs_url, auth_headers=mcs_auth_headers)
                await wipe_patient_data(base_url=active_mcs_url, strict=False, auth_headers=mcs_auth_headers)
                await _poll_zero_counts(active_mcs_url, ["Patient", "Measure"], "me_wipe", mcs_auth_headers)
                await _record("wipe_measure_engine", "succeeded")

        if request.include_app_db:
            async with async_session() as db_session:
                for table in ("measure_results", "jobs", "validation_results", "validation_runs", "expected_results"):
                    await db_session.execute(sa_text(f"DELETE FROM {table}"))
                await db_session.commit()
            await _record("wipe_app_db", "succeeded")

        async with async_session() as session:
            op = await session.get(AdminOperation, operation_id)
            if op:
                op.status = AdminOperationStatus.succeeded
                op.completed_at = datetime.now(timezone.utc)
                op.steps_json = list(steps)
                await session.commit()
        logger.info("Factory reset completed", extra={"operation_id": operation_id})

    except Exception as exc:
        error_msg = sanitize_error(exc)
        await _record("error", "failed", error=error_msg)
        async with async_session() as session:
            op = await session.get(AdminOperation, operation_id)
            if op:
                op.status = AdminOperationStatus.failed
                op.error = error_msg
                op.completed_at = datetime.now(timezone.utc)
                await session.commit()
        logger.error("Factory reset failed", extra={"operation_id": operation_id, "error": error_msg})


async def _run_reseed(operation_id: int) -> None:
    """Background task: reseed connectathon bundles and update the AdminOperation row."""
    async with async_session() as session:
        op = await session.get(AdminOperation, operation_id)
        if op:
            op.status = AdminOperationStatus.running
            await session.commit()

    try:
        summary = await load_connectathon_bundles()
        async with async_session() as session:
            op = await session.get(AdminOperation, operation_id)
            if op:
                op.status = AdminOperationStatus.succeeded
                op.completed_at = datetime.now(timezone.utc)
                op.steps_json = [{"step": "reseed_bundles", "status": "succeeded", "summary": summary}]
                await session.commit()
        logger.info("Reseed completed", extra={"operation_id": operation_id, **summary})

    except Exception as exc:
        error_msg = sanitize_error(exc)
        async with async_session() as session:
            op = await session.get(AdminOperation, operation_id)
            if op:
                op.status = AdminOperationStatus.failed
                op.error = error_msg
                op.completed_at = datetime.now(timezone.utc)
                await session.commit()
        logger.error("Reseed failed", extra={"operation_id": operation_id, "error": error_msg})


@router.post("/admin/factory-reset", status_code=202)
async def factory_reset(
    request: FactoryResetRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Wipe CDR patient data, measure-engine data, and/or app DB records.

    Returns 202 Accepted with an operation_id to poll via GET /admin/operations/{id}.
    Returns 409 if a job is currently running (cancel or wait first).
    """
    running_job = await session.execute(select(Job.id).where(Job.status == JobStatus.running).limit(1))
    if running_job.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="A job is currently running. Cancel or wait for it to complete before resetting.",
        )

    scopes = {
        "include_cdr": request.include_cdr,
        "include_measure_engine": request.include_measure_engine,
        "include_app_db": request.include_app_db,
    }
    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        scopes_json=scopes,
    )
    session.add(op)
    await session.flush()
    operation_id = op.id
    await session.commit()

    asyncio.create_task(_run_factory_reset(operation_id, request))

    logger.info("Factory reset accepted", extra={"operation_id": operation_id, **scopes})
    return {"status": "accepted", "operation_id": operation_id, "scopes": scopes}


@router.post("/admin/reseed-bundles", status_code=202)
async def reseed_bundles(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reload connectathon bundles into both FHIR servers (idempotent).

    Returns 202 Accepted with an operation_id to poll via GET /admin/operations/{id}.
    """
    op = AdminOperation(
        kind=AdminOperationKind.reseed_bundles,
        status=AdminOperationStatus.pending,
    )
    session.add(op)
    await session.flush()
    operation_id = op.id
    await session.commit()

    asyncio.create_task(_run_reseed(operation_id))

    logger.info("Reseed accepted", extra={"operation_id": operation_id})
    return {"status": "accepted", "operation_id": operation_id}


@router.get("/admin/operations/{operation_id}", response_model=AdminOperationResponse)
async def get_admin_operation(
    operation_id: int,
    session: AsyncSession = Depends(get_session),
) -> AdminOperationResponse:
    """Poll an admin operation by ID."""
    op = await session.get(AdminOperation, operation_id)
    if op is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return AdminOperationResponse(
        operation_id=op.id,
        status=op.status.value,
        kind=op.kind.value,
        scopes=op.scopes_json,
        steps=op.steps_json,
        error=op.error,
        started_at=op.started_at,
        completed_at=op.completed_at,
    )
