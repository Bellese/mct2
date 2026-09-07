"""DEQM STU5 data-exchange payload builders.

Pure functions that assemble the DEQM Data Exchange MeasureReport and the two
$submit-data Parameters envelopes (STU5 `bundle` form and base-FHIR
`measureReport`+`resource` form). No I/O here — HTTP delivery lives in
fhir_client.submit_data, orchestration in workflows.DeqmSubmitDataWorkflow.

Spec: docs/superpowers/specs/2026-08-21-deqm-submit-data-workflow-design.md
IG:   https://hl7.org/fhir/us/davinci-deqm/STU5/
"""

import hashlib
import re
from typing import Any

DEQM_DATA_EXCHANGE_PROFILE = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/datax-measurereport-deqm"
DEQM_UPDATE_TYPE_EXT = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/extension-submitDataUpdateType"

# FHIR's `id` element is capped at 64 characters (spec: Resource.id).
_MAX_FHIR_ID_LENGTH = 64

# FHIR's `id` element is also restricted to this charset (spec: Resource.id).
# patient_id comes from a third-party CDR, so an underscore/colon/non-ASCII id
# would otherwise flow straight into the composed id below and yield an
# invalid MeasureReport id — a 400 for every patient with such an id.
_FHIR_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9\-.]+$")

# DEQM requires MeasureReport.reporter 1..1 (Organization). Lenny is the
# reporter; this fixed resource travels inside every submission so the
# reference resolves without the receiver chasing external references.
LENNY_REPORTER_ORG: dict[str, Any] = {
    "resourceType": "Organization",
    "id": "lenny-reporter",
    "name": "Lenny Measure Calculation Tool",
    "active": True,
}


def _measure_report_id(job_id: int, patient_id: str) -> str:
    """Build the `deqm-{job_id}-{patient_id}` id, falling back to a stable
    hash of patient_id when the composed id would overflow FHIR's 64-char
    `id` limit OR contain characters outside `[A-Za-z0-9\\-.]`.

    A long or illegally-charset patient_id can otherwise produce an invalid
    `id`; HAPI 400s the whole submission when it does. When that happens,
    keep the short `deqm-{job_id}-` prefix (useful for debugging) and replace
    patient_id with a stable hash of it, so the result is deterministic
    across calls for the same (job_id, patient_id) pair.
    """
    candidate = f"deqm-{job_id}-{patient_id}"
    if len(candidate) <= _MAX_FHIR_ID_LENGTH and _FHIR_ID_CHARSET_RE.match(candidate):
        return candidate
    prefix = f"deqm-{job_id}-"
    digest = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
    available = max(_MAX_FHIR_ID_LENGTH - len(prefix), 1)
    return f"{prefix}{digest[:available]}"[:_MAX_FHIR_ID_LENGTH]


def build_data_exchange_measure_report(
    *,
    job_id: int,
    patient_id: str,
    measure_canonical: str,
    period_start: str,
    period_end: str,
    resources: list[dict[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    """Build a DEQM Data Exchange MeasureReport for one patient's submission.

    `type` is `data-collection` — the R4 wire code; R5 renamed it to
    `data-exchange` but DEQM STU5 is R4-based. `submitDataUpdateType` is
    always `snapshot`: the job wipes the target's prior-run data first, and
    `incremental` would require stable ids + meta.source on every resource.
    `group` is intentionally absent — the profile prohibits measureScore and
    stratifier on data-exchange reports.
    """
    return {
        "resourceType": "MeasureReport",
        "id": _measure_report_id(job_id, patient_id),
        "meta": {"profile": [DEQM_DATA_EXCHANGE_PROFILE]},
        "extension": [{"url": DEQM_UPDATE_TYPE_EXT, "valueCode": "snapshot"}],
        "status": "complete",
        "type": "data-collection",
        "measure": measure_canonical,
        "subject": {"reference": f"Patient/{patient_id}"},
        "date": timestamp,
        "reporter": {"reference": f"Organization/{LENNY_REPORTER_ORG['id']}"},
        "period": {"start": period_start, "end": period_end},
        "evaluatedResource": [
            {"reference": f"{r['resourceType']}/{r['id']}"} for r in resources if r.get("resourceType") and r.get("id")
        ],
    }


def build_stu5_parameters(measure_report: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """STU5 $deqm-submit-data envelope: one single-subject collection Bundle."""
    return {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "bundle",
                "resource": {
                    "resourceType": "Bundle",
                    "type": "collection",
                    "entry": [{"resource": measure_report}] + [{"resource": r} for r in resources],
                },
            }
        ],
    }


def build_base_parameters(measure_report: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Base-FHIR $submit-data envelope (what HAPI clinical-reasoning accepts)."""
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "measureReport", "resource": measure_report}]
        + [{"name": "resource", "resource": r} for r in resources],
    }
