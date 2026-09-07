# DEQM $submit-data Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick a per-job data submission workflow — today's `$everything`+PUT push (`direct_load`, default) or a new DEQM STU5 `$submit-data` workflow (`deqm_submit_data`) with targeted queries and capability-detected wire format.

**Architecture:** A `SubmissionWorkflow` strategy layer (new `backend/app/services/workflows.py`) owns phase-1 "transfer" (gather from CDR + deliver to MCS) per patient; the orchestrator calls `workflow.transfer_patient(...)` instead of inline gather+push. Pure DEQM payload builders live in a new `backend/app/services/deqm.py`. The MCS's `$submit-data` capability is probed once at `POST /jobs` pre-flight and snapshotted on the Job (`submit_data_mode`), with `base-fallback` surfaced as an STU5-compliance warning in the UI.

**Tech Stack:** FastAPI + SQLAlchemy (async) + httpx backend, plain-JS React frontend, HAPI FHIR (CDR + measure engine), pytest / react-testing-library.

**Spec:** `docs/superpowers/specs/2026-08-21-deqm-submit-data-workflow-design.md`

## Global Constraints

- Python 3.10+; `X | None` union syntax, NOT `Optional[X]` in new code; type hints required.
- Lint gate: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`.
- React is plain JavaScript, PascalCase components, co-located CSS Modules.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`).
- No hardcoded URLs/credentials — config via `backend/app/config.py` env settings.
- Do NOT modify `TODOS.md`.
- Workflow value strings are exactly `direct_load` and `deqm_submit_data`; submit-data mode strings are exactly `stu5` and `base-fallback`.
- Already true on main — do NOT re-implement: `DataRequirementsStrategy(measure_id, mcs_url, mcs_auth_headers)` targets the job's MCS (issue #397); the scoped wipe already deletes `MeasureReport` by patient (`_PATIENT_SCOPED_TYPES`, fhir_client.py:1114).
- All backend work under `backend/`, run pytest from `backend/`: `python3 -m pytest tests/ --ignore=tests/integration -v`.

---

### Task 1: DEQM payload builders (`deqm.py`)

**Files:**
- Create: `backend/app/services/deqm.py`
- Test: `backend/tests/test_services_deqm.py`

**Interfaces:**
- Consumes: nothing (pure functions).
- Produces (used by Task 4):
  - `LENNY_REPORTER_ORG: dict` — Organization resource, id `lenny-reporter`
  - `build_data_exchange_measure_report(*, job_id: int, patient_id: str, measure_canonical: str, period_start: str, period_end: str, resources: list[dict], timestamp: str) -> dict`
  - `build_stu5_parameters(measure_report: dict, resources: list[dict]) -> dict`
  - `build_base_parameters(measure_report: dict, resources: list[dict]) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_services_deqm.py`:

```python
"""Tests for the DEQM data-exchange payload builders (deqm.py)."""

from app.services.deqm import (
    DEQM_DATA_EXCHANGE_PROFILE,
    DEQM_UPDATE_TYPE_EXT,
    LENNY_REPORTER_ORG,
    build_base_parameters,
    build_data_exchange_measure_report,
    build_stu5_parameters,
)

_RESOURCES = [
    {"resourceType": "Patient", "id": "p1"},
    {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}},
    {"resourceType": "Encounter", "id": "e1"},
]


def _mr() -> dict:
    return build_data_exchange_measure_report(
        job_id=42,
        patient_id="p1",
        measure_canonical="http://example.org/Measure/CMS122|1.0.0",
        period_start="2025-01-01",
        period_end="2025-12-31",
        resources=_RESOURCES,
        timestamp="2026-08-21T12:00:00+00:00",
    )


class TestBuildDataExchangeMeasureReport:
    def test_required_deqm_elements(self):
        mr = _mr()
        assert mr["resourceType"] == "MeasureReport"
        assert mr["id"] == "deqm-42-p1"
        assert mr["meta"]["profile"] == [DEQM_DATA_EXCHANGE_PROFILE]
        assert mr["status"] == "complete"
        assert mr["type"] == "data-collection"
        assert mr["measure"] == "http://example.org/Measure/CMS122|1.0.0"
        assert mr["subject"] == {"reference": "Patient/p1"}
        assert mr["date"] == "2026-08-21T12:00:00+00:00"
        assert mr["reporter"] == {"reference": "Organization/lenny-reporter"}
        assert mr["period"] == {"start": "2025-01-01", "end": "2025-12-31"}

    def test_snapshot_update_type_extension(self):
        mr = _mr()
        assert {"url": DEQM_UPDATE_TYPE_EXT, "valueCode": "snapshot"} in mr["extension"]

    def test_evaluated_resources_reference_all_submitted(self):
        mr = _mr()
        refs = [er["reference"] for er in mr["evaluatedResource"]]
        assert refs == ["Patient/p1", "Condition/c1", "Encounter/e1"]

    def test_no_group_score_or_stratifier(self):
        mr = _mr()
        assert "group" not in mr  # profile prohibits measureScore/stratifier

    def test_resources_without_ids_are_skipped_in_evaluated_resource(self):
        mr = build_data_exchange_measure_report(
            job_id=1,
            patient_id="p1",
            measure_canonical="http://example.org/Measure/M",
            period_start="2025-01-01",
            period_end="2025-12-31",
            resources=[{"resourceType": "Observation"}],  # no id
            timestamp="2026-08-21T12:00:00+00:00",
        )
        assert mr["evaluatedResource"] == []


class TestParameterEnvelopes:
    def test_stu5_parameters_single_bundle(self):
        mr = _mr()
        params = build_stu5_parameters(mr, [LENNY_REPORTER_ORG, *_RESOURCES])
        assert params["resourceType"] == "Parameters"
        assert len(params["parameter"]) == 1
        p = params["parameter"][0]
        assert p["name"] == "bundle"
        bundle = p["resource"]
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "collection"
        entry_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        # MeasureReport first, then reporter org + data-of-interest
        assert entry_types == ["MeasureReport", "Organization", "Patient", "Condition", "Encounter"]

    def test_base_parameters_measurereport_plus_resources(self):
        mr = _mr()
        params = build_base_parameters(mr, [LENNY_REPORTER_ORG, *_RESOURCES])
        assert params["resourceType"] == "Parameters"
        names = [p["name"] for p in params["parameter"]]
        assert names == ["measureReport", "resource", "resource", "resource", "resource"]
        assert params["parameter"][0]["resource"] is mr
        assert params["parameter"][1]["resource"]["resourceType"] == "Organization"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_deqm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.deqm'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/deqm.py`:

```python
"""DEQM STU5 data-exchange payload builders.

Pure functions that assemble the DEQM Data Exchange MeasureReport and the two
$submit-data Parameters envelopes (STU5 `bundle` form and base-FHIR
`measureReport`+`resource` form). No I/O here — HTTP delivery lives in
fhir_client.submit_data, orchestration in workflows.DeqmSubmitDataWorkflow.

Spec: docs/superpowers/specs/2026-08-21-deqm-submit-data-workflow-design.md
IG:   https://hl7.org/fhir/us/davinci-deqm/STU5/
"""

from typing import Any

DEQM_DATA_EXCHANGE_PROFILE = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/datax-measurereport-deqm"
DEQM_UPDATE_TYPE_EXT = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/extension-submitDataUpdateType"

# DEQM requires MeasureReport.reporter 1..1 (Organization). Lenny is the
# reporter; this fixed resource travels inside every submission so the
# reference resolves without the receiver chasing external references.
LENNY_REPORTER_ORG: dict[str, Any] = {
    "resourceType": "Organization",
    "id": "lenny-reporter",
    "name": "Lenny Measure Calculation Tool",
    "active": True,
}


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
        "id": f"deqm-{job_id}-{patient_id}",
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
            {"reference": f"{r['resourceType']}/{r['id']}"}
            for r in resources
            if r.get("resourceType") and r.get("id")
        ],
    }


def build_stu5_parameters(
    measure_report: dict[str, Any], resources: list[dict[str, Any]]
) -> dict[str, Any]:
    """STU5 $deqm-submit-data envelope: one single-subject collection Bundle."""
    return {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "bundle",
                "resource": {
                    "resourceType": "Bundle",
                    "type": "collection",
                    "entry": [{"resource": measure_report}]
                    + [{"resource": r} for r in resources],
                },
            }
        ],
    }


def build_base_parameters(
    measure_report: dict[str, Any], resources: list[dict[str, Any]]
) -> dict[str, Any]:
    """Base-FHIR $submit-data envelope (what HAPI clinical-reasoning accepts)."""
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "measureReport", "resource": measure_report}]
        + [{"name": "resource", "resource": r} for r in resources],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_deqm.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format app/services/deqm.py tests/test_services_deqm.py
git add backend/app/services/deqm.py backend/tests/test_services_deqm.py
git commit -m "feat(deqm): add DEQM data-exchange MeasureReport and Parameters builders"
```

---

### Task 2: FHIR client additions — canonical fetch, capability probe, submit_data

**Files:**
- Modify: `backend/app/services/fhir_client.py` (append new functions near `evaluate_measure`, which ends ~line 902)
- Test: `backend/tests/test_services_fhir_client.py` (append)

**Interfaces:**
- Consumes: existing `FhirOperationError` / `FhirOperationOutcome` from `app.services.fhir_errors` (constructor: `FhirOperationError(*, operation, url, status_code, outcome, latency_ms, cause=None)`), existing `_DUMMY_REQUEST`/`_make_response` test helpers.
- Produces (used by Tasks 3–5):
  - `SUBMIT_DATA_MODE_STU5 = "stu5"`, `SUBMIT_DATA_MODE_BASE = "base-fallback"` (module constants)
  - `async get_measure_canonical(measure_id: str, *, mcs_url: str, auth_headers: dict[str, str] | None = None) -> str`
  - `async detect_submit_data_mode(*, mcs_url: str, auth_headers: dict[str, str] | None = None, timeout: float = 10.0) -> str`
  - `async submit_data(*, mcs_url: str, parameters: dict, mode: str, auth_headers: dict[str, str] | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_fhir_client.py` (reuse the file's existing `_make_response` helper and `AsyncMock`/`patch` mocking style — see its `TestEvaluateMeasure`-style classes for the `httpx.AsyncClient` context-manager mock pattern):

```python
from app.services.fhir_client import (  # add to the existing import block
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    detect_submit_data_mode,
    get_measure_canonical,
    submit_data,
)


def _mock_async_client(mock_httpx, *, get=None, post=None):
    """Wire an AsyncMock client into the patched httpx.AsyncClient ctor."""
    ctx = AsyncMock()
    if get is not None:
        ctx.get = get
    if post is not None:
        ctx.post = post
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestGetMeasureCanonical:
    async def test_returns_url_pipe_version(self):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1", "version": "2.0"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1|2.0"

    async def test_returns_bare_url_without_version(self):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1"

    async def test_falls_back_to_relative_reference_when_url_missing(self):
        measure = {"resourceType": "Measure", "id": "m1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "Measure/m1"

    async def test_raises_on_http_error(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(404, {"resourceType": "OperationOutcome"})))
            with pytest.raises(FhirOperationError):
                await get_measure_canonical("m1", mcs_url="http://mcs")


class TestDetectSubmitDataMode:
    def _capability(self, operations: list[dict]) -> dict:
        return {
            "resourceType": "CapabilityStatement",
            "rest": [{"mode": "server", "resource": [{"type": "Measure", "operation": operations}]}],
        }

    async def test_stu5_when_deqm_operation_name_present(self):
        cap = self._capability([{"name": "deqm-submit-data", "definition": "http://x"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, cap)))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5

    async def test_stu5_when_deqm_canonical_definition_present(self):
        cap = self._capability(
            [{"name": "whatever", "definition": "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data"}]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, cap)))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5

    async def test_fallback_when_only_base_submit_data(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, cap)))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_fallback_when_probe_raises(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=httpx.ConnectError("boom")))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE


class TestSubmitData:
    async def test_posts_to_deqm_operation_in_stu5_mode(self):
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(mcs_url="http://mcs", parameters={"resourceType": "Parameters"}, mode=SUBMIT_DATA_MODE_STU5)
        assert post.call_args[0][0] == "http://mcs/Measure/$deqm-submit-data"

    async def test_posts_to_base_operation_in_fallback_mode(self):
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(mcs_url="http://mcs", parameters={"resourceType": "Parameters"}, mode=SUBMIT_DATA_MODE_BASE)
        assert post.call_args[0][0] == "http://mcs/Measure/$submit-data"

    async def test_raises_fhir_operation_error_on_4xx(self):
        oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "invalid", "diagnostics": "bad payload"}]}
        post = AsyncMock(return_value=_make_response(400, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError) as exc_info:
                await submit_data(mcs_url="http://mcs", parameters={"resourceType": "Parameters"}, mode=SUBMIT_DATA_MODE_BASE)
        assert exc_info.value.status_code == 400
        assert exc_info.value.operation == "submit-data"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py -k "Canonical or DetectSubmitData or TestSubmitData" -v`
Expected: FAIL with `ImportError: cannot import name 'SUBMIT_DATA_MODE_BASE'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/services/fhir_client.py` after `evaluate_measure` (~line 903). Mirror `evaluate_measure`'s error construction (`FhirOperationOutcome.from_response`, `FhirOperationError`); those names are already imported at the top of the module:

```python
# --- DEQM $submit-data support (spec: 2026-08-21-deqm-submit-data-workflow) ---

SUBMIT_DATA_MODE_STU5 = "stu5"
SUBMIT_DATA_MODE_BASE = "base-fallback"

_DEQM_SUBMIT_DATA_CANONICAL = "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data"
_DEQM_SUBMIT_DATA_OP_NAME = "deqm-submit-data"


async def get_measure_canonical(
    measure_id: str,
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
) -> str:
    """Read Measure/{id} off the MCS and return its canonical `url|version`.

    The DEQM Data Exchange MeasureReport's `measure` element must carry the
    measure's canonical URL, which only the MCS knows. Raises
    FhirOperationError when the Measure can't be read — the job should fail
    fast rather than submit reports pointing at nothing. A Measure without a
    `url` (unusual but legal) degrades to the relative reference.
    """
    url = f"{mcs_url}/Measure/{measure_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        start_ms = int(time.monotonic() * 1000)
        resp = await client.get(url, headers=auth_headers or {})
        latency_ms = int(time.monotonic() * 1000) - start_ms
        if resp.status_code != 200:
            raise FhirOperationError(
                operation="read-measure",
                url=url,
                status_code=resp.status_code,
                outcome=FhirOperationOutcome.from_response(resp),
                latency_ms=latency_ms,
            )
        measure = resp.json()
    canonical = measure.get("url")
    if not canonical:
        return f"Measure/{measure_id}"
    version = measure.get("version")
    return f"{canonical}|{version}" if version else canonical


async def detect_submit_data_mode(
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> str:
    """Probe the MCS CapabilityStatement for DEQM STU5 $deqm-submit-data.

    Returns SUBMIT_DATA_MODE_STU5 when the Measure resource advertises an
    operation named `deqm-submit-data` or defined by the DEQM canonical.
    Everything else — including an unreachable/unparseable /metadata — is
    SUBMIT_DATA_MODE_BASE. Never raises: the probe decides the envelope,
    it must not block job creation (the measure pre-flight already proved
    the MCS reachable).
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{mcs_url}/metadata", headers=auth_headers or {})
            resp.raise_for_status()
            capability = resp.json()
        for rest in capability.get("rest", []):
            operations = list(rest.get("operation", []))
            for res in rest.get("resource", []):
                if res.get("type") == "Measure":
                    operations.extend(res.get("operation", []))
            for op in operations:
                if op.get("name") == _DEQM_SUBMIT_DATA_OP_NAME:
                    return SUBMIT_DATA_MODE_STU5
                if str(op.get("definition", "")).startswith(_DEQM_SUBMIT_DATA_CANONICAL):
                    return SUBMIT_DATA_MODE_STU5
    except Exception as exc:
        logger.warning(
            "CapabilityStatement probe for $deqm-submit-data failed — assuming base $submit-data",
            extra={"mcs_url": sanitize_url(mcs_url), "error": str(exc)},
        )
    return SUBMIT_DATA_MODE_BASE


async def submit_data(
    *,
    mcs_url: str,
    parameters: dict[str, Any],
    mode: str,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """POST a $submit-data Parameters payload to the MCS.

    STU5 mode targets `Measure/$deqm-submit-data`; base mode targets
    `Measure/$submit-data` (the shape HAPI clinical-reasoning implements —
    it stores the MeasureReport and resources into the server). Any 2xx is
    success; the response body (HAPI returns a transaction Bundle) carries
    no information the job needs.
    """
    operation = "$deqm-submit-data" if mode == SUBMIT_DATA_MODE_STU5 else "$submit-data"
    url = f"{mcs_url}/Measure/{operation}"
    headers = {"Content-Type": "application/fhir+json", **(auth_headers or {})}
    async with httpx.AsyncClient(timeout=120.0) as client:
        start_ms = int(time.monotonic() * 1000)
        resp = await client.post(url, json=parameters, headers=headers)
        latency_ms = int(time.monotonic() * 1000) - start_ms
        if resp.status_code >= 300:
            raise FhirOperationError(
                operation="submit-data",
                url=url,
                status_code=resp.status_code,
                outcome=FhirOperationOutcome.from_response(resp),
                latency_ms=latency_ms,
            )
    logger.info(
        "Submitted data via %s", operation,
        extra={"mcs_url": sanitize_url(mcs_url), "latency_ms": latency_ms},
    )
```

Note: `time` is already imported in fhir_client.py (used by `evaluate_measure`). Verify `Any` is imported from `typing` (it is — `GatherResult` uses it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py -v`
Expected: all PASS (new and pre-existing)

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/fhir_client.py backend/tests/test_services_fhir_client.py
git commit -m "feat(fhir): add measure-canonical fetch, \$submit-data capability probe, and submit_data"
```

---

### Task 3: Job model columns, migration, and jobs API surface

**Files:**
- Modify: `backend/app/models/job.py` (Job class, after `mcs_wipe_before_job` ~line 86)
- Modify: `backend/app/main.py` (`_run_schema_migrations` jobs block, ~lines 230–242)
- Modify: `backend/app/routes/jobs.py` (`JobCreate` ~line 49, `JobResponse` ~line 66, `_job_to_response` ~line 113, `create_job` ~line 188)
- Test: `backend/tests/test_routes_jobs.py` (append)

**Interfaces:**
- Consumes: `detect_submit_data_mode`, `SUBMIT_DATA_MODE_BASE` from Task 2.
- Produces: `Job.workflow: str` (default `direct_load`), `Job.submit_data_mode: str | None`; `JobCreate.workflow`; `workflow` + `submit_data_mode` in every job response dict. Task 5 reads `job.workflow`/`job.submit_data_mode`; Task 6 reads the response fields.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_routes_jobs.py`. First read its existing job-creation tests to copy the fixture/mocking style (it patches `measure_exists` and uses the app test client). Add:

```python
class TestJobWorkflowSelection:
    async def test_create_job_defaults_to_direct_load(self, client, mock_measure_exists):
        resp = await client.post("/jobs", json={
            "measure_id": "M1", "period_start": "2025-01-01", "period_end": "2025-12-31",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["workflow"] == "direct_load"
        assert body["submit_data_mode"] is None

    async def test_create_job_rejects_unknown_workflow(self, client, mock_measure_exists):
        resp = await client.post("/jobs", json={
            "measure_id": "M1", "period_start": "2025-01-01", "period_end": "2025-12-31",
            "workflow": "carrier-pigeon",
        })
        assert resp.status_code == 422

    async def test_deqm_job_records_probe_result(self, client, mock_measure_exists):
        with patch("app.routes.jobs.detect_submit_data_mode", new=AsyncMock(return_value="base-fallback")) as probe:
            resp = await client.post("/jobs", json={
                "measure_id": "M1", "period_start": "2025-01-01", "period_end": "2025-12-31",
                "workflow": "deqm_submit_data",
            })
        assert resp.status_code == 201
        body = resp.json()
        assert body["workflow"] == "deqm_submit_data"
        assert body["submit_data_mode"] == "base-fallback"
        probe.assert_awaited_once()

    async def test_direct_load_job_skips_probe(self, client, mock_measure_exists):
        with patch("app.routes.jobs.detect_submit_data_mode", new=AsyncMock()) as probe:
            resp = await client.post("/jobs", json={
                "measure_id": "M1", "period_start": "2025-01-01", "period_end": "2025-12-31",
                "workflow": "direct_load",
            })
        assert resp.status_code == 201
        probe.assert_not_awaited()
```

Adapt fixture names (`client`, `mock_measure_exists`) to whatever `test_routes_jobs.py` actually uses — copy an existing passing create-job test as the template. Keep the four behaviors exactly as above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py -k Workflow -v`
Expected: FAIL (`workflow` KeyError / 422 mismatch / ImportError on `detect_submit_data_mode`)

- [ ] **Step 3: Implement model + migration**

In `backend/app/models/job.py`, after `mcs_wipe_before_job` (line 86):

```python
    # Data submission workflow (spec: 2026-08-21-deqm-submit-data-workflow).
    # Snapshotted at creation like the connection fields; legacy rows read as
    # direct_load, which is exactly how they behaved.
    workflow: Mapped[str] = mapped_column(
        String(32), nullable=False, default="direct_load", server_default="direct_load"
    )
    # Wire-format decision from the creation-time CapabilityStatement probe:
    # "stu5" | "base-fallback". NULL for direct_load jobs, where no $submit-data
    # call ever happens. base-fallback renders as an STU5-compliance warning.
    submit_data_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
```

(`Optional` is already imported in this module; keep its existing style.)

In `backend/app/main.py`, add to the jobs `ALTER TABLE` list (with the other `jobs` lines, ~line 240):

```python
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow VARCHAR(32) NOT NULL DEFAULT 'direct_load'",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS submit_data_mode VARCHAR(32)",
```

- [ ] **Step 4: Implement the route changes**

In `backend/app/routes/jobs.py`:

1. Import: add `detect_submit_data_mode` to the existing `from app.services.fhir_client import ...` block (line 27).
2. Add a module constant under `_PREFLIGHT_TIMEOUT_SECONDS` (line 46):

```python
_VALID_WORKFLOWS = {"direct_load", "deqm_submit_data"}
```

3. `JobCreate` (line 49) — add field + validator:

```python
    workflow: str = "direct_load"

    @field_validator("workflow")
    @classmethod
    def validate_workflow(cls, v: str) -> str:
        if v not in _VALID_WORKFLOWS:
            raise ValueError(f"workflow must be one of {sorted(_VALID_WORKFLOWS)}")
        return v
```

4. `JobResponse` (line 66) — add:

```python
    workflow: str = "direct_load"
    submit_data_mode: Optional[str] = None
```

5. `_job_to_response` (line 113) — add to the dict:

```python
        "workflow": job.workflow,
        "submit_data_mode": job.submit_data_mode,
```

6. `create_job` (line 188) — after the `if not found:` block (line 276) and before `job = Job(...)`:

```python
    # For DEQM jobs, decide the $submit-data wire format now and snapshot it.
    # The probe never raises (detect_submit_data_mode swallows errors into
    # base-fallback), so it cannot block creation; base-fallback renders as an
    # STU5-compliance warning in the UI from the moment the job appears.
    submit_data_mode: str | None = None
    if body.workflow == "deqm_submit_data":
        submit_data_mode = await detect_submit_data_mode(
            mcs_url=mcs.mcs_url,
            auth_headers=mcs_auth_headers,
            timeout=float(min(mcs.request_timeout_seconds, _PREFLIGHT_TIMEOUT_SECONDS)),
        )
```

and add to the `Job(...)` constructor call (line 278):

```python
        workflow=body.workflow,
        submit_data_mode=submit_data_mode,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_routes_jobs.py -v`
Expected: all PASS (new and pre-existing — pre-existing response-shape assertions may need the two new keys if they compare full dicts; fix those tests, not the code)

- [ ] **Step 6: Run the full unit suite, lint, commit**

```bash
cd backend && ruff check app/ tests/ && python3 -m pytest tests/ --ignore=tests/integration -q
git add backend/app/models/job.py backend/app/main.py backend/app/routes/jobs.py backend/tests/test_routes_jobs.py
git commit -m "feat(jobs): per-job workflow selection with \$submit-data capability snapshot"
```

---

### Task 4: `workflows.py` — SubmissionWorkflow strategies

**Files:**
- Create: `backend/app/services/workflows.py`
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `DataRequirementsStrategy`, `BatchQueryStrategy`, `GatherResult`, `push_resources`, `submit_data`, `get_measure_canonical`, `SUBMIT_DATA_MODE_STU5/BASE` (fhir_client); builders from `deqm.py` (Task 1); `settings.PATIENT_DATA_STRATEGY`.
- Produces (used by Task 5):
  - `class TransferPhaseError(Exception)` with `.phase: str` (`"gather"` | `"submit"`) and `.cause: Exception`
  - `class SubmissionWorkflow` — `async transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult`
  - `class DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers=None)`
  - `class DeqmSubmitDataWorkflow(*, job_id, measure_id, mcs_url, mcs_auth_headers, measure_canonical, period_start, period_end, mode)`
  - `async build_submission_workflow(*, workflow: str, job_id: int, measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None, submit_data_mode: str | None, period_start: str, period_end: str) -> SubmissionWorkflow`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_services_workflows.py`:

```python
"""Tests for the per-job submission workflow strategies (workflows.py)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.fhir_client import GatherResult
from app.services.workflows import (
    DeqmSubmitDataWorkflow,
    DirectLoadWorkflow,
    TransferPhaseError,
    build_submission_workflow,
)

pytestmark = pytest.mark.asyncio

_GATHER = GatherResult(resources=[
    {"resourceType": "Patient", "id": "p1"},
    {"resourceType": "Condition", "id": "c1"},
])


class TestDirectLoadWorkflow:
    async def test_gathers_then_pushes(self):
        wf = DirectLoadWorkflow("M1", "http://mcs", {"Authorization": "Bearer t"})
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is _GATHER
        push.assert_awaited_once_with(
            _GATHER.resources, target_url="http://mcs", auth_headers={"Authorization": "Bearer t"}
        )

    async def test_skips_push_when_nothing_gathered(self):
        wf = DirectLoadWorkflow("M1", "http://mcs")
        empty = GatherResult(resources=[])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=empty)),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        push.assert_not_awaited()

    async def test_gather_failure_raises_gather_phase(self):
        wf = DirectLoadWorkflow("M1", "http://mcs")
        with patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"

    async def test_push_failure_raises_gather_phase(self):
        # Push failures keep today's error_phase="gather" labeling for direct_load.
        wf = DirectLoadWorkflow("M1", "http://mcs")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.push_resources", new=AsyncMock(side_effect=RuntimeError("mcs down"))),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"


def _deqm_workflow(mode: str = "base-fallback") -> DeqmSubmitDataWorkflow:
    return DeqmSubmitDataWorkflow(
        job_id=7,
        measure_id="M1",
        mcs_url="http://mcs",
        mcs_auth_headers={},
        measure_canonical="http://ex.org/Measure/M1|1.0",
        period_start="2025-01-01",
        period_end="2025-12-31",
        mode=mode,
    )


class TestDeqmSubmitDataWorkflow:
    async def test_submits_deqm_measure_report_with_data(self):
        wf = _deqm_workflow()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is _GATHER
        submit.assert_awaited_once()
        kwargs = submit.call_args.kwargs
        assert kwargs["mcs_url"] == "http://mcs"
        assert kwargs["mode"] == "base-fallback"
        params = kwargs["parameters"]
        assert params["parameter"][0]["name"] == "measureReport"
        mr = params["parameter"][0]["resource"]
        assert mr["type"] == "data-collection"
        assert mr["subject"] == {"reference": "Patient/p1"}
        assert mr["id"] == "deqm-7-p1"
        submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
        assert submitted_types == ["Organization", "Patient", "Condition"]

    async def test_stu5_mode_uses_bundle_envelope(self):
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        params = submit.call_args.kwargs["parameters"]
        assert params["parameter"][0]["name"] == "bundle"
        assert submit.call_args.kwargs["mode"] == "stu5"

    async def test_submit_failure_raises_submit_phase(self):
        wf = _deqm_workflow()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=RuntimeError("rejected"))),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"

    async def test_gather_failure_raises_gather_phase(self):
        wf = _deqm_workflow()
        with patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"


class TestBuildSubmissionWorkflow:
    async def test_direct_load_needs_no_canonical_fetch(self):
        with patch("app.services.workflows.get_measure_canonical", new=AsyncMock()) as canon:
            wf = await build_submission_workflow(
                workflow="direct_load", job_id=1, measure_id="M1", mcs_url="http://mcs",
                mcs_auth_headers=None, submit_data_mode=None,
                period_start="2025-01-01", period_end="2025-12-31",
            )
        assert isinstance(wf, DirectLoadWorkflow)
        canon.assert_not_awaited()

    async def test_deqm_fetches_canonical_and_defaults_mode(self):
        with patch(
            "app.services.workflows.get_measure_canonical",
            new=AsyncMock(return_value="http://ex.org/Measure/M1|1.0"),
        ) as canon:
            wf = await build_submission_workflow(
                workflow="deqm_submit_data", job_id=1, measure_id="M1", mcs_url="http://mcs",
                mcs_auth_headers={}, submit_data_mode=None,  # legacy NULL → base
                period_start="2025-01-01", period_end="2025-12-31",
            )
        assert isinstance(wf, DeqmSubmitDataWorkflow)
        canon.assert_awaited_once_with("M1", mcs_url="http://mcs", auth_headers={})
        assert wf._mode == "base-fallback"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.workflows'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/workflows.py`:

```python
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

    @abc.abstractmethod
    async def transfer_patient(
        self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]
    ) -> GatherResult:
        """Transfer one patient's data; return the GatherResult for
        partial-failure bookkeeping. Raises TransferPhaseError on failure."""
        ...


class DirectLoadWorkflow(SubmissionWorkflow):
    """Today's behavior: env-configured gather, then a batch Bundle of PUTs."""

    name = "direct_load"

    def __init__(
        self, measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None = None
    ):
        self._strategy = _acquisition_strategy(measure_id, mcs_url, mcs_auth_headers)
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers

    async def transfer_patient(
        self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]
    ) -> GatherResult:
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
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers
        self._measure_canonical = measure_canonical
        self._period_start = period_start
        self._period_end = period_end
        self._mode = mode

    async def transfer_patient(
        self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]
    ) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc

        measure_report = build_data_exchange_measure_report(
            job_id=self._job_id,
            patient_id=patient_id,
            measure_canonical=self._measure_canonical,
            period_start=self._period_start,
            period_end=self._period_end,
            resources=gather.resources,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        submitted = [dict(LENNY_REPORTER_ORG)] + gather.resources
        if self._mode == SUBMIT_DATA_MODE_STU5:
            parameters = build_stu5_parameters(measure_report, submitted)
        else:
            parameters = build_base_parameters(measure_report, submitted)

        try:
            await submit_data(
                mcs_url=self._mcs_url,
                parameters=parameters,
                mode=self._mode,
                auth_headers=self._mcs_auth_headers,
            )
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
    the MCS — raising (job fails fast) when the Measure can't be read."""
    if workflow == "deqm_submit_data":
        canonical = await get_measure_canonical(
            measure_id, mcs_url=mcs_url, auth_headers=mcs_auth_headers or {}
        )
        return DeqmSubmitDataWorkflow(
            job_id=job_id,
            measure_id=measure_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            measure_canonical=canonical,
            period_start=period_start,
            period_end=period_end,
            mode=submit_data_mode or SUBMIT_DATA_MODE_BASE,
        )
    return DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat(workflows): SubmissionWorkflow strategies for direct load and DEQM \$submit-data"
```

---

### Task 5: Orchestrator wiring

**Files:**
- Modify: `backend/app/services/orchestrator.py`:
  - `run_job` (~line 160): read `job.workflow`/`job.submit_data_mode`, build the workflow once, pass to batches
  - `_process_single_batch` (~line 458): accept `workflow` param; replace inline strategy+gather+push (lines 503–535) with `workflow.transfer_patient`; use `TransferPhaseError.phase` in the error rows (lines 558–629)
  - Delete `_patient_data_strategy` (lines 135–144) — moved to `workflows._acquisition_strategy`
- Test: `backend/tests/test_services_orchestrator.py` (update + append)

**Interfaces:**
- Consumes: `build_submission_workflow`, `TransferPhaseError`, `SubmissionWorkflow` from Task 4.
- Produces: `_process_single_batch(job_id, batch_id, patient_map, cdr_url, auth_headers, mcs_url, mcs_auth_headers=None, *, workflow: SubmissionWorkflow)` — Task 7's integration tests exercise it via `run_job`.

- [ ] **Step 1: Update existing tests that the refactor breaks, then write the new failing tests**

Three groups of existing tests in `backend/tests/test_services_orchestrator.py` touch code that moves:

1. **`push_resources` patch target moves.** After this task, `push_resources` is called from `app.services.workflows`, not the orchestrator. Run `grep -n 'orchestrator.push_resources' backend/tests/test_services_orchestrator.py` and change every `patch("app.services.orchestrator.push_resources", ...)` to `patch("app.services.workflows.push_resources", ...)`. Same for any `orchestrator.push_resources` patches in other test files (`grep -rn 'orchestrator.push_resources' backend/tests/`).
2. **`test_process_batch_uses_everything_strategy` (line 938) and `test_process_batch_uses_data_requirements_strategy_when_configured` (line 1012)** patch `app.services.orchestrator.BatchQueryStrategy`/`DataRequirementsStrategy` and call `_process_single_batch` without a workflow. Rewrite both: patch the strategy class at `app.services.workflows.BatchQueryStrategy` (resp. `DataRequirementsStrategy`), build the workflow explicitly, and pass it:

```python
        from app.services.workflows import DirectLoadWorkflow

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
```

   with `mock_strategy_cls` patched BEFORE `DirectLoadWorkflow(...)` is constructed (the constructor instantiates the strategy). Keep each test's original assertion (`mock_strategy_cls.assert_called_once_with()` / the data-requirements constructor args).
3. **`test_run_job_happy_path` and friends** patch `BatchQueryStrategy.gather_patient_data` at class level (`patch.object(...BatchQueryStrategy, "gather_patient_data", ...)`) — these keep working unchanged, since `DirectLoadWorkflow` delegates to the same class. Only the `push_resources` target (group 1) changes in them.

Then add the new tests (module imports at top: `from app.services.workflows import SubmissionWorkflow, TransferPhaseError`):

```python
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
    from contextlib import ExitStack

    job_id = await _setup_job(test_session)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.workflow = "deqm_submit_data"
        await session.commit()

    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _StubWorkflow(TransferPhaseError("submit", RuntimeError("MCS rejected the payload")))

    with ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        mock_eval = stack.enter_context(
            patch("app.services.orchestrator.evaluate_measure", new_callable=AsyncMock)
        )
        await run_job(job_id)

    mock_eval.assert_not_awaited()
    async with session_factory() as session:
        row = (
            await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        ).scalar_one()
        assert row.error_phase == "submit"
        assert row.populations["error_phase"] == "submit"
        assert row.populations["error"] is True


async def test_direct_load_gather_failure_still_recorded_as_gather(test_session, session_factory):
    """Regression: phase labeling for direct_load transfer failures is unchanged."""
    from contextlib import ExitStack

    job_id = await _setup_job(test_session)
    patients = [{"resourceType": "Patient", "id": "p1", "name": [{"family": "Test"}]}]
    stub = _StubWorkflow(TransferPhaseError("gather", RuntimeError("CDR down")))

    with ExitStack() as stack:
        for p in _run_job_patches(session_factory, patients, stub):
            stack.enter_context(p)
        stack.enter_context(patch("app.services.orchestrator.evaluate_measure", new_callable=AsyncMock))
        await run_job(job_id)

    async with session_factory() as session:
        row = (
            await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        ).scalar_one()
        assert row.error_phase == "gather"
```

(`_setup_job` and `_make_session_factory_patch` are this file's existing helpers — read their definitions before use; if `_setup_job` accepts overrides, pass `workflow="deqm_submit_data"` there instead of the post-hoc update.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -v`
Expected: new tests FAIL (`build_submission_workflow` not used by orchestrator yet); updated imports FAIL until Step 3 lands

- [ ] **Step 3: Implement orchestrator changes**

1. Imports in `orchestrator.py`: remove `BatchQueryStrategy`/`DataRequirementsStrategy` from the `fhir_client` import if now unused elsewhere in the file (`BatchQueryStrategy` is still used at line 203 for `gather_patients` — keep it), and add:

```python
from app.services.workflows import TransferPhaseError, build_submission_workflow
```

2. Delete `_patient_data_strategy` (lines 135–144).

3. In `run_job`, extend the job read at lines 195–197 to also capture workflow fields, then build the workflow after the wipe (after line 240, where `mcs_url`/creds/patients are known — build BEFORE batch processing so a canonical-fetch failure fails the whole job via the existing outer `except`):

```python
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
```

(replaces the existing `job_for_group` read at lines 195–197)

```python
        # Build the submission workflow once per job. For DEQM this reads the
        # measure canonical off the MCS; failure aborts the job before any
        # patient work, surfacing as job.error_message via the outer except.
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
```

3b. Pass it through `process_batch` (line 271):

```python
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
```

4. `_process_single_batch` signature (line 458) — add keyword-only param:

```python
async def _process_single_batch(
    job_id: int,
    batch_id: int,
    patient_map: dict[str, Any],
    cdr_url: str,
    auth_headers: dict[str, str],
    mcs_url: str,
    mcs_auth_headers: dict[str, str] | None = None,
    *,
    workflow: SubmissionWorkflow,
) -> None:
```

(import `SubmissionWorkflow` for the annotation.)

5. Replace lines 503–535 (strategy creation + gather + push + log) with:

```python
            logger.info(
                "Using submission workflow",
                extra={"workflow": workflow.name, "job_id": job_id, "batch_id": batch_id},
            )
```

and inside the per-patient `try` (line 524), replace the gather+push block with:

```python
                try:
                    gather_result = await workflow.transfer_patient(cdr_url, patient_id, auth_headers)
                    logger.info(
                        f"Transferred {len(gather_result.resources)} resources for {patient_id[:8]}",
                        extra={"job_id": job_id, "patient_id": patient_id},
                    )
```

The partial-failure bookkeeping (lines 537–556) stays byte-identical — it only reads `gather_result`.

6. The `except` at line 558: change `except Exception as push_exc:` to unwrap phase and cause:

```python
                except Exception as transfer_exc:
                    if isinstance(transfer_exc, TransferPhaseError):
                        error_phase = transfer_exc.phase
                        push_exc: Exception = transfer_exc.cause
                    else:
                        error_phase = "gather"
                        push_exc = transfer_exc
```

then in the rest of that handler replace the three hardcoded `"gather"` phase strings (lines 604, 607, 622, 625 — `populations["error_phase"]`, `existing_row.error_phase`, and the `MeasureResult(...)` kwargs) with `error_phase`. The variable name `push_exc` is kept so the FhirOperationError detail extraction (lines 562–572) is untouched. The `gather_failed_patients.add(patient_id)` line stays — submit-failed patients also skip evaluation.

- [ ] **Step 4: Run the orchestrator + full unit suite**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -v && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/orchestrator.py backend/tests/test_services_orchestrator.py
git commit -m "feat(orchestrator): route phase-1 transfer through per-job submission workflows"
```

---

### Task 6: Frontend — workflow selector + fallback warning

**Files:**
- Modify: `frontend/src/pages/JobsPage.js` (form state line 58, `handleCreateJob` lines 158–178, modal JSX lines 470–489, status cell line 412)
- Modify: `frontend/src/pages/JobsPage.module.css` (append)
- Test: Create `frontend/src/pages/JobsPage.workflow.test.js` (render harness copied from `JobsPage.measureReset.test.js`)

**Interfaces:**
- Consumes: `workflow` + `submit_data_mode` in job API responses (Task 3); `createJob` API wrapper (unchanged — it JSON-posts whatever it's given).
- Produces: `formData.workflow`, POST body `workflow` field, `.workflowTag`/`.workflowTagWarn` CSS classes.

- [ ] **Step 1: Write the failing tests**

**Note:** `JobsPage.test.js` currently only unit-tests `formatDuration` — it has NO render harness. The render harness (ToastProvider + ConnectionContext + MemoryRouter + `jest.mock('../api/client')`) lives in `frontend/src/pages/JobsPage.measureReset.test.js`. Create a NEW file `frontend/src/pages/JobsPage.workflow.test.js`, copying that harness exactly (read `JobsPage.measureReset.test.js` first — reuse its `Harness` component and api mock shapes, e.g. `api.getJobs.mockResolvedValue(...)` matching whatever shape that file uses):

```javascript
import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import JobsPage from './JobsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

jest.mock('../api/client');

function Harness() {
  return (
    <ToastProvider>
      <ConnectionContext.Provider
        value={{
          cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
          mcs: { id: 'mcs-1', name: 'MCS', state: 'healthy', isReadOnly: false },
          refresh: jest.fn(),
        }}
      >
        <MemoryRouter>
          <JobsPage />
        </MemoryRouter>
      </ConnectionContext.Provider>
    </ToastProvider>
  );
}

const BASE_JOB = {
  id: 1,
  measure_id: 'CMS999',
  measure_name: 'Test Measure',
  period_start: '2025-01-01',
  period_end: '2025-12-31',
  cdr_url: 'http://cdr/fhir',
  group_id: null,
  status: 'complete',
  total_patients: 1,
  processed_patients: 1,
  failed_patients: 0,
  total_batches: 1,
  batches_completed: 1,
  delete_requested: false,
  created_at: '2026-08-21T00:00:00Z',
  started_at: '2026-08-21T00:00:01Z',
  completed_at: '2026-08-21T00:01:00Z',
  error_message: null,
};

describe('JobsPage — data submission workflow', () => {
  beforeEach(() => {
    api.getGroups = jest.fn().mockResolvedValue({ groups: [] });
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS999' }] });
    api.createJob = jest.fn().mockResolvedValue({ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' });
  });

  test('modal defaults to direct load and sends the selected workflow', async () => {
    api.getJobs = jest.fn().mockResolvedValue([]);
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const workflowSelect = await screen.findByLabelText(/Data submission workflow/i);
    expect(workflowSelect.value).toBe('direct_load');
    await userEvent.selectOptions(workflowSelect, 'deqm_submit_data');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    await waitFor(() =>
      expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ workflow: 'deqm_submit_data' }))
    );
  });

  test('DEQM job with base-fallback shows the STU5 warning badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue([
      { ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' },
    ]);
    render(<Harness />);
    const badge = await screen.findByTitle(/does not support DEQM STU5/i);
    expect(badge).toHaveTextContent('DEQM');
  });

  test('direct load jobs show no workflow badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue([
      { ...BASE_JOB, workflow: 'direct_load', submit_data_mode: null },
    ]);
    render(<Harness />);
    await screen.findByText('Test Measure');
    expect(screen.queryByText(/DEQM/)).not.toBeInTheDocument();
  });
});
```

Adjust the `api.getJobs` mock return shape to match how `JobsPage.js` consumes it (check `loadJobs` in the component — if it expects `{ jobs: [...] }` like `JobsPage.measureReset.test.js` mocks, wrap the arrays accordingly), and the measure-name assertion to whatever the table actually renders for `BASE_JOB` (the `measureOptionLabel` output).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && CI=true npm test -- --watchAll=false src/pages/JobsPage.workflow.test.js` (verify the invocation against the repo's `package.json` test script first)
Expected: FAIL (no workflow select, no badge)

- [ ] **Step 3: Implement**

1. Form state (line 58):

```javascript
const [formData, setFormData] = useState({ measure_id: '', group_id: '', period_start: '', period_end: '', workflow: 'direct_load' });
```

2. `handleCreateJob` POST body (lines 163–168) — add:

```javascript
        workflow: formData.workflow,
```

and after the `await createJob(...)` succeeds, surface the fallback immediately (the response carries `submit_data_mode`):

```javascript
      const created = await createJob({ /* existing fields */ });
      if (created.submit_data_mode === 'base-fallback') {
        toast(
          'MCS does not support DEQM STU5 $deqm-submit-data — base $submit-data fallback will be used.',
          { icon: '⚠️' }
        );
      }
```

(the file already imports `toast`; check its import and match the API used elsewhere in the file, e.g. `toast.error(...)`.)

3. Modal JSX — insert between the patient-group `</div>` (line 484) and `<PeriodPicker>` (line 485):

```jsx
              <div className={styles.field}>
                <label className={styles.label} htmlFor="workflow-select">Data submission workflow</label>
                <select id="workflow-select" className={styles.select} value={formData.workflow}
                  onChange={e => setFormData(p => ({ ...p, workflow: e.target.value }))}>
                  <option value="direct_load">Direct load — $everything (default)</option>
                  <option value="deqm_submit_data">DEQM Data Exchange — $submit-data (STU5)</option>
                </select>
              </div>
```

4. Status cell (line 412):

```jsx
                      <td data-label="Status">
                        <StatusBadge status={job.status} />
                        {job.workflow === 'deqm_submit_data' && (
                          <span
                            className={`${styles.workflowTag} ${job.submit_data_mode === 'base-fallback' ? styles.workflowTagWarn : ''}`}
                            title={job.submit_data_mode === 'base-fallback'
                              ? 'MCS does not support DEQM STU5 $deqm-submit-data — base $submit-data fallback used.'
                              : 'DEQM STU5 $deqm-submit-data'}
                          >
                            DEQM{job.submit_data_mode === 'base-fallback' ? ' ⚠' : ''}
                          </span>
                        )}
                      </td>
```

5. `JobsPage.module.css` — append (match the file's existing color-token usage; if it uses CSS variables, reuse them):

```css
.workflowTag {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  background: #eef2ff;
  color: #3730a3;
  vertical-align: middle;
}

.workflowTagWarn {
  background: #fef3c7;
  color: #92400e;
}
```

- [ ] **Step 4: Run frontend tests + build**

Run: `cd frontend && CI=true npm test -- --watchAll=false && npm run build`
Expected: all tests PASS (including `JobsPage.measureReset.test.js` — fix its fixtures if the new response fields break strict assertions), build succeeds

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/JobsPage.js frontend/src/pages/JobsPage.module.css frontend/src/pages/JobsPage.workflow.test.js
git commit -m "feat(ui): per-job data submission workflow selector with STU5 fallback warning"
```

---

### Task 7: Integration test — DEQM job end-to-end against real HAPI

**Files:**
- Create: `backend/tests/integration/test_deqm_submit_data_workflow.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5 via the public API + `run_job`; existing integration fixtures `integration_client`, `db_session`, `integration_session_factory`, `TEST_CDR_URL`, `TEST_MEASURE_URL` from `tests/integration/conftest.py`.
- Produces: proof that the feature works against the bundled engine (base-fallback path) with population parity.

- [ ] **Step 1: Manually probe $submit-data on the local test HAPI (implementation checkpoint)**

Bring up the test stack the way `./scripts/run-integration-tests.sh` does (read the script; it starts docker compose test services). Then:

```bash
python3 - <<'EOF'
import httpx, json
base = "http://localhost:8091/fhir"  # use TEST_MEASURE_URL from tests/integration/conftest.py
params = {"resourceType": "Parameters", "parameter": [
    {"name": "measureReport", "resource": {
        "resourceType": "MeasureReport", "id": "probe-mr", "status": "complete",
        "type": "data-collection", "measure": "http://example.org/probe",
        "period": {"start": "2025-01-01", "end": "2025-12-31"}}},
    {"name": "resource", "resource": {"resourceType": "Patient", "id": "probe-patient"}},
]}
r = httpx.post(f"{base}/Measure/$submit-data", json=params,
               headers={"Content-Type": "application/fhir+json"}, timeout=60)
print(r.status_code, r.text[:500])
# Re-run the same POST a second time: confirm 2xx again and that
# GET {base}/Patient/probe-patient returns ONE resource (upsert, not duplicate).
print(httpx.get(f"{base}/Patient/probe-patient").status_code)
EOF
```

Expected: 2xx both times, patient readable, no duplicates. **If the operation 404s** (CR module doesn't expose it on our HAPI version/config): STOP, report to Bill — the fallback envelope needs a different delivery (this invalidates part of the design and needs a decision, not a workaround).

- [ ] **Step 2: Write the integration test**

Create `backend/tests/integration/test_deqm_submit_data_workflow.py`, modeled directly on `tests/integration/test_full_workflow.py` (same `pytestmark`, same settings-patching context manager, same `_create_job` idea — copy its patch list verbatim):

```python
"""End-to-end test: deqm_submit_data workflow against real HAPI (base-fallback path)."""

from unittest.mock import patch

import httpx
import pytest

from app.models.job import Job, MeasureResult
from app.services.orchestrator import run_job
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration

MEASURE_ID = "CMS122FHIRDiabetesAssessGreaterThan9Percent"


def _run_patches(integration_session_factory):
    return (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.orchestrator.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.fhir_client.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.fhir_client.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MAX_RETRIES", 1),
        patch("app.services.orchestrator.settings.BATCH_SIZE", 100),
        patch("app.services.orchestrator.async_session", integration_session_factory),
    )


async def _create_and_run(integration_client, integration_session_factory, workflow: str) -> dict:
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": MEASURE_ID,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "cdr_url": TEST_CDR_URL,
            "workflow": workflow,
        },
    )
    assert resp.status_code == 201, f"Job creation failed: {resp.text}"
    job = resp.json()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _run_patches(integration_session_factory):
            stack.enter_context(p)
        await run_job(job["id"])
    detail = await integration_client.get(f"/jobs/{job['id']}")
    return detail.json()


async def test_deqm_job_records_base_fallback_mode(integration_client, integration_session_factory):
    """Bundled HAPI has no $deqm-submit-data, so the probe must record base-fallback."""
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": MEASURE_ID,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "cdr_url": TEST_CDR_URL,
            "workflow": "deqm_submit_data",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["workflow"] == "deqm_submit_data"
    assert body["submit_data_mode"] == "base-fallback"


async def test_deqm_job_matches_direct_load_populations(integration_client, integration_session_factory):
    """The DEQM workflow must produce the same populations as direct load."""
    direct = await _create_and_run(integration_client, integration_session_factory, "direct_load")
    deqm = await _create_and_run(integration_client, integration_session_factory, "deqm_submit_data")

    assert deqm["status"] == "complete", f"DEQM job failed: {deqm.get('error_message')}"
    assert direct["status"] == "complete", f"direct job failed: {direct.get('error_message')}"
    assert deqm["failed_patients"] == 0, "DEQM job had failed patients"

    # Compare stored populations per patient via the DB session factory.
    from sqlalchemy import select

    async with integration_session_factory() as session:
        rows_direct = (
            (await session.execute(select(MeasureResult).where(MeasureResult.job_id == direct["id"]))).scalars().all()
        )
        rows_deqm = (
            (await session.execute(select(MeasureResult).where(MeasureResult.job_id == deqm["id"]))).scalars().all()
        )
    pops_direct = {r.patient_id: r.populations for r in rows_direct}
    pops_deqm = {r.patient_id: r.populations for r in rows_deqm}
    assert set(pops_deqm) == set(pops_direct)
    for pid, pops in pops_direct.items():
        assert pops_deqm[pid] == pops, f"Population mismatch for {pid}"


async def test_deqm_submission_stored_on_mcs(integration_client, integration_session_factory):
    """The base $submit-data path stores the DEQM MeasureReport on the MCS."""
    from sqlalchemy import select

    deqm = await _create_and_run(integration_client, integration_session_factory, "deqm_submit_data")
    assert deqm["status"] == "complete", deqm.get("error_message")

    # Client-assigned MeasureReport id is deqm-{job_id}-{patient_id}; read one
    # back directly (direct reads bypass any index lag — CLAUDE.md triage rule).
    async with integration_session_factory() as session:
        row = (
            (await session.execute(select(MeasureResult).where(MeasureResult.job_id == deqm["id"]))).scalars().first()
        )
    assert row is not None
    async with httpx.AsyncClient(timeout=30.0) as client:
        direct_read = await client.get(f"{TEST_MEASURE_URL}/MeasureReport/deqm-{deqm['id']}-{row.patient_id}")
    assert direct_read.status_code == 200
    stored = direct_read.json()
    assert stored["type"] == "data-collection"
```

Adjust fixture names to the real conftest (`integration_client`, `db_session`, `integration_session_factory` per `tests/integration/test_full_workflow.py`).

- [ ] **Step 3: Run the new integration test explicitly**

Run: `./scripts/run-integration-tests.sh tests/integration/test_deqm_submit_data_workflow.py`
Expected: all PASS. Debug notes: if populations mismatch, the targeted-query gather missed a resource type — compare resource types on the MCS after each job (`GET {TEST_MEASURE_URL}/{Type}?_summary=count`) before touching code; remember the CLAUDE.md HAPI-indexing triage rule (direct read vs search) before chasing phantom bugs.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/integration/test_deqm_submit_data_workflow.py
git commit -m "test(integration): DEQM \$submit-data workflow end-to-end with population parity"
```

---

### Task 8: Full verification (mandatory pre-push checklist)

**Files:** none new — verification only.

- [ ] **Step 1: Lint** — `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` → clean.
- [ ] **Step 2: Unit suite** — `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v` → all pass.
- [ ] **Step 3: CI-equivalent integration suite** — `./scripts/run-integration-tests.sh --ignore=tests/integration/test_golden_measures.py --ignore=tests/integration/test_connectathon_measures.py --ignore=tests/integration/test_full_workflow.py --ignore=tests/integration/test_groups_dropdown.py --ignore=tests/integration/test_full_jobs_pipeline.py --ignore=tests/integration/test_factory_reset.py` → all pass (this run includes the new `test_deqm_submit_data_workflow.py`).
- [ ] **Step 4: Full workflow suite** (orchestrator/fhir_client touched) — `./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py` → all pass.
- [ ] **Step 5: E2E smoke** — `cp .env.example .env && docker compose up -d`; in the UI (localhost:3001) create one `direct_load` job and one `deqm_submit_data` job on the seeded measure; confirm both complete with identical populations, and the DEQM job shows the ⚠ fallback badge. Probe `$everything` on one patient afterwards per `docs/runbooks/everything-probe.md`.
- [ ] **Step 6: Frontend build + tests** — `cd frontend && CI=true npm test -- --watchAll=false && npm run build` → pass.
- [ ] **Step 7: Commit any fixes; do NOT push until 1–6 all pass.** If anything is red, fix locally or document the blocker instead of pushing.
