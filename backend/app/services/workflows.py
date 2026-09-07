"""Per-job data submission workflows (spec: 2026-08-21-deqm-submit-data-workflow).

A SubmissionWorkflow owns phase 1 of a job for one patient: gather from the
CDR, deliver to the MCS. The orchestrator picks the concrete class from
Job.workflow and calls transfer_patient(); phase 2 ($evaluate-measure) is
identical for every workflow and stays in the orchestrator.
"""

import abc
import logging
from datetime import datetime, timezone

from app.config import settings
from app.services.deqm import (
    LENNY_REPORTER_ORG,
    build_base_parameters,
    build_data_exchange_measure_report,
    build_stu5_parameters,
)
from app.services.fhir_client import (
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    BatchQueryStrategy,
    DataAcquisitionStrategy,
    DataRequirementsStrategy,
    GatherResult,
    get_measure_canonical,
    push_resources,
    submit_data,
)
from app.services.fhir_errors import FhirOperationError

# Capability-mismatch signals only: a server that advertises $deqm-submit-data
# but doesn't actually implement the type-level POST commonly answers with one
# of these. 401/403 are deliberately excluded — those are auth failures, not
# a capability mismatch, and must not be masked as a silent downgrade.
# 429/5xx are deliberately excluded too — those are transient/overload
# signals, not "this operation doesn't exist here", and downgrading on them
# would paper over a retry-able failure as a permanent capability verdict.
_DOWNGRADE_STATUS_CODES = {400, 404, 405, 501}

logger = logging.getLogger(__name__)


class TransferPhaseError(Exception):
    """A transfer failed; `phase` says which half, for MeasureResult.error_phase.

    direct_load labels both halves "gather" — the historical behavior, kept so
    existing dashboards/tests keep meaning the same thing. deqm_submit_data
    labels delivery failures "submit".
    """

    def __init__(self, phase: str, cause: Exception):
        super().__init__(str(cause))
        self.phase = phase
        self.cause = cause


def _acquisition_strategy(
    measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None = None
) -> DataAcquisitionStrategy:
    """The env-configured CDR acquisition strategy (moved from orchestrator).

    `mcs_url`/`mcs_auth_headers` are threaded to DataRequirementsStrategy so
    `$data-requirements` asks the job's own measure engine (issue #397).
    BatchQueryStrategy ignores them — it only talks to the CDR.
    """
    if settings.PATIENT_DATA_STRATEGY == "data_requirements":
        return DataRequirementsStrategy(measure_id, mcs_url, mcs_auth_headers)
    return BatchQueryStrategy()


class SubmissionWorkflow(abc.ABC):
    """Gathers one patient's data from the CDR and delivers it to the MCS."""

    name: str

    async def ensure_target_prerequisites(self) -> None:
        """Write any job-scoped resources the workflow needs on the MCS.

        Called by the orchestrator AFTER `_wipe_prior_run_data` -- which is the
        entire reason this is separate from `build_submission_workflow`. Build
        runs BEFORE the wipe on purpose, so a canonical-fetch failure aborts
        without wiping anything; but that ordering means anything build *writes*
        is deleted by the wipe moments later. `wipe_patient_data`'s full-wipe
        list includes "Organization", so the DEQM reporter created at build time
        was being removed before a single patient was submitted.

        Default is a no-op: direct_load needs nothing staged.
        """
        return None

    @abc.abstractmethod
    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        """Transfer one patient's data; return the GatherResult for
        partial-failure bookkeeping. Raises TransferPhaseError on failure."""
        ...


class DirectLoadWorkflow(SubmissionWorkflow):
    """Today's behavior: env-configured gather, then a batch Bundle of PUTs."""

    name = "direct_load"

    def __init__(self, measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None = None):
        self._strategy = _acquisition_strategy(measure_id, mcs_url, mcs_auth_headers)
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
            if gather.resources:
                await push_resources(
                    gather.resources,
                    target_url=self._mcs_url,
                    auth_headers=self._mcs_auth_headers,
                )
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc
        return gather


class DeqmSubmitDataWorkflow(SubmissionWorkflow):
    """DEQM data exchange: targeted queries, then Measure/$submit-data."""

    name = "deqm_submit_data"

    def __init__(
        self,
        *,
        job_id: int,
        measure_id: str,
        mcs_url: str,
        mcs_auth_headers: dict[str, str] | None,
        measure_canonical: str,
        period_start: str,
        period_end: str,
        mode: str,
    ):
        # Targeted queries are part of the DEQM workflow by design, independent
        # of the env-configured default strategy.
        self._strategy = DataRequirementsStrategy(measure_id, mcs_url, mcs_auth_headers)
        self._job_id = job_id
        self._measure_id = measure_id
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers
        self._measure_canonical = measure_canonical
        self._period_start = period_start
        self._period_end = period_end
        self._mode = mode

    async def ensure_target_prerequisites(self) -> None:
        """Store the shared reporter Organization once, after the wipe.

        DEQM requires MeasureReport.reporter 1..1 and every patient in the job
        references the same client-assigned Organization/lenny-reporter. Sending
        it inline per patient made concurrent batches upsert one id at once,
        which HAPI answered with ResourceVersionConflictException, failing 100%
        of patients; storing it once server-side and referencing it is the fix.

        This must run AFTER `_wipe_prior_run_data`, or the full-wipe branch
        deletes it and every submission then carries a dangling reporter
        reference -- which, because $submit-data is transaction-backed, fails
        that patient's whole submission.

        Raising here fails the job fast with one clear error rather than every
        patient failing later for the same reason.
        """
        try:
            await push_resources(
                [dict(LENNY_REPORTER_ORG)],
                target_url=self._mcs_url,
                auth_headers=self._mcs_auth_headers,
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to store reporter Organization '{LENNY_REPORTER_ORG['id']}' on the MCS "
                f"for job {self._job_id}: {exc}"
            ) from exc

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc

        # Filter once, and derive BOTH the MeasureReport (evaluatedResource)
        # and the submitted Parameters from the SAME filtered list. Without
        # this, an id-less resource is silently excluded from
        # evaluatedResource (build_data_exchange_measure_report has its own
        # filter) but still shipped as a `resource` parameter — the
        # MeasureReport and the payload disagree, and under HAPI's
        # transaction semantics one bad entry can 400 the whole patient.
        filtered_resources = [r for r in gather.resources if "resourceType" in r and "id" in r]
        measure_report = build_data_exchange_measure_report(
            job_id=self._job_id,
            patient_id=patient_id,
            measure_canonical=self._measure_canonical,
            period_start=self._period_start,
            period_end=self._period_end,
            resources=filtered_resources,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # The reporter Organization is NOT re-sent here. build_submission_workflow
        # PUTs it to the MCS once per job, before any batch starts; every
        # patient's MeasureReport.reporter reference resolves against that
        # server-side copy. See the comment there for why inlining a copy of
        # the SAME client-assigned Organization/lenny-reporter into every
        # patient's payload is unsafe under concurrent batches.
        submitted = filtered_resources
        # Snapshot the mode used for THIS attempt. self._mode is shared,
        # mutable instance state: one DeqmSubmitDataWorkflow is built per job
        # (orchestrator.py) and its transfer_patient() runs concurrently
        # across patients in different batches under
        # asyncio.Semaphore(MAX_WORKERS) + asyncio.gather. Two patients can
        # both read self._mode == stu5 here, both send STU5 requests, and
        # both fail with a 400 — but by the time the SECOND one's except
        # handler below runs, the FIRST one may already have flipped
        # self._mode to base-fallback. The downgrade guard must judge this
        # attempt against the mode it was actually sent under (attempt_mode),
        # never against the live self._mode read after the `await` —
        # otherwise the second patient's guard sees the sibling's flip, comes
        # up False, and that patient is stranded (raises TransferPhaseError)
        # instead of being rescued like its sibling. Do NOT "simplify" this
        # back to `self._mode` in the guard below.
        attempt_mode = self._mode
        if attempt_mode == SUBMIT_DATA_MODE_STU5:
            parameters = build_stu5_parameters(measure_report, submitted)
        else:
            parameters = build_base_parameters(measure_report, submitted)

        try:
            await submit_data(
                mcs_url=self._mcs_url,
                parameters=parameters,
                mode=attempt_mode,
                measure_id=self._measure_id,
                auth_headers=self._mcs_auth_headers,
            )
        except FhirOperationError as exc:
            # A mis-probed capability stamps Job.submit_data_mode="stu5" for a
            # server that doesn't actually implement $deqm-submit-data. Rather
            # than fail every patient in the job, downgrade to base mode on the
            # first capability-mismatch status (_DOWNGRADE_STATUS_CODES) and
            # retry once. Job.submit_data_mode still shows the probe's
            # original verdict — reconciling the UI badge with a runtime
            # downgrade is deliberately out of scope here.
            if attempt_mode == SUBMIT_DATA_MODE_STU5 and exc.status_code in _DOWNGRADE_STATUS_CODES:
                logger.warning(
                    "STU5 $deqm-submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                    exc.status_code,
                    self._job_id,
                    extra={"job_id": self._job_id, "patient_id": patient_id, "status_code": exc.status_code},
                )
                # Flipping shared state here is the intended optimization —
                # later patients (and later batches) skip straight to base
                # mode instead of re-probing STU5 themselves. It's fine for
                # this write to race with a concurrent sibling's own flip
                # because it's idempotent (both write the same constant); the
                # bug was only ever in a *guard* consulting this field.
                self._mode = SUBMIT_DATA_MODE_BASE
                retry_parameters = build_base_parameters(measure_report, submitted)
                try:
                    await submit_data(
                        mcs_url=self._mcs_url,
                        parameters=retry_parameters,
                        mode=SUBMIT_DATA_MODE_BASE,
                        measure_id=self._measure_id,
                        auth_headers=self._mcs_auth_headers,
                    )
                except Exception as retry_exc:
                    raise TransferPhaseError("submit", retry_exc) from retry_exc
            else:
                raise TransferPhaseError("submit", exc) from exc
        except Exception as exc:
            raise TransferPhaseError("submit", exc) from exc
        return gather


async def build_submission_workflow(
    *,
    workflow: str,
    job_id: int,
    measure_id: str,
    mcs_url: str,
    mcs_auth_headers: dict[str, str] | None,
    submit_data_mode: str | None,
    period_start: str,
    period_end: str,
) -> SubmissionWorkflow:
    """Build the job's workflow. For DEQM, fetches the measure canonical from
    the MCS — raising (job fails fast) when the Measure can't be read, or when
    the resolved mode is STU5 and the canonical isn't an absolute URL."""
    if workflow == "deqm_submit_data":
        canonical = await get_measure_canonical(measure_id, mcs_url=mcs_url, auth_headers=mcs_auth_headers or {})
        resolved_mode = submit_data_mode or SUBMIT_DATA_MODE_BASE
        if resolved_mode == SUBMIT_DATA_MODE_STU5 and not canonical.startswith("http"):
            # In STU5 mode the type-level POST makes MeasureReport.measure the
            # ONLY identifier for the submitted measure — a relative reference
            # (degraded from a Measure with no `url`) is not a resolvable
            # canonical there. Fail fast at job build rather than emitting an
            # unattributable submission. Base-fallback mode is unaffected: the
            # measure is already named in the instance-level submit URL.
            raise ValueError(
                f"Measure '{measure_id}' has no absolute canonical URL (got {canonical!r}), "
                "which is required for DEQM STU5 $deqm-submit-data submissions."
            )
        # DEQM STU5 says a submission's references should resolve WITHIN the
        # submission, which is why the reporter Organization used to travel
        # inline in every patient's $submit-data payload. In practice that
        # inline copy is the SAME client-assigned Organization/lenny-reporter
        # on every patient in the job, and batches run concurrently
        # (asyncio.Semaphore(settings.MAX_WORKERS) in orchestrator.py) — HAPI
        # saw up to MAX_WORKERS transactions try to upsert that one resource
        # id at the same time and answered every one of them with
        # ResourceVersionConflictException (HAPI-0550/HAPI-0823), failing
        # 100% of patients on the real stack (two 319-patient runs both
        # status=failed, processed=0). Store the reporter ONCE per job here,
        # before any batch starts, and let each patient's
        # MeasureReport.reporter reference resolve against this server-side
        # copy instead. Tradeoff: a strict-STU5 receiver that refuses to
        # resolve references outside the submitted payload would need the
        # inline per-patient copy back — and would then also need
        # submissions serialized (not concurrent) to avoid reintroducing
        # this same version conflict. Let failure here raise: it fails the
        # job fast with one clear error instead of every patient failing
        # later for the same reason.
        # The reporter push itself now lives in
        # DeqmSubmitDataWorkflow.ensure_target_prerequisites(), which the
        # orchestrator calls AFTER the wipe. Pushing it here meant any job with
        # mcs_wipe_before_job set deleted it again immediately, because the full
        # wipe removes Organization.
        return DeqmSubmitDataWorkflow(
            job_id=job_id,
            measure_id=measure_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            measure_canonical=canonical,
            period_start=period_start,
            period_end=period_end,
            mode=resolved_mode,
        )
    return DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers)
