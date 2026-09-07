# Pre-landing review fixes — DEQM $submit-data

Branch: `feature/deqm-submit-data`, applied on top of `faaac49`.

## Per-fix summary

### F1 — Filter resources once, derive both MeasureReport and Parameters from it
`backend/app/services/workflows.py` (`DeqmSubmitDataWorkflow.transfer_patient`): added
`filtered_resources = [r for r in gather.resources if "resourceType" in r and "id" in r]`
before building the MeasureReport, and pass `filtered_resources` (not `gather.resources`)
into both `build_data_exchange_measure_report` and the `submitted` list used for the
Parameters envelope. `GatherResult` returned to the caller is unchanged (still the
original unfiltered gather) — only the two DEQM payloads are now derived from the same
filtered list.
Test added: `test_id_less_resource_excluded_from_submission_and_evaluated_resource`
(`test_services_workflows.py`) — asserts a resource missing `id` and one missing
`resourceType` are excluded from both the submitted `resource` parameters and
`evaluatedResource`.

### F2 — Forced Patient fetch no longer swallows non-200
`backend/app/services/fhir_client.py` (`_fetch_by_requirements`): a non-200 Patient
read now appends a `FailedResourceFetch(resource_type="Patient", ...)` to
`failed_types` so `GatherResult.has_partial_failure` is True and the failure is
attributable. Deliberately **not** added to `failed_type_names` (the set that drives
the "all REQUIRED types failed → trigger $everything fallback" check) — I found
during testing that `failed_type_names` is *also* populated, unconditionally, by
the pre-existing generic `except Exception` handler whenever the Patient read raises
an exception (as opposed to returning a non-200 status) — and an existing test
(`test_data_requirements_strategy_fetch_fails_falls_back_to_everything`) already
relies on that exception-path behavior counting Patient toward the "all failed" check
when Patient is a declared dataRequirement. Adding "Patient" to `failed_type_names` in
the *new* non-200 branch would have made that check fire in cases it doesn't
today (confirmed by first implementing it that way, running the suite, and watching
that test fail — reverted to the narrower fix). This keeps the "do NOT add Patient to
required_types" instruction intact without touching the unrelated exception path.
Test added: `test_data_requirements_strategy_patient_failure_reports_partial_gather`
(`test_services_fhir_client.py`) — Patient 404s, the one declared type (Condition)
succeeds; asserts `has_partial_failure is True`, `failed_types` names "Patient", and
no `$everything` fallback fires.

### F3 — Downgrade triggers widened to 400/404/405/501
`backend/app/services/workflows.py`: `_DOWNGRADE_STATUS_CODES = {400, 404, 405, 501}`.
Comment rewritten to explain the set (capability-mismatch signals only; 401/403
excluded as auth failures, 429/5xx excluded as transient/overload signals).
Tests added: parametrized `test_stu5_405_and_501_also_downgrade` (405, 501 downgrade)
and `test_stu5_auth_and_transient_failures_do_not_downgrade` (401, 403, 429, 500 do
NOT downgrade — raise immediately, mode stays "stu5").

### F4 — Fail fast on non-absolute canonical in STU5 mode
`backend/app/services/workflows.py` (`build_submission_workflow`): `get_measure_canonical`
itself is untouched (still degrades to `Measure/{id}` when the Measure has no `url`).
`build_submission_workflow` now computes `resolved_mode` up front and raises `ValueError`
when `resolved_mode == SUBMIT_DATA_MODE_STU5` and the canonical doesn't start with
`"http"`. Base-fallback mode is unaffected.
Tests added: `test_deqm_stu5_mode_raises_on_relative_canonical` and
`test_deqm_base_mode_tolerates_relative_canonical`.

### F5 — Job failure summary now covers submit/gather phases
`backend/app/services/orchestrator.py` (`run_job` finalize step): the aggregation query
now selects `error_phase` alongside `populations` and filters
`error_phase.in_(("evaluate", "submit", "gather"))` instead of `== "evaluate"`. A
`dominant_phase` is computed (most common phase among the failed rows); the existing
unknown-ValueSet special case is preserved for `dominant_phase == "evaluate"` only.
For `submit`/`gather`, the message is `"All N patient submissions failed: <first
sanitized error>"` / `"...data gathers failed: <first sanitized error>"` (falling
back to no trailing detail when no error_message is present). No new test added
for this one specifically — the existing tests (`test_all_patients_evaluation_failure_sets_job_error`
at line ~734 and the unknown-ValueSet test at ~825) exercise the "evaluate" branch
unchanged and both pass; F5 was not in the explicit "must add a test" list, and I
judged extending `test_services_orchestrator.py`'s heavier fixture-based `run_job`
integration-style tests to fabricate a submit-phase all-failed scenario as more risk
than value here, given time — flag this if you want one added.

### F6 — MeasureReport id charset guard
`backend/app/services/deqm.py` (`_measure_report_id`): added `_FHIR_ID_CHARSET_RE =
re.compile(r"^[A-Za-z0-9\-.]+$")`; the short-circuit now requires both length **and**
charset match (`len(candidate) <= 64 and _FHIR_ID_CHARSET_RE.match(candidate)`),
falling back to the existing sha256-hash form otherwise.
Tests added: `test_illegal_charset_patient_id_falls_back_to_hash` and
`test_illegal_charset_patient_id_is_stable_across_calls`.

### F7 — measure_id format validator
`backend/app/routes/jobs.py`: added `_MEASURE_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,256}$")`
and a `field_validator("measure_id")` on `JobCreate`, mirroring `validate_group_id`
exactly (same regex, same error-message style). Checked all existing test fixtures'
`measure_id` values (`measure-1`, `CMS122`, `CMS122FHIR...`, `M1`) — all match the
new pattern, no other test needed adjustment.
Test added: `test_create_job_path_bearing_measure_id_rejected` (`../../etc/passwd` → 422).

### F8 — sanitize_error in the capability probe log
`backend/app/services/fhir_client.py` (`detect_submit_data_mode`): replaced
`"error": str(exc)` with `"error": sanitize_error(exc)`. `sanitize_error` lives in
`app.services.validation`, which itself imports from `app.services.fhir_client` at
module scope — a top-level import would be circular, so the import is deferred inside
the `except` block (same pattern already used in `app/dependencies.py` for the
inverse direction). Verified with a plain `python3 -c "import app.services.fhir_client..."`
that no circular-import error surfaces.

### F9 — Accurate strategy log
`backend/app/services/orchestrator.py` (`_process_single_batch`): `strategy_label =
settings.PATIENT_DATA_STRATEGY if workflow.name == "direct_load" else
"data_requirements"`, logged instead of the raw env var unconditionally.

### F10 — Stale phase-1 comment
Same location: reworded the `# Phase 1: Gather all patient data and push to measure
engine` comment to describe `workflow.transfer_patient()` generically (direct_load
pushes a Bundle of PUTs; deqm_submit_data POSTs a $submit-data envelope).

### F11 — Hoisted repeated JSX expression
`frontend/src/pages/JobsPage.js`: added `const isFallback = job.submit_data_mode ===
'base-fallback';` once per row render, replacing all four inline occurrences
(className, title, aria-label, and the `⚠` suffix). Pure refactor — no rendered-output
change. Frontend unit tests (121) and `npm run build` both pass unchanged.

### F12 — Bounded conflict retry on $submit-data
`backend/app/services/fhir_client.py` (`submit_data`): restructured the single POST
into a 3-attempt loop (mirroring `evaluate_measure`'s retry style — `asyncio.sleep(0.5
* (attempt + 1))` backoff). On HTTP 409 or 412 with `attempt < 2`, logs a warning and
retries the *same* request; otherwise raises `FhirOperationError` as before. The
Organization stays inline in the payload — not touched.
Tests added: `test_409_conflict_retries_and_succeeds`, `test_412_conflict_also_retries`,
`test_409_conflict_exhausts_retries_and_raises` (3 attempts total, then raises with
`status_code == 409`).

## Verification output (verbatim)

### 1. Backend unit suite
Ran with `--ignore=tests/test_services_bundle_loader.py` because that file hangs on
this machine — confirmed independently with `timeout 60 python3 -m pytest
tests/test_services_bundle_loader.py -q`, which timed out (exit 124) rather than
completing, matching the known issue noted in the task.

```
$ cd backend && python3 -m pytest tests/ --ignore=tests/integration --ignore=tests/test_services_bundle_loader.py -q
........................................................................ [ 10%]
........................................................................ [ 20%]
........................................................................ [ 31%]
........................................................................ [ 41%]
........................................................................ [ 51%]
........................................................................ [ 62%]
........................................................................ [ 72%]
........................................................................ [ 82%]
........................................................................ [ 93%]
...............................................                          [100%]
=============================== warnings summary ===============================
tests/test_services_orchestrator.py::test_run_job_passes_job_fields_to_build_submission_workflow
  .../app/services/orchestrator.py:79: RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
    for group in measure_report.get("group", []):
  Enable tracemalloc to get traceback where the object was allocated.
  See https://docs.pytest.org/en/stable/how-to/capture-warnings.html#resource-warnings for more info.

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
695 passed, 1 warning in 14.16s
```

695 = 679 pre-existing + 16 new (1 F1 + 1 F2 + 6 F3 + 2 F4 + 2 F6 + 1 F7 + 3 F12).
The one warning is pre-existing (unrelated `AsyncMock` coroutine-not-awaited noise in
an existing test), not a failure.

### 2. Frontend tests + build

```
$ cd frontend && CI=true npm test -- --watchAll=false
Test Suites: 13 passed, 13 total
Tests:       121 passed, 121 total
Snapshots:   0 total
Time:        2.254 s
Ran all test suites.
```
(Console shows pre-existing React `act()` warnings in `GroupsPage`/`ConnectionContext`
tests — not failures, not touched by this change.)

```
$ cd frontend && npm run build
Compiled successfully.

File sizes after gzip:
  100.64 kB  build/static/js/main.b6070bbf.js
  18.83 kB   build/static/css/main.0be25dda.css
The build folder is ready to be deployed.
```

### 3. Lint

```
$ cd backend && ruff check app/ tests/
All checks passed!
$ cd backend && ruff format --check app/ tests/
78 files already formatted
```

## Not fixed / deviations

- **F2**: implemented with one deliberate deviation from the literal fix text — the
  new Patient-failure record is added to `failed_types` (for `has_partial_failure`)
  but NOT to `failed_type_names` (the set used in the "all required types failed"
  check), because `failed_type_names` is also populated by the pre-existing
  generic-exception path for Patient, and an existing test
  (`test_data_requirements_strategy_fetch_fails_falls_back_to_everything`) depends on
  that pre-existing behavior counting a Patient-exception failure toward the "all
  failed" trigger when Patient is a declared requirement. Naively adding "Patient" to
  `failed_type_names` in the new non-200 branch broke that test by changing when the
  `$everything` fallback fires — exactly what the instruction said not to do. The
  implemented fix achieves the stated goal (partial-failure reporting via
  `has_partial_failure`) without that side effect. All 695 tests pass with this
  version, including the new dedicated F2 test.
- **F5**: no new test added beyond the existing evaluate-phase regression tests
  (which still pass with the widened aggregation). Judged lower-value/higher-effort
  to fabricate a full `run_job` submit-phase-all-failed fixture given the other 6
  explicitly-required tests; flagging this as a gap rather than silently skipping it.
- All other fixes (F1, F3, F4, F6, F7, F8, F9, F10, F11, F12) applied exactly as
  specified, with tests added per the instructions.
- `direct_load` invariant: not touched by any of F1, F3, F4, F5 (wording only for
  evaluate-phase unaffected), F6, F7 (validator applies to both workflows equally,
  as intended — measure_id format is not workflow-specific), F8, F9, F10, F11, F12
  (submit_data is DEQM-only). F2 touches the shared `_fetch_by_requirements` method
  used by `DataRequirementsStrategy` for both workflows when
  `PATIENT_DATA_STRATEGY=data_requirements` — verified the change does not alter
  `gather.resources` (what actually gets pushed) in any case, only adds an
  additional `failed_types` entry for partial-failure *reporting* on a
  previously-silent Patient-read failure. This is a behavior addition (better
  reporting) shared by both workflows, not a change to what direct_load pushes or
  evaluates against.
