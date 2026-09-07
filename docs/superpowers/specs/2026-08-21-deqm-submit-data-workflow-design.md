# Design: Selectable Data Submission Workflows (DEQM `$submit-data`)

**Date:** 2026-08-21
**Status:** Approved (design reviewed section-by-section with Bill on 2026-08-21)

## Context

Lenny's job pipeline moves patient data from the CDR to the MCS one way: per-patient
`Patient/$everything` → batch Bundle of PUTs pushed to the MCS → per-patient
`Measure/$evaluate-measure`. This feature lets the user choose a **data submission
workflow at job creation time**. The first additional workflow uses the **DEQM STU5
`$submit-data` operation** with the **DEQM Data Exchange MeasureReport profile**
(https://hl7.org/fhir/us/davinci-deqm/STU5/).

## Decisions

1. **Job outcome:** submit, then evaluate — jobs still end with per-patient
   `$evaluate-measure` and show population results. Only the transfer step changes.
2. **CDR gathering for the DEQM workflow:** targeted queries driven by the measure's
   data requirements (not `$everything`).
3. **MCS targets:** design for any configured MCS; v1 tests/guarantees the bundled
   engine only ("bundled + best-effort external").
4. **Wire format:** support both — probe the MCS CapabilityStatement and send STU5
   `$deqm-submit-data` (bundle parameter) where advertised, else fall back to base
   FHIR `$submit-data` (measureReport + resource), **loudly surfacing the fallback
   to the user as an STU5-compliance warning on the job**. HAPI's clinical-reasoning
   module only implements the base shape today, so the bundled engine will always be
   in fallback mode.
5. **Selection model:** one per-job `workflow` selector; each workflow is a strategy
   class owning both gather and delivery.
   - `direct_load` (default): `$everything` + PUT push — today's behavior, unchanged.
   - `deqm_submit_data`: targeted queries + DEQM MeasureReport + `$submit-data`.

## Research findings the design relies on

- Existing reusable pieces: `DataAcquisitionStrategy` ABC with `BatchQueryStrategy`
  and `DataRequirementsStrategy` (targeted queries) in
  `backend/app/services/fhir_client.py`; orchestrator phases in
  `backend/app/services/orchestrator.py` (`run_job`, `_process_single_batch`).
- `DataRequirementsStrategy._get_data_requirements` hardcodes
  `settings.MEASURE_ENGINE_URL` — must be fixed to use the job's MCS + credentials.
- STU5 defines `POST [base]/Measure/$deqm-submit-data` with `bundle` (1..*)
  Parameters; each bundle SHOULD be single-subject and must contain the MeasureReport
  **and all referenced data-of-interest** (references are never resolved outside the
  submission).
- DEQM Data Exchange MeasureReport (profile `datax-measurereport-deqm`): required
  `submitDataUpdateType` extension (`snapshot` | `incremental`), `type` fixed to
  `data-collection` (the R4 wire code for "data-exchange"), `subject` 1..1,
  `date` 1..1, `reporter` 1..1 (Organization), `period` 1..1 at day precision,
  `group.measureScore`/`stratifier` prohibited, `evaluatedResource` must-support.
- HAPI clinical-reasoning implements only base `Measure/$submit-data`
  (`measureReport` 1..1 + `resource` 0..*), at type and instance level; it stores the
  submitted resources into the server.

## Section 1 — Backend workflow abstraction

New module `backend/app/services/workflows.py` with a `SubmissionWorkflow` strategy
the orchestrator calls per patient in phase 1 (replacing inline gather +
`push_resources`):

- **`DirectLoadWorkflow`** (`direct_load`, default):
  `BatchQueryStrategy.gather_patient_data` then `push_resources` — byte-for-byte
  today's behavior; zero change for existing jobs.
- **`DeqmSubmitDataWorkflow`** (`deqm_submit_data`): `DataRequirementsStrategy`
  (fixed to target the job's MCS for `$data-requirements`), then build the DEQM
  Data Exchange MeasureReport and deliver via `$submit-data`.

Additional backend points:

- Capability detection runs once in the existing `POST /jobs` pre-flight: fetch the
  MCS CapabilityStatement; `deqm-submit-data` on Measure present → `stu5`, absent →
  `base-fallback` + compliance warning surfaced on the job.
- MCS `wipe_patient_data` list gains `MeasureReport` so submitted data-collection
  reports don't accumulate across jobs.
- The `PATIENT_DATA_STRATEGY` env var stays as-is (controls the default workflow's
  acquisition only); no removal this release.
- Wipe phase and per-patient `$evaluate-measure` phase are shared and untouched.

## Section 2 — DEQM submission construction & wire formats

Per patient, `DeqmSubmitDataWorkflow`:

1. **Gather:** `DataRequirementsStrategy.gather_patient_data` — targeted CDR queries
   from the measure's `$data-requirements` (existing `$everything` fallback retained).
2. **Build the DEQM Data Exchange MeasureReport** (one per patient):
   - `meta.profile` = `http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/datax-measurereport-deqm`
   - `extension[submitDataUpdateType]` = **`snapshot`** (Lenny wipes the MCS at job
     start; `incremental` needs stable ids + `meta.source` on all resources — v2)
   - `status` = `complete`; `type` = `data-collection`
   - `measure` = canonical `url|version`, fetched once per job from `{mcs}/Measure/{id}`
   - `subject` = `Patient/{patient_id}`; `date` = run timestamp; `period` = job period
   - `reporter` = `Organization/lenny-reporter` — fixed synthetic Lenny Organization,
     included in every submission (DEQM requires reporter 1..1)
   - `evaluatedResource` = references to every submitted resource
   - No `group`/`measureScore`/`stratifier` (prohibited by the profile)
   - Client-assigned id `deqm-{job_id}-{patient_id}` for traceability
3. **Deliver** — identical content, two envelopes:
   - **STU5 mode:** `POST {mcs}/Measure/$deqm-submit-data`; Parameters with one
     `bundle` param = single-subject collection Bundle (MeasureReport + Organization +
     all data-of-interest).
   - **Fallback mode:** `POST {mcs}/Measure/$submit-data`; Parameters with
     `measureReport` (1..1) + repeated `resource` params — the shape HAPI
     clinical-reasoning implements. Any 2xx = success.

**Capability probe rule** (once, at `POST /jobs` pre-flight): GET `{mcs}/metadata`;
if `rest.resource[type=Measure].operation[]` contains name `deqm-submit-data` or a
definition matching the DEQM canonical
`http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data` → `stu5`;
otherwise (including probe failure when the measure pre-flight succeeded) →
`base-fallback` + compliance warning recorded on the job.

## Section 3 — API / DB / frontend surface

**Backend API & DB**

- `JobCreate` (`backend/app/routes/jobs.py`): optional `workflow` field, validated
  against `{direct_load, deqm_submit_data}`, default `direct_load` — existing clients
  unchanged.
- `Job` model: new columns `workflow` (text, default `direct_load`) and
  `submit_data_mode` (text, nullable; `stu5` | `base-fallback`, set only for DEQM
  jobs during pre-flight). Migration via the existing `ALTER TABLE … ADD COLUMN IF
  NOT EXISTS` pattern in `backend/app/main.py:_run_schema_migrations`.
- `JobResponse` exposes `workflow` and `submit_data_mode`.
- `MeasureResult.error_phase` gains a `submit` value (delivery failures; Section 4).

**Frontend (`frontend/src/pages/JobsPage.js`)**

- "New calculation" modal: new **"Data submission workflow"** select between the
  patient-group select and `PeriodPicker`. Options: "Direct load — $everything
  (default)" and "DEQM Data Exchange — $submit-data (STU5)". Value included in the
  `createJob` POST body.
- Jobs table/detail: show the workflow; when `submit_data_mode === 'base-fallback'`,
  a visible warning badge: "MCS does not support DEQM STU5 $deqm-submit-data — base
  $submit-data fallback used." Shown from creation time (mode decided in pre-flight).
- Update `JobsPage.test.js` and `JobsPage.measureReset.test.js`.

## Section 4 — Error handling & testing

**Error handling**

- Gather failures: unchanged (`error_phase = gather` / `gather_partial`), both
  workflows; partial gathers still submit what was collected.
- Submit failures: non-2xx / OperationOutcome from `$submit-data` → `MeasureResult`
  with new `error_phase = 'submit'`, redacted OperationOutcome in `error_details`
  (reuse `fhir_errors.FhirOperationError` / `redact_outcome`); evaluation skipped for
  that patient; existing batch retry-with-backoff applies. Snapshot semantics +
  client-assigned ids should make retries idempotent — **implementation checkpoint:
  verify HAPI `$submit-data` upserts (not duplicates) on re-submit.**
- Measure canonical unreadable from `{mcs}/Measure/{id}` at run start → job fails fast.
- Capability probe failure → `base-fallback` + warning (never blocks job creation if
  the measure pre-flight succeeded).

**Testing**

- Unit: DEQM MeasureReport builder (required elements, snapshot extension, prohibited
  group/score), capability-probe parsing (stu5 / fallback / probe-error), workflow
  factory + `JobCreate` validation, orchestrator dispatch with mocked workflows.
- Integration (new files run explicitly — the CI-equivalent runner won't pick them
  up): DEQM job vs bundled HAPI asserting: `submit_data_mode = base-fallback`
  recorded; submitted MeasureReport + resources stored on MCS; **populations match a
  `direct_load` job on the same measure/patients** (guards targeted-query gaps vs
  `$everything`).
- STU5 envelope: mocked-server test asserting the `$deqm-submit-data` Parameters shape.
- Touches `orchestrator.py`/`fhir_client.py` → per CLAUDE.md decision tree: full
  workflow suite (`test_full_workflow.py`), jobs-pipeline validation, and
  docker-compose e2e smoke before push, plus lint/unit/CI-equivalent integration.
