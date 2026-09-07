"""Job orchestrator — the core $gather workflow.

Fetches patients from the CDR, pushes their data to the measure engine,
evaluates the measure, and stores results.
"""

import asyncio
import copy
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.dependencies import resolve_job_mcs_auth_headers
from app.models.config import CDRConfig
from app.models.job import Batch, BatchStatus, Job, JobStatus, MeasureResult
from app.services.fhir_client import (
    BatchQueryStrategy,
    FhirOperationError,
    _build_auth_headers,
    evaluate_measure,
    get_group_members,
    snapshot_evaluated_resources,
    wipe_patient_data,
    wipe_patients_by_id,
)
from app.services.fhir_errors import redact_outcome, sanitize_url
from app.services.validation import sanitize_error
from app.services.workflows import SubmissionWorkflow, TransferPhaseError, build_submission_workflow

logger = logging.getLogger(__name__)

LENNY_ERROR_EXT = "https://lenny.bellese.io/fhir/StructureDefinition/synthesized-error"

# HAPI-2788: HAPI echoes ValueSet URLs percent-encoded in its diagnostic message.
# sanitize_error() strips plain URLs (https?://...) but leaves percent-encoded ones intact,
# so we match both forms here and decode before surfacing to the user.
_HAPI_UNKNOWN_VS_RE = re.compile(
    r"HAPI-2788[^:]*:\s*Unknown ValueSet:\s*(\S+)",
    re.IGNORECASE,
)


def _extract_unknown_valueset_urls(error_messages: list[str]) -> list[str]:
    """If ALL messages are HAPI-2788 Unknown ValueSet errors, return sorted unique decoded URLs.

    Returns [] if any message does not match (mixed error types should use the generic message).
    """
    urls: set[str] = set()
    for msg in error_messages:
        m = _HAPI_UNKNOWN_VS_RE.search(msg)
        if not m:
            return []
        urls.add(urllib.parse.unquote(m.group(1)))
    return sorted(urls)


def _extract_populations(measure_report: dict[str, Any]) -> dict[str, bool]:
    """Parse a MeasureReport and return population boolean flags."""
    populations = {
        "initial_population": False,
        "denominator": False,
        "numerator": False,
        "denominator_exclusion": False,
        "numerator_exclusion": False,
    }
    code_map = {
        "initial-population": "initial_population",
        "denominator": "denominator",
        "numerator": "numerator",
        "denominator-exclusion": "denominator_exclusion",
        "numerator-exclusion": "numerator_exclusion",
    }
    for group in measure_report.get("group", []):
        for pop in group.get("population", []):
            code_coding = pop.get("code", {}).get("coding", [])
            for coding in code_coding:
                code = coding.get("code", "")
                if code in code_map:
                    count = pop.get("count", 0)
                    populations[code_map[code]] = count > 0
    return populations


def _extract_patient_name(patient_resource: dict[str, Any]) -> Optional[str]:
    """Extract a display name from a Patient FHIR resource."""
    for name_obj in patient_resource.get("name", []):
        parts = []
        given = name_obj.get("given", [])
        if given:
            parts.extend(given)
        family = name_obj.get("family")
        if family:
            parts.append(family)
        if parts:
            return " ".join(parts)
    return None


def _error_measure_report(
    patient_id: str,
    exc: Exception,
    upstream_outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a persisted per-patient error result for failed evaluations.

    When upstream_outcome is an OperationOutcome from the MCS, embed it directly
    (sanitized) with the synthesized error string attached as a FHIR Extension.
    Deep-copies to prevent cross-patient mutation when two patients share an OO.
    """
    if upstream_outcome and upstream_outcome.get("resourceType") == "OperationOutcome":
        oo = copy.deepcopy(redact_outcome(upstream_outcome))
        oo["subject"] = {"reference": f"Patient/{patient_id}"}
        oo.setdefault("extension", []).append({"url": LENNY_ERROR_EXT, "valueString": sanitize_error(exc)})
        return oo
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": sanitize_error(exc),
            }
        ],
        "subject": {"reference": f"Patient/{patient_id}"},
    }


async def _stop_or_delete_job(job_id: int) -> bool:
    """Return True when work should stop because the job was cancelled or deleted."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job:
            return True
        if job.delete_requested:
            await session.delete(job)
            await session.commit()
            return True
        return job.status == JobStatus.cancelled


async def run_job(job_id: int) -> None:
    """Execute the full $gather workflow for a job."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job:
            logger.error("Job not found", extra={"job_id": job_id})
            return
        if job.delete_requested:
            await session.delete(job)
            await session.commit()
            logger.info("Job deleted before start", extra={"job_id": job_id})
            return
        if job.status == JobStatus.cancelled:
            logger.info("Job already cancelled", extra={"job_id": job_id})
            return

        job.status = JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        # Resolve the MCS up front: the wipe below must target the same engine the
        # job will push to and evaluate against, not the env-var default. Pointing
        # the wipe at a different server would leave the real target's prior-run
        # data in place and silently contaminate this job's populations.
        mcs_url = await _get_mcs_url(job_id)
        mcs_auth_headers = await _get_mcs_auth_headers(job_id)

        # Step 2: Resolve CDR connection settings
        auth_headers = await _get_cdr_auth_headers(job_id)
        cdr_url = await _get_cdr_url(job_id)
        if await _stop_or_delete_job(job_id):
            return

        # Step 3: Fetch patients from CDR (optionally filtered by Group)
        async with async_session() as session:
            job_row = await session.get(Job, job_id)
            if not job_row:
                return
            group_id = job_row.group_id
            job_workflow = job_row.workflow
            job_submit_data_mode = job_row.submit_data_mode
            job_measure_id = job_row.measure_id
            job_period_start = job_row.period_start
            job_period_end = job_row.period_end

        if group_id:
            logger.info("Gathering patients from Group", extra={"job_id": job_id, "group_id": group_id})
            patients = await get_group_members(cdr_url, group_id, auth_headers)
        else:
            strategy = BatchQueryStrategy()
            logger.info("Gathering patients from CDR", extra={"job_id": job_id, "cdr_url": sanitize_url(cdr_url)})
            patients = await strategy.gather_patients(cdr_url, auth_headers)

        if not patients:
            if await _stop_or_delete_job(job_id):
                return
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job:
                    job.status = JobStatus.complete
                    job.total_patients = 0
                    job.completed_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info("No patients found, job complete", extra={"job_id": job_id})
            return

        # Step 4: Update total and create batches
        patient_map: dict[str, dict[str, Any]] = {p["id"]: p for p in patients}
        patient_ids = list(patient_map.keys())
        batch_size = settings.BATCH_SIZE

        # Build the submission workflow once per job, BEFORE the wipe below. For
        # DEQM this reads the measure canonical off the MCS; failure aborts the
        # job before any patient work — including before the wipe — so a
        # canonical-fetch failure no longer wipes the MCS and then fails
        # anyway. Everything this needs (mcs_url/auth, measure_id, period) was
        # already resolved above; it doesn't need patient_ids.
        workflow = await build_submission_workflow(
            workflow=job_workflow,
            job_id=job_id,
            measure_id=job_measure_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            submit_data_mode=job_submit_data_mode,
            period_start=job_period_start,
            period_end=job_period_end,
        )

        # Step 4a: Clear the prior run's data off the MCS (issue #392).
        #
        # This sits after the gather and before the push, not at the top of the
        # job, because the scoped wipe needs the patient IDs to scope to. Running
        # it after the push would delete the data this job just pushed.
        #
        # Consequence of the move: a job that gathers zero patients returns above
        # without wiping at all. Nothing is evaluated in that case either, so no
        # result is affected — but it does mean an empty job no longer doubles as
        # a way to clear the local engine.
        await _wipe_prior_run_data(
            job_id=job_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            patient_ids=patient_ids,
        )

        # Stage workflow prerequisites AFTER the wipe, never before. The wipe's
        # full-wipe branch deletes Organization, so the DEQM reporter has to be
        # (re)created on this side of it -- otherwise every MeasureReport in the
        # job references an Organization that was just deleted, and because
        # $submit-data is transaction-backed that fails each patient's whole
        # submission. No-op for direct_load.
        await workflow.ensure_target_prerequisites()

        # The cancellation check that already guarded the batch-creation block
        # below now also covers the wipe above — no second check needed.
        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return
            job.total_patients = len(patient_ids)
            batches_data: list[Batch] = []
            for i in range(0, len(patient_ids), batch_size):
                chunk = patient_ids[i : i + batch_size]
                batch = Batch(
                    job_id=job_id,
                    batch_number=len(batches_data) + 1,
                    patient_ids=chunk,
                    status=BatchStatus.pending,
                )
                session.add(batch)
                batches_data.append(batch)
            await session.commit()
            batch_ids = [b.id for b in batches_data]

        # Step 5: Process batches with concurrency control
        semaphore = asyncio.Semaphore(settings.MAX_WORKERS)

        # mcs_url / mcs_auth_headers were resolved before the wipe above so every
        # MCS interaction in this job targets one server with one set of credentials.

        async def process_batch(batch_id: int) -> None:
            async with semaphore:
                await _process_single_batch(
                    job_id=job_id,
                    batch_id=batch_id,
                    patient_map=patient_map,
                    cdr_url=cdr_url,
                    auth_headers=auth_headers,
                    mcs_url=mcs_url,
                    mcs_auth_headers=mcs_auth_headers,
                    workflow=workflow,
                )

        # Check for cancellation before starting
        if await _stop_or_delete_job(job_id):
            return

        await asyncio.gather(*[process_batch(bid) for bid in batch_ids])

        # Step 6: Finalize job
        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return
            if job.status == JobStatus.cancelled:
                return
            if job.total_patients and job.processed_patients == 0 and job.failed_patients > 0:
                job.status = JobStatus.failed
                # Aggregate over every phase that can fail a patient, not just
                # "evaluate" — a DEQM job where every patient fails at submit
                # was previously reported as an evaluation failure with no
                # diagnostic at all (F5).
                error_rows = (
                    await session.execute(
                        select(MeasureResult.error_phase, MeasureResult.populations).where(
                            MeasureResult.job_id == job_id,
                            MeasureResult.error_phase.in_(("evaluate", "submit", "gather")),
                        )
                    )
                ).all()
                phase_counts: dict[str, int] = {}
                for phase, _pop in error_rows:
                    if phase:
                        phase_counts[phase] = phase_counts.get(phase, 0) + 1
                dominant_phase = max(phase_counts, key=phase_counts.get) if phase_counts else "evaluate"
                patient_errors = [
                    pop["error_message"]
                    for phase, pop in error_rows
                    if phase == dominant_phase and isinstance(pop, dict) and pop.get("error_message")
                ]
                if dominant_phase == "evaluate":
                    # Keep the existing unknown-ValueSet special case working
                    # for the evaluate phase only.
                    vs_urls = _extract_unknown_valueset_urls(patient_errors) if patient_errors else []
                    if vs_urls:
                        vs_list = ", ".join(vs_urls)
                        job.error_message = (
                            f"All {job.failed_patients} patient evaluations failed: unknown ValueSet(s): {vs_list}"
                        )
                    else:
                        job.error_message = f"All {job.failed_patients} patient evaluations failed"
                else:
                    phase_word = {"submit": "submissions", "gather": "data gathers"}[dominant_phase]
                    if patient_errors:
                        job.error_message = (
                            f"All {job.failed_patients} patient {phase_word} failed: {patient_errors[0]}"
                        )
                    else:
                        job.error_message = f"All {job.failed_patients} patient {phase_word} failed"
            else:
                job.status = JobStatus.complete
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info("Job finalized", extra={"job_id": job_id})

    except Exception as exc:
        logger.exception("Job failed", extra={"job_id": job_id})
        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job:
                job.status = JobStatus.failed
                job.error_message = str(exc)[:2000]
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()


async def _get_cdr_auth_headers(job_id: int) -> dict[str, str]:
    """Resolve auth headers by reading live credentials from the referenced CDR config."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return {}
        if job.cdr_id is None:
            # No CDR config linked — either job was created without one (unauthenticated
            # direct URL) or the config was deleted after creation.  If auth type is
            # "none"/unset no credentials are needed; for auth-bearing types the
            # credentials are unrecoverable.
            if not job.cdr_auth_type or job.cdr_auth_type == "none":
                return {}
            raise RuntimeError(
                f"Job {job_id} has no cdr_id — CDR config was deleted after job creation. "
                "Cannot fetch auth credentials."
            )
        cfg = await session.get(CDRConfig, job.cdr_id)
        if cfg is None:
            raise RuntimeError(f"CDR config {job.cdr_id} referenced by job {job_id} no longer exists.")
        return await _build_auth_headers(cfg.auth_type, cfg.auth_credentials)


async def _get_cdr_url(job_id: int) -> str:
    """Resolve the CDR URL for a job."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job:
            return job.cdr_url
    return settings.DEFAULT_CDR_URL


async def _get_mcs_auth_headers(job_id: int) -> dict[str, str]:
    """Resolve auth headers by reading live credentials from the referenced MCS config.

    Mirrors `_get_cdr_auth_headers`: the job snapshots `mcs_id`, and credentials are
    read from the live config rather than duplicated onto the job row, so secrets
    live in exactly one place.

    The logic lives in `dependencies.resolve_job_mcs_auth_headers` so request
    handlers can run it on the request's own session (issue #397). This wrapper is
    the background-task entry point: it owns the session, the shared helper owns the
    rules.
    """
    async with async_session() as session:
        return await resolve_job_mcs_auth_headers(session, job_id)


async def _wipe_prior_run_data(
    *,
    job_id: int,
    mcs_url: str,
    mcs_auth_headers: dict[str, str],
    patient_ids: list[str],
) -> None:
    """Clear the previous run's data off the MCS before this job pushes (issue #392).

    Two modes, chosen by the job's `mcs_wipe_before_job` snapshot:

    - False (default for every user-created connection): delete only the patients
      this job is about to push. Safe on a shared server, and equivalent for
      correctness because evaluation is per-subject.
    - True (the seeded local engine, or an explicit opt-in): the historical
      unfiltered wipe of every patient on the target.

    The full-wipe branch logs at WARNING with the target URL. It is a destructive
    operation against a server Lenny may not own, and issue #392 was filed partly
    because the only trace it left was a routine INFO line.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        full_wipe = bool(job.mcs_wipe_before_job) if job else False

    if full_wipe:
        logger.warning(
            "Full patient-data wipe starting — deletes ALL patients on the target MCS",
            extra={
                "job_id": job_id,
                "mcs_url": sanitize_url(mcs_url),
                "scope": "all-patients",
            },
        )
        await wipe_patient_data(base_url=mcs_url, strict=False, auth_headers=mcs_auth_headers)
        return

    logger.info(
        "Scoped patient-data wipe starting — deletes only this job's patients",
        extra={
            "job_id": job_id,
            "mcs_url": sanitize_url(mcs_url),
            "scope": "job-patients",
            "patient_count": len(patient_ids),
        },
    )
    await wipe_patients_by_id(base_url=mcs_url, patient_ids=patient_ids, auth_headers=mcs_auth_headers)


async def _get_mcs_url(job_id: int) -> str:
    """Resolve the MCS URL for a job.

    Falls back to `settings.MEASURE_ENGINE_URL` when:
    - The job pre-dates the mcs_url snapshot column (legacy row), AND
    - The migration backfill couldn't run because MEASURE_ENGINE_URL was unset.

    Both conditions together mean Job.mcs_url is NULL; the env-var fallback
    keeps legacy jobs runnable, matching the historical behavior where every
    job called `settings.MEASURE_ENGINE_URL` directly.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job and job.mcs_url:
            return job.mcs_url
    return settings.MEASURE_ENGINE_URL


async def _process_single_batch(
    job_id: int,
    batch_id: int,
    patient_map: dict[str, dict[str, Any]],
    cdr_url: str,
    auth_headers: dict[str, str],
    mcs_url: str,
    mcs_auth_headers: dict[str, str] | None = None,
    *,
    workflow: SubmissionWorkflow,
) -> None:
    """Process a single batch in two phases.

    Phase 1 — TRANSFER: Delegate to `workflow.transfer_patient()` for each
    patient. `direct_load` gathers from the CDR and pushes a Bundle of PUTs
    straight to the measure engine; `deqm_submit_data` gathers via targeted
    `$data-requirements` queries and delivers via `Measure/$submit-data`
    instead. Either way, HAPI FHIR's synchronous indexing strategy
    (synchronization.strategy=sync) ensures resources are immediately
    searchable once the workflow's delivery step returns — no post-transfer
    wait needed.

    Phase 2 — EVALUATE: Call $evaluate-measure for each patient.  Because all
    patient data is already indexed, CQL evaluation sees the correct resources.
    """
    async with async_session() as session:
        batch = await session.get(Batch, batch_id)
        if not batch:
            return
        patient_ids: list[str] = batch.patient_ids  # type: ignore[assignment]
        batch.status = BatchStatus.running
        await session.commit()

    retry_count = 0

    while retry_count <= settings.MAX_RETRIES:
        try:
            processed = 0
            failed = 0
            if await _stop_or_delete_job(job_id):
                return

            # Read job params once
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return
                measure_id = job.measure_id
                period_start = job.period_start
                period_end = job.period_end

            # DEQM jobs always use DataRequirementsStrategy regardless of the
            # env-configured default (see workflows._acquisition_strategy) —
            # only direct_load's strategy is actually chosen by that setting.
            strategy_label = settings.PATIENT_DATA_STRATEGY if workflow.name == "direct_load" else "data_requirements"
            logger.info(
                "Using submission workflow",
                extra={
                    "workflow": workflow.name,
                    "strategy": strategy_label,
                    "job_id": job_id,
                    "batch_id": batch_id,
                },
            )

            # ----------------------------------------------------------
            # Phase 1: Gather this batch's patient data and deliver it to
            # the MCS via workflow.transfer_patient() — direct_load pushes a
            # Bundle of PUTs; deqm_submit_data POSTs a $submit-data envelope.
            # ----------------------------------------------------------
            # Track patients that FULLY failed gather so they are skipped in evaluate.
            # Partial-gather patients proceed to evaluate with available data (AT-2).
            # Per-patient exceptions MUST stay swallowed here — letting them escape
            # would trigger the outer batch-retry handler and re-push healthy patients.
            gather_failed_patients: set[str] = set()
            # Partial-gather: some resource types failed but data was pushed.
            # Mapped to error_details dict for annotation after evaluate succeeds.
            partial_gather_patients: dict[str, dict] = {}
            for patient_id in patient_ids:
                if await _stop_or_delete_job(job_id):
                    return

                try:
                    gather_result = await workflow.transfer_patient(cdr_url, patient_id, auth_headers)
                    logger.info(
                        f"Transferred {len(gather_result.resources)} resources for {patient_id[:8]}",
                        extra={"job_id": job_id, "patient_id": patient_id},
                    )

                    if gather_result.has_partial_failure:
                        # Partial gather — continue to evaluate with available data (AT-2).
                        # Record which types failed so we can annotate the result after evaluate.
                        failed_type_names = [f.resource_type for f in gather_result.failed_types]
                        succeeded_type_names = sorted(
                            {r.get("resourceType") for r in gather_result.resources if r.get("resourceType")}
                        )
                        partial_gather_patients[patient_id] = {
                            "operation": "gather",
                            "failed_types": failed_type_names,
                            "succeeded_types": succeeded_type_names,
                        }
                        logger.warning(
                            "Partial CDR gather — continuing evaluation with available data",
                            extra={
                                "job_id": job_id,
                                "patient_id": patient_id,
                                "failed_types": failed_type_names,
                            },
                        )

                except Exception as transfer_exc:
                    if isinstance(transfer_exc, TransferPhaseError):
                        error_phase = transfer_exc.phase
                        push_exc: Exception = transfer_exc.cause
                    else:
                        error_phase = "gather"
                        push_exc = transfer_exc
                    gather_failed_patients.add(patient_id)
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
                    sanitized_msg = sanitize_error(push_exc)
                    error_details: dict[str, Any] = {"operation": error_phase, "error": sanitized_msg}
                    if isinstance(push_exc, FhirOperationError):
                        error_details["url"] = push_exc.url
                        error_details["status_code"] = push_exc.status_code
                        error_details["latency_ms"] = push_exc.latency_ms
                        if push_exc.outcome:
                            error_details["raw_outcome"] = redact_outcome(push_exc.outcome.raw)
                    error_report = _error_measure_report(
                        patient_id,
                        push_exc,
                        push_exc.outcome.raw if isinstance(push_exc, FhirOperationError) and push_exc.outcome else None,
                    )
                    logger.warning(
                        "Failed to transfer patient data",
                        extra={
                            "job_id": job_id,
                            "batch_id": batch_id,
                            "patient_id": patient_id,
                            "error": sanitized_msg,
                            "error_phase": error_phase,
                        },
                    )
                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if existing_row:
                            existing_row.measure_report = error_report
                            existing_row.populations = {
                                "initial_population": False,
                                "denominator": False,
                                "numerator": False,
                                "denominator_exclusion": False,
                                "numerator_exclusion": False,
                                "error": True,
                                "error_message": sanitized_msg,
                                "error_phase": error_phase,
                            }
                            existing_row.error_details = error_details
                            existing_row.error_phase = error_phase
                        else:
                            result = MeasureResult(
                                job_id=job_id,
                                patient_id=patient_id,
                                patient_name=patient_name,
                                measure_report=error_report,
                                populations={
                                    "initial_population": False,
                                    "denominator": False,
                                    "numerator": False,
                                    "denominator_exclusion": False,
                                    "numerator_exclusion": False,
                                    "error": True,
                                    "error_message": sanitized_msg,
                                    "error_phase": error_phase,
                                },
                                error_details=error_details,
                                error_phase=error_phase,
                            )
                            session.add(result)
                        await session.commit()
                    failed += 1

            if await _stop_or_delete_job(job_id):
                return

            # ----------------------------------------------------------
            # Phase 2: Evaluate each patient
            # ----------------------------------------------------------
            for patient_id in patient_ids:
                if patient_id in gather_failed_patients:
                    continue  # Already persisted error row in Phase 1

                if await _stop_or_delete_job(job_id):
                    return

                try:
                    measure_report = await evaluate_measure(
                        measure_id,
                        patient_id,
                        period_start,
                        period_end,
                        measure_engine_url=mcs_url,
                        auth_headers=mcs_auth_headers,
                    )

                    populations = _extract_populations(measure_report)
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))

                    # Snapshot evaluatedResource references before the next job's
                    # wipe_patient_data() makes them irretrievable. See routes/results.py
                    # — the historical "kept on the measure engine until the NEXT job
                    # starts" contract is what this snapshot replaces.
                    #
                    # Column semantics: NULL = no snapshot was written (legacy row, or
                    # snapshot raised); list = snapshot succeeded ([] is a valid value
                    # meaning "snapshotted, no refs to resolve"). Coalesce the helper's
                    # None (no refs) to [] so the route can distinguish legacy rows from
                    # new rows without refs.
                    evaluated_resources_snapshot: list[dict] | None = None
                    try:
                        snapshot_result = await snapshot_evaluated_resources(measure_report, mcs_url, mcs_auth_headers)
                        evaluated_resources_snapshot = snapshot_result if snapshot_result is not None else []
                    except Exception as snap_exc:
                        logger.warning(
                            "snapshot_evaluated_resources_failed",
                            extra={"job_id": job_id, "patient_id": patient_id, "error": str(snap_exc)[:200]},
                        )

                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        gather_partial_details = partial_gather_patients.get(patient_id)
                        if existing_row:
                            existing_row.measure_report = measure_report
                            existing_row.populations = populations
                            existing_row.evaluated_resources = evaluated_resources_snapshot
                            existing_row.patient_name = patient_name
                            existing_row.error_details = gather_partial_details
                            existing_row.error_phase = "gather_partial" if gather_partial_details else None
                        else:
                            result = MeasureResult(
                                job_id=job_id,
                                patient_id=patient_id,
                                patient_name=patient_name,
                                measure_report=measure_report,
                                populations=populations,
                                evaluated_resources=evaluated_resources_snapshot,
                                error_details=gather_partial_details,
                                error_phase="gather_partial" if gather_partial_details else None,
                            )
                            session.add(result)
                        await session.commit()

                    processed += 1

                except Exception as patient_exc:
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
                    sanitized_error = sanitize_error(patient_exc)

                    upstream_outcome_raw: dict[str, Any] | None = None
                    eval_error_details: dict[str, Any] = {
                        "operation": "evaluate-measure",
                        "error": sanitized_error,
                        "error_phase": "evaluate",
                    }
                    if isinstance(patient_exc, FhirOperationError):
                        eval_error_details["url"] = patient_exc.url
                        eval_error_details["status_code"] = patient_exc.status_code
                        eval_error_details["latency_ms"] = patient_exc.latency_ms
                        if patient_exc.outcome:
                            upstream_outcome_raw = patient_exc.outcome.raw
                            eval_error_details["raw_outcome"] = redact_outcome(patient_exc.outcome.raw)

                    error_report = _error_measure_report(patient_id, patient_exc, upstream_outcome_raw)
                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if existing_row:
                            existing_row.measure_report = error_report
                            existing_row.populations = {
                                "initial_population": False,
                                "denominator": False,
                                "numerator": False,
                                "denominator_exclusion": False,
                                "numerator_exclusion": False,
                                "error": True,
                                "error_message": sanitized_error,
                                "error_phase": "evaluate",
                            }
                            existing_row.error_details = eval_error_details
                            existing_row.error_phase = "evaluate"
                        else:
                            result = MeasureResult(
                                job_id=job_id,
                                patient_id=patient_id,
                                patient_name=patient_name,
                                measure_report=error_report,
                                populations={
                                    "initial_population": False,
                                    "denominator": False,
                                    "numerator": False,
                                    "denominator_exclusion": False,
                                    "numerator_exclusion": False,
                                    "error": True,
                                    "error_message": sanitized_error,
                                    "error_phase": "evaluate",
                                },
                                error_details=eval_error_details,
                                error_phase="evaluate",
                            )
                            session.add(result)
                        await session.commit()

                    logger.warning(
                        "Failed to evaluate patient",
                        extra={
                            "job_id": job_id,
                            "batch_id": batch_id,
                            "patient_id": patient_id,
                            "error": sanitized_error,
                        },
                    )
                    failed += 1

            # Update batch and job counters
            if await _stop_or_delete_job(job_id):
                return
            async with async_session() as session:
                batch = await session.get(Batch, batch_id)
                if batch:
                    batch.status = BatchStatus.complete
                    batch.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                job = await session.get(Job, job_id)
                if job:
                    job.processed_patients = job.processed_patients + processed
                    job.failed_patients = job.failed_patients + failed
                    await session.commit()

            return  # Success — exit retry loop

        except Exception as batch_exc:
            retry_count += 1
            logger.warning(
                "Batch failed, retrying",
                extra={
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "retry": retry_count,
                    "error": str(batch_exc),
                },
            )
            if retry_count > settings.MAX_RETRIES:
                async with async_session() as session:
                    batch = await session.get(Batch, batch_id)
                    if batch:
                        batch.status = BatchStatus.failed
                        batch.retry_count = retry_count
                        batch.error_message = str(batch_exc)[:2000]
                        batch.completed_at = datetime.now(timezone.utc)
                        await session.commit()

                    job = await session.get(Job, job_id)
                    if job:
                        job.failed_patients = job.failed_patients + len(patient_ids)
                        await session.commit()
                return

            # Exponential backoff before retry
            await asyncio.sleep(2**retry_count)
