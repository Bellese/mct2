# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0.0] - 2026-09-03

### Added
- **You can now choose how a calculation delivers patient data to the measure engine.** The "New calculation" form has a new "Data submission workflow" dropdown with two options. **Direct load** is the default and is exactly what Lenny always did: gather each patient with `$everything` and push the resources straight onto the measure server. **DEQM Data Exchange** instead delivers each patient through the FHIR `$submit-data` operation, the mechanism the DEQM implementation guide defines for handing measure data to a receiving system. Nothing changes for existing calculations — leave the dropdown alone and you get the same behavior and the same numbers as before. (#50)
- **Lenny checks what your measure server actually supports before using it.** When you pick the DEQM workflow, Lenny probes the measure at creation time and records which wire format that server accepts: the STU5 operation if the measure advertises it, or the base `$submit-data` operation if not. In practice every server tested so far takes the base path, so that is the route this release is verified on; the STU5 route is not yet exercised against a real server. (#413) The decision is pinned to the job, so a job that starts under one server's capabilities does not silently change format if you switch connections while it waits in the queue.
- **A job that fell back to the older format says so.** Jobs run with the DEQM workflow carry a `DEQM` tag in the Jobs list. If the server could not do STU5 and Lenny used base `$submit-data` instead, the tag shows a warning marker and explains why on hover, and a toast tells you at the moment you start the calculation. The explanation is on the element's accessible label too, not only the tooltip, so it is available to screen readers rather than sighted mouse users only.
- **Patient data submission failures now have their own error message.** A patient whose data could not be delivered to the measure server used to fall under a generic "Unknown error" in Results. It now reads "Couldn't submit patient data to the measure server", distinguishing a delivery failure from a gather failure or a calculation failure.

### Changed
- **The DEQM workflow fetches only what the measure declares it needs.** Rather than pulling everything about a patient, it asks the measure for its `$data-requirements` and fetches those resource types. This is what DEQM expects — a submission is meant to be self-contained — and it means less data crosses the wire per patient. It also means the reference graph can be less complete than `$everything` produced; see the known limitation below.
- **The bundled local and CI measure engine now gets 8 GB instead of 6 GB.** `-Xmx4g` caps the Java heap but not the JVM's native footprint (Lucene indexes, metaspace, direct buffers, thread stacks), and back-to-back DEQM submissions pushed total memory past the old 6 GB ceiling. The kernel then killed the process outright — no Java error, no heapdump, just a connection failure. The heap stays at 4 GB; the container simply has room around it. This affects local development and CI only.

### Fixed
- **Re-running a DEQM calculation now re-runs it as DEQM.** The Re-run button on the Results page rebuilt the job from the original's measure, cohort and period but not its submission workflow, so the backend default took over and you silently got a direct-load job instead — same numbers to look at, different delivery path, and nothing on screen saying so. Re-run now carries the original workflow.
- **"Delete all patient data before each job" no longer breaks DEQM submissions.** DEQM requires every submission to name a reporting organization, and Lenny stores that organization on the measure server once per job. It was being stored *before* the pre-job wipe, and the wipe deletes organizations — so on any job with that option ticked, every patient's submission referenced an organization that had just been deleted. Because `$submit-data` is processed as a transaction, a broken reference fails the whole submission for that patient. The organization is now stored after the wipe.

### Known limitations
- **DEQM submissions are all-or-nothing per patient.** `$submit-data` is processed as a FHIR transaction, unlike the direct-load path which deliberately uses a batch so unresolvable cross-references cannot reject unrelated entries. One reference the receiver cannot resolve therefore fails that patient's entire submission, where direct load would have accepted everything else and recorded only the bad entry. Combined with the narrower targeted fetch above, the DEQM path is the more exposed of the two. Direct load remains the default and is unaffected. (#409)
- **Three narrower issues in the DEQM path are known and deferred**, none of which affect direct load: a rejected submission can be misread as "server doesn't support STU5" and silently switch the job's wire format mid-run while the on-screen badge keeps claiming the original (#414); a rejection returned inside an HTTP 200 response is currently counted as a successful transfer (#415); and `direct_load` behaves differently than before if you have set `PATIENT_DATA_STRATEGY=data_requirements`, which is not the default (#416).
- **DEQM has not been exercised at production's memory settings.** The workflow is verified against the local and CI measure engine, which now runs an 8 GB container around a 4 GB heap. Production runs that same server at 4 GB around a 2 GB heap — half the heap the memory failure above was observed with — and raising it is not a change this release can safely make, because production is a single 8 GiB host already declaring 7 GB across its two FHIR servers. If a DEQM job does exhaust memory there, the failure is a kernel kill with no Java error and no heapdump, and it takes down the measure engine shared by every other job. Sizing that host is tracked separately. (#412)

## [0.0.22.0] - 2026-08-20

### Fixed
- **Validation now runs against the measure engine connection you selected.** It always ran against Lenny's own local container, no matter which MCS connection was active — so "validate my measures" quietly answered a question about a different server than the one in front of you. Uploading a test bundle, reloading measures, pushing patient data and evaluating each patient all now go to the connection you chose, with its credentials. (#397)
- **A validation run no longer deletes other people's patient data.** Every run began by deleting *all* patients from the measure engine. That was invisible while it only ever hit the local container; pointing validation at a shared or connectathon server would have wiped every other participant's test data with no prompt and no undo. A run now deletes only the patients it is about to evaluate. Your numbers do not change: evaluation is per-patient, so patients a run never evaluates could never have affected its results. Connections with "delete all patient data before each job" ticked keep the full wipe, same as jobs. (#397, same bug class as #392)
- **A queued validation run sticks to the connection it was started on.** Now that runs follow the connection you selected (above), which one that is has to be pinned when you press Start: each run records it internally — name, URL, and whether credentials were involved — and executes against it even if you switch connections while the run waits in the queue. Runs no longer store nothing about where they ran, so a stored pass/fail result is no longer a claim about correctness with nothing attached saying which server produced it. That record is not visible yet, though: the runs list and run detail views still do not show the server, so reading it back today means querying the database. Surfacing it in the API and the UI is follow-up work. (#397)

### Changed
- **A read-only measure engine connection now refuses validation instead of ignoring the flag.** Validation has to write measures, terminology and patient data to the engine to produce a result, so it can no longer run against a connection you have marked read-only: uploading a test bundle is rejected before anything is written, and starting a run fails immediately with the reason on screen. **This is a behavior change.** If you marked your measure engine read-only and still expect validation to run, it will now stop. Switch to a writable connection, or clear the read-only flag. The alternative was a run that half-writes to a server you asked Lenny not to write to. (#397)

## [0.0.21.0] - 2026-08-19

### Fixed
- **Admin controls now act on the measure engine you are actually connected to.** "Wipe measure engine" and the measure-engine half of Factory Reset both targeted Lenny's own local container regardless of which MCS connection was active. Connected to a remote server, you got a success message while the server in front of you kept all its data — and the local one silently lost its measure definitions. Both now follow the active connection and send its credentials. (#397)
- **Comparison view says what actually went wrong.** When Lenny could not resolve a job's measure on the measure engine, the Results comparison returned an empty result, which read as "No expected results available for this measure and period. Load a connectathon bundle via Settings" — advice that sends you to load data you already have. It now reports the real problem, and distinguishes an unreachable server from one that rejected the request for credentials. A measure that genuinely is not on the server still shows the empty state, because that is not an error. (#397)
- **A job's comparison and evaluated-resource views resolve against the server that job ran on.** Both read the environment default, so for a job that ran against a remote MCS they queried the wrong server and returned nothing useful. (#397)
- **`$data-requirements` asks the job's own measure engine.** It asked the local engine what a remote job's measure needed, then quietly fell back to fetching everything when the answer was wrong or the call was rejected. Only affects installs running `PATIENT_DATA_STRATEGY=data_requirements`. (#397)

### Added
- **Read-only connections are now respected by destructive admin operations.** Marking a connection read-only previously did nothing for these paths: "Wipe measure engine" would wipe a server you had flagged, and Factory Reset ignored the flag on both the CDR and the measure engine. The admin wipe now refuses outright, and Factory Reset skips that step, names the reason on screen, and still completes its other steps. (#397)

## [0.0.20.0] - 2026-08-19

### Fixed
- **Running a job against a shared measure server no longer deletes other people's patient data.** Every job used to begin by deleting all patients from the target measure engine — correct for Lenny's own container, destructive once you point Lenny at a connectathon or shared server, where it silently removed every other participant's test data with no prompt and no way to undo. Jobs now delete only the patients they are about to evaluate. Measure results are unchanged: evaluation is per-patient, so patients a job never evaluates could never have affected its numbers. (#392)
- **Wiping now works against measure servers that reject bulk deletes.** When a server refuses a conditional delete that matches multiple resources (`allow_multiple_delete` disabled, which Lenny's own containers enable but a shared server may not), Lenny falls back to deleting each resource individually instead of reporting success having deleted nothing. (#392)

### Added
- **"Delete all patient data before each job" setting on each measure engine connection.** Off for every connection you create, so a shared server is safe by default. Ticking it shows a warning naming exactly what it will do, and any connection with it enabled carries a "full wipe" badge in the connections list. The built-in Local Measure Engine keeps its existing full-wipe behavior with no action needed from you. (#392)

### Changed
- **The pre-job cleanup now runs after Lenny gathers patients, instead of at job start.** It needs to know which patients to clean. One visible consequence: a job that finds zero patients no longer clears the local measure engine as a side effect. (#392)

## [0.0.19.3] - 2026-06-09

### Fixed
- **Bundle loader no longer fails with 409 Conflict when the dev stack is running.** `_delete_existing_valueset` now treats HTTP 409 (referential integrity — Measure/Library resources already reference the ValueSet) as a no-op instead of calling `raise_for_status()`. The ValueSet is left in place and the subsequent PUT updates it normally. Previously, starting the test suite or re-running the seed service while the dev stack was up caused the entire bundle load to abort. (#359)
- **`run-integration-tests.sh` aborts early when the dev stack is already running.** Docker Compose derives the project name from the working directory, so `docker-compose.yml` and `docker-compose.test.yml` share containers. The script now detects a running dev stack and exits with a clear message (`docker compose down -v` first). (#359)
- **`test_smart_load.py` `loader_result` fixture truncates `expected_results` before loading.** Prevents `uq_measure_patient` UniqueViolationError when the dev seed has already written rows for the same measure+patient pairs. (#359)

## [0.0.19.2] - 2026-05-18

### Security
- **Dependabot alert #29 (GHSA-58qx-3vcg-4xpx / CVE-2026-45736) resolved.** Bumped `ws` from `8.20.0` to `8.20.1` for `webpack-dev-server` via a scoped npm override in `frontend/package.json`. Fixes uninitialized memory disclosure in `ws.close()` when a `TypedArray` is passed as the reason argument. `jsdom`'s `ws@7.5.10` is unaffected (different major version, not in the vulnerable range). No API or behavioral changes.

## [0.0.19.1] - 2026-05-18

### Fixed
- **Jobs / Measures / Results: table content no longer clipped at narrow viewports.** Added an inner `tableScroll` wrapper (`overflow-x: auto`) inside each table card so a horizontal scrollbar appears when the viewport is between 820px and ~1100px — rightmost columns (Status, Duration, Actions) are now reachable by scroll instead of being silently hidden. Fixes the Duration column appearing missing on prod for users with narrower windows.
- **Jobs / Measures: Measure column now has a 260px minimum width.** Long CMS measure names like "Diabetes: Glycemic Status Assessment Greater Than 9%" no longer squeeze to 5–6 lines; they render in ≤2 lines at typical desktop widths.
- **Jobs: Period column wraps cleanly.** Each date value stays on one line; the break only happens at the " – " separator between start and end dates.
- **Jobs: Cohort column truncated at 145px.** Prevents long camelCase cohort identifiers from pushing the table past the card width.
- **Mobile stacked layout unaffected.** The `@media (max-width: 820px)` stacked-card treatment on all three pages is preserved exactly.

## [0.0.18.1] - 2026-05-18

### Added
- **CloudWatch Alarms.** Two alarms in the `Leonard` CloudWatch namespace notify `leonard-alerts` SNS → email on: Caddy 5xx errors (>5 in a 5-minute window) and backend ERROR-level log events (any in a 5-minute window). Both use `treat-missing-data: notBreaching` to avoid noise during quiet periods.
- **CloudWatch Logs metric filters.** `caddy-5xx-errors` on `/leonard/caddy` (pattern `{ $.status >= 500 }`) and `backend-error-logs` on `/leonard/backend` (pattern `{ $.level = "ERROR" }`) feed the `Leonard` custom metric namespace.
- **`bootstrap-aws.sh` extended.** Steps 9–13 create the SNS topic, email subscription, metric filters, and alarms idempotently.
- **`iam/leonard-cli-alarms-addendum.json`** documents the additional `leonard-cli` permissions needed to run steps 9–13 (SNS + metric filter + alarm write actions).

## [0.0.18.0] - 2026-05-18

### Fixed
- **CMS122 comparison panel always showed `has_expected: false`.** The comparison endpoint resolved the CMS122 measure by its FHIR resource ID, which in the prebaked image was the abbreviated `CMS122FHIRDiabetesAssessGT9Pct` (from `seed/measure-bundle.json`) while `ExpectedResult` rows were keyed to the full MADiE canonical `CMS122FHIRDiabetesAssessGreaterThan9Percent` (from the connectathon bundle). Both IDs coexisted silently in HAPI. Fix: `seed/measure-bundle.json` now uses the full MADiE canonical IDs so Phase 1 and Phase 2 of the bake load the same resource. A defense-in-depth guard (`_assert_no_canonical_url_clash`) raises immediately when any upload would introduce canonical-URL drift, preventing recurrence. (#319)

## [0.0.17.19] - 2026-05-18

### Added
- **CloudWatch Logs monitoring.** All five production services (caddy, backend, hapi-fhir-cdr, hapi-fhir-measure, frontend) now ship container logs to AWS CloudWatch Logs via the `awslogs` Docker driver. Log groups `/leonard/{caddy,backend,hapi-cdr,hapi-measure,frontend}` with 90-day retention. Non-blocking mode with 16 MB in-memory buffer prevents log writes from blocking container I/O. `docker logs` is replaced by `aws logs filter-log-events` for these five services after deploy; `db` and `seed` retain the default json-file driver.
- **Caddy structured JSON access logging.** Both vhosts (`{$CADDY_HOST}` and `api.{$CADDY_HOST}`) emit structured JSON access logs to stdout, enabling CloudWatch Logs Insights queries on `request.method`, `request.uri`, `status`, `duration`, and `request.remote_ip`.
- **IAM policy `CloudWatchLogsWrite`.** Least-privilege inline policy on `leonard-ec2-prod` role: `CreateLogGroup`/`DescribeLogGroups` scoped to log-group ARN, stream write actions scoped to stream ARN.
- **Config validation CI job.** New `config-validation` job in `pr-checks.yml` validates Caddyfile syntax (via `caddy validate`), `docker-compose.prod.yml` overlay resolution (via `docker compose config --quiet`), and IAM policy JSON (via `jq`).
- **CloudWatch Logs runbook.** `docs/runbooks/cloudwatch-logs.md` documents log group table, IAM setup, retention, log viewing commands, and CloudWatch Logs Insights queries.
- **`bootstrap-aws.sh` extended.** Steps 4c–4d apply the CloudWatch inline policy and pre-create log groups with retention idempotently, so DR rebuilds and new environments start with the full logging stack configured.

## [0.0.17.18] - 2026-05-18

### Removed
- **Retired legacy `98-89-219-217.nip.io` redirects.** The 301-redirect Caddy blocks that forwarded the legacy nip.io URLs to `lenny.bellese.dev` during the two-week soft-cutover window (issue #264 Phase 5) are removed. Clients hitting the old URLs now receive a TLS certificate error rather than a redirect. Archived deployment design and plan docs updated to reference `lenny.bellese.dev` throughout.

## [0.0.17.17] - 2026-05-17

### Added
- **Experimental Groups page.** Admin-gated UI at `/groups` lists CQL-evaluatable `Group` resources on the active CDR (filtered to those carrying a `characteristicExpression` extension with `text/cql*` language) and invokes the CQL IG `Group/<id>/$evaluate` operation per row. Results — name, id, gender, birth date for each resolved member — render in an inline accordion. Default `groups_enabled=false`; toggle in Settings → Developer Tools. Architecturally independent from the Measure pipeline (asserted by `tests/test_groups_independence.py`). Requires a CDR that implements the operation; the bundled HAPI image does not. (#322)

## [0.0.17.16] - 2026-05-17

### Added
- Per-CDR `max_bundle_entries` setting that lets `push_resources()` partition clinical-data pushes into chunks of ≤N entries. Supports CDRs that enforce a per-bundle entry cap (e.g., Firely Sandbox = 200). Default `null` = single-shot push (existing behavior). (#321)

## [0.0.17.15] - 2026-05-11

### Security
- **Resolved 3 additional Dependabot alerts via npm overrides.** `serialize-javascript` 6.0.2→7.0.5 (closes alerts #9 high-severity RCE and #22 medium-severity CPU-exhaustion DoS); `underscore` 1.13.6→1.13.8 (closes alert #10 high-severity unlimited-recursion DoS); `http-proxy-agent` 4.0.1→7.0.2 (drops `@tootallnate/once` dep entirely, closing alert #11 low-severity incorrect control flow). All overrides are build/test-time only; no runtime impact on the deployed app.
- **Dismissed webpack-dev-server alerts #7 and #8** as "vulnerable code not in production." No 4.x patch exists; 5.x would silently break `npm start` while CI passes. Production image serves static build output via `serve@14` — webpack-dev-server never runs in deployment. Decision documented in `docs/decisions.md` (ADR-008).

## [0.0.17.14] - 2026-05-11

### Security
- **Resolved 5 Dependabot alerts in frontend dependencies.** Upgraded transitive npm packages via `overrides` in `frontend/package.json`: `fast-uri` 3.1.0→3.1.2 (two CVEs: host confusion, path traversal), `@babel/plugin-transform-modules-systemjs` 7.29.0→7.29.4 (arbitrary code generation), `nth-check` 1.0.2→2.1.1 (ReDoS via SVG processing), `postcss` 7.0.39→8.5.14 (line return parsing error in nested copy). All build-time deps; no runtime impact. `npm run build` and all 69 frontend tests continue to pass.

## [0.0.17.13] - 2026-05-11

### Changed
- **Favicon redesigned.** Replaces the PNG/ICO favicon stack with a single SVG — a white "L" mark centered on the app's accent blue (`#3760c9`). Scales cleanly at all sizes. Old `.ico`, `.png`, and manifest links removed from `index.html`.

## [0.0.17.12] - 2026-05-08

### Added
- **Favicon.** Lenny now shows a branded favicon across all browsers and devices — a heart + bar chart icon in Lenny blue. Includes `favicon.ico` (16/32/48px), `favicon-32x32.png`, `favicon-16x16.png`, `apple-touch-icon.png` (180×180 for iOS home screen), and a `lenny-icon-512.png` for future use.

## [0.0.17.11] - 2026-05-08

### Added
- **GitHub repo link in sidebar footer.** A `github.com/Bellese/Lenny` link now appears below the version string in the bottom-left sidebar — surfacing the open-source nature of the project without requiring users to open Settings. The link opens in a new tab with proper `noopener noreferrer` attrs. Hover and focus states raise opacity and underline the link for discoverability.

### Fixed
- **Stale `v0.0.3` version string corrected.** The sidebar footer now reads the version from `frontend/package.json` at build time (`pkg.version`) instead of a hardcoded constant, so the displayed version stays in sync with each release.

### Fixed
- **"Test connection" on the seeded Local CDR and "Verify with sample evaluate" on the seeded Local MCS now succeed in local Docker.** Both flows previously returned `SSRF protection: must use https for non-localhost hosts` because `_validate_ssrf_url`'s http allowlist was just `{localhost, 127.0.0.1, ::1}` — the seeded connections use Docker service hostnames (`hapi-fhir-cdr`, `hapi-fhir-measure`) baked into `DEFAULT_CDR_URL` / `MEASURE_ENGINE_URL`. The allowlist now extends with hosts parsed from those settings at import time. Arbitrary http hosts and private/loopback IP literals remain blocked. (#302)

## [0.0.17.10] - 2026-05-08

### Changed
- **README Quick Start simplified.** Removed stale GHCR auth (`docker login`) and `CDR_FERNET_KEY` setup steps from the two-command quickstart. GHCR images are public; `CDR_FERNET_KEY` is only needed when saving custom CDR credentials via Settings.
- **README resource baselines re-baselined to 7 active measures.** Updated from pre-removal counts (Patient: 568, Measure: ≥12, Library: 24, ValueSet: ≈123) to post-removal actuals (Patient: 319, Measure: 8 [7 connectathon + 1 seed], Library: 18, ValueSet: 78). Documents that 5 measures were removed 2026-05-06 (issue #278).
- **"12 bundles" language updated to count-agnostic form** in `.env.example`, `.github/workflows/connectathon-measures.yml`, `scripts/inventory-bundles.py`, `backend/tests/integration/test_smart_load.py`, and `backend/app/services/validation.py` so the count does not drift when measures are added or removed.

## [0.0.17.9] - 2026-05-08

### Added
- **Validation tab is now opt-in.** The Validation nav item is hidden by default and appears only after an admin enables it via Settings → Admin → Features → Validation toggle. Existing deployments that relied on the previous always-on default are preserved by a startup migration that seeds `validation_enabled = true` with `ON CONFLICT DO NOTHING`, so no admin intervention is needed on upgrade.
- **In-app explanation of what Validation does.** The page header subtitle now describes the mechanism in plain language: Lenny loads a FHIR test bundle (test patients plus expected population counts), re-runs `$evaluate-measure` against each patient, and flags mismatches. Previously the subtitle described the goal without the how.
- **KPI card captions.** Each of the three headline numbers on the Validation tab now has a one-line caption clarifying what it counts: "Test bundles available to validate against", "Patients tested across all runs", and "Test patients whose populations matched expected, across all runs".
- **Admin settings test coverage.** `GET /settings/admin` and `PUT /settings/admin` now have 4 unit tests covering the no-row default, enable/disable round-trip, and empty-body no-op.

### Fixed
- **Smushed action buttons in the Validation page header.** The "Upload bundle" and "New run" buttons were compressed by the longer subtitle text. Fixed by adding `flex-shrink: 0` and `margin-left: 24px` to `.headerActions`; the desktop margin is reset to 0 at the 820 px mobile breakpoint so the buttons stay flush on narrow screens.
- **Docker `npm ci` failure.** `tailwindcss/postcss-load-config` declares `yaml@^2.4.2` as a peer dependency. npm 10 in the Docker node:20-alpine image enforces this strictly while npm 11 (used locally) does not. Adding `yaml@2.8.4` as an explicit dev dependency fixes the CI build without changing production behavior.

## [0.0.17.8] - 2026-05-08

### Security
- **Cap `request_timeout_seconds` at 1800s in CDR + MCS create schemas.** The DB column has always been settable, but the Pydantic create schemas didn't expose it; now they do, with `Field(default=30, ge=1, le=1800)`. Closes the timeout-as-DoS-vector identified in the multi-MCS design doc — an attacker (or a misconfigured admin) can no longer set a 24-hour timeout to hold a backend worker. 8 new regression tests cover the cap, zero-rejection, the within-cap accept path, and the 30s default. Surfaced via the `/cso`-style audit at PR #6's threat-surface walkthrough.
- **Sanitize URLs in `fhir_client.py` log payloads.** 10 call sites (`logger.info` / `logger.warning` with `extra={"url": ...}` or `extra={"valueset_url": ...}`) were emitting raw URLs that could carry embedded credentials (`https://user:pass@host/fhir`) if a connection had been configured with userinfo in the URL. All call sites now wrap the URL through the existing `sanitize_url()` helper, which strips userinfo, redacts auth-shaped query params, and replaces single-label internal Docker hostnames with `[host]`. No prior incident — defense-in-depth fix from the same audit.

### Added
- **`CONTRIBUTING.md` — "How to add a connection kind" recipe.** Five-step walkthrough (model + migration + factory wiring + dependency + frontend) with the MCS implementation called out as the canonical reference. Captures the things a contributor would otherwise re-derive: the partial-unique-index pattern, the SAME factory mounting two `include_router` calls, where to NOT touch (the factory itself, EncryptedJSON), and the documented out-of-scope decisions (per-kind key isolation, CDR_FERNET_KEY env-var rename).

### Changed
- **`CDRConnectionResponse` and `MCSConnectionResponse`** now include `request_timeout_seconds` so the field round-trips. Previously the create schemas didn't expose it at all; now both response and create schemas do.

## [0.0.17.7] - 2026-05-07

### Added
- **Responsive topbar — collapse to a single "Connections" pill below 768 px.** New `HealthChipGroup` component renders the array of per-kind `HealthIndicator` chips on wider viewports and switches to an aggregate pill + popover on mobile (44 px touch targets per chip, click-outside / Esc closes the popover). Aggregate dot color is the worst-case across kinds: green if all healthy, red if any unreachable, gray otherwise. Container queries weren't usable because the topbar's intrinsic width depends on its children; falls back to a viewport media query.
- **"Verify with sample evaluate" button on the active MCS connection card row.** Backend gains `POST /settings/mcs-connections/{id}/probe`; the route delegates to a new `probe_mcs_data_requirements()` helper in `fhir_client.py` that runs `Measure?_count=1` on the MCS, then calls `$data-requirements` against the first measure with a benign `periodStart=2024-01-01`/`periodEnd=2024-12-31` window. This is a stricter probe than `test-connection` (which only fetches `/metadata`) — it forces the engine to resolve a Library + ValueSets, which is the failure surface attendees actually hit at the connectathon. Empty-MCS path is non-fatal: returns `{status: "warning", outcome: ...}` with the OperationOutcome rendered via the existing `OperationOutcomeView`.

### Changed
- **Health polling pauses when the document is hidden.** Previously the 30 s `setInterval` fired regardless of tab visibility; on a session with multiple Lenny tabs open this was a small but real thundering-herd cost. The polling loop now starts/stops based on `document.visibilityState`, so background tabs don't probe `/health`. Refresh-on-visible (added in v0.0.17.6) is preserved — when a tab becomes visible again it does one immediate probe before re-arming the interval.
- **Topbar chips are always tabbable with full keyboard activation.** `HealthIndicator` is now `tabIndex={0}` in every state (was `0` only when interactive); `role="button"` is set whenever `onClick` is provided regardless of state; Enter/Space activate the click handler; Esc closes the popover. ARIA label is `"{kind}: {name}, {status}"` per the saved spec, so screen readers get the full chip story instead of just the connection name.

## [0.0.17.6] - 2026-05-07

### Added
- **Multi-MCS connection management UI** — frontend exposes the backend MCS connection-management surface from PRs #293/#294. Settings → Connections now stacks two cards (CDR Connections, MCS Connections) via a reusable `ConnectionSection` component, each with its own list/add/edit/activate/delete controls. `ConnectionModal` is `kind`-driven via a `KIND_SPECS` map (`cdr`, `mcs`); URL field name (`cdr_url` vs `mcs_url`), labels, and the `is_read_only` checkbox (CDR-only) all derive from the spec. One modal serves both kinds; future kinds add a `KIND_SPECS` entry.
- **Topbar chip per connection kind** — replaces the single CDR `HealthIndicator` with an array of chips (one per kind). Four-state machine: `pending` (gray, initial), `healthy` (green), `unreachable` (red, hover tooltip with hint + sanitized URL), `none` (gray, plumbed for future "no active connection" case). Debounced: 2 consecutive failed probes before flipping to `unreachable`. Cadence: `setInterval` at 30 s + Page Visibility refresh on tab focus (no thundering herd on visibilitychange). Click on `unreachable`/`none` navigates to `/settings#{kind}-connections`.
- **7 new MCS API client functions** in `frontend/src/api/client.js` — `getMcsConnections`, `createMcsConnection`, `getMcsConnection`, `updateMcsConnection`, `deleteMcsConnection`, `activateMcsConnection`, `testMcsConnection`. Mirror the CDR surface.
- **`/health` resolves the active MCS via `get_active_mcs`** — was: probed `settings.MEASURE_ENGINE_URL`. Now uses the active row from `mcs_configs` (with the same legacy env-var fallback as `get_active_cdr`). Response gains `measure_engine.name` so the topbar chip can display the connection name.

### Changed
- **Test-connection routes are now per-kind** — were colliding at `/settings/test-connection` (both CDR and MCS factory instantiations registered the same path; CDR's schema won, so MCS sends were rejected with `field required: cdr_url`). The factory now registers `f"{prefix}/test-connection"`. New paths: CDR `/settings/connections/test-connection`, MCS `/settings/mcs-connections/test-connection`. Frontend `client.js` and 12 backend test callsites updated. This was originally deferred to a follow-up PR; folded in here once it surfaced during smoke testing of the MCS edit modal.

### Known limitations
- Measures page is not scoped to the active MCS (#296). The list is global, but only measures actually loaded on the active MCS will evaluate successfully. Switching MCS does not re-scope the page.
- Failed-job detail surfacing is minimal (#297). Today the UI shows "Failed" with little context; backend logs hold the actual cause.

## [0.0.17.5] - 2026-05-07

### Added
- **Job→MCS snapshot + active-MCS routing for measure evaluation** — every new measure-calculation job now snapshots the active MCS connection (`Job.mcs_id`, `Job.mcs_url`, `Job.mcs_name`) at creation time, mirroring the existing CDR snapshot fields. The orchestrator resolves the snapshot URL when the job runs and threads it into `evaluate_measure(...)` via the new `measure_engine_url` parameter, so jobs run against the MCS that was active at job creation — not whatever's active now. Connectathon attendees can switch between their MCS and a reference MCS without breaking in-flight jobs. Legacy rows with NULL `mcs_url` fall back to `settings.MEASURE_ENGINE_URL` (preserves the historical "always call the env var" behavior for jobs created before #12).
- **`get_active_mcs()` FastAPI dependency** — parallel of `get_active_cdr()`, returns a `ConnectionContext` with `kind=ConnectionKind.mcs` and the active MCS row's URL/name/timeout.
- **`ConnectionContext.mcs_url` field + `url` kind-agnostic property** — each kind populates its own URL field (`cdr_url` for CDR, `mcs_url` for MCS); the `url` property dispatches by `kind`. CDR consumers continue using `ctx.cdr_url` unchanged.

### Changed
- **`_run_schema_migrations()`** adds `Job.mcs_id` (FK to `mcs_configs.id` ON DELETE SET NULL), `Job.mcs_url`, `Job.mcs_name` — all nullable. Backfill UPDATE populates the snapshot for existing jobs from `MEASURE_ENGINE_URL` so the job-history view doesn't show "(unknown)" on legacy rows. If the env var is unset at migration time, the backfill skips and a warning logs (jobs render "(unknown)" for those rows; the orchestrator's env-var fallback keeps them runnable).

## [0.0.17.4] - 2026-05-07

### Added
- **Measure Calculation Server (MCS) connection management** — new `MCSConfig` model + `/settings/mcs-connections` routes, mirroring the CDR connection-management surface. Connectathon attendees can configure multiple MCS endpoints (their own + reference servers like cqf-ruler) and switch between them via the same CRUD + activate API as CDR. Full feature surface: list, create, get, update, delete, activate, test-connection. Schema differences vs CDR: `mcs_url` field name (per the doc-locked decision to defer generic `url` until kind #3), no `is_read_only` flag (Lenny only POSTs `$evaluate-measure` and `$data-requirements` to the MCS, so the read/write distinction doesn't apply).
- **`ConnectionKind.mcs`** enum value alongside `cdr`. Future kinds (TS, MR, MRR) extend the same enum.
- **`seed_default_connections()` lifespan hook** — replaces the old hardcoded-URL CDR seed with a Python function that reads URLs from env vars (`DEFAULT_CDR_URL`, `MEASURE_ENGINE_URL`). Idempotent across restarts. Runs identically on Postgres and SQLite (no more raw-SQL Postgres-only gate). Fixes the pre-existing CLAUDE.md "no hardcoded URLs" violation in the seed path.

### Changed
- **Activation-race partial-unique-index** now created for both `cdr_configs` and `mcs_configs` (declared via `__table_args__` on each model + raw-SQL belt-and-suspenders in lifespan, both dialects).
- **JSONFormatter** structured-log allowlist extended with `mcs_id`, `mcs_name`, `mcs_url` so MCS audit events serialize correctly.

## [0.0.17.3] - 2026-05-07

### Changed
- **CDR connection routes refactored to a generic factory** — the seven CRUD endpoints (`list/create/get/update/delete/activate/test-connection`) now live in `app/routes/connection_factory.py:make_connection_router(...)`, parameterized by model, schemas, URL field name, kind, default name, and Job FK column. `app/routes/settings.py` becomes a single `make_connection_router(...)` instantiation for CDR (~75 LOC, down from ~480). Same routes, same response shapes, same audit-log structure — verified by all 31 existing CDR tests passing unchanged. The factory is the seam where MCS, TS, MR, and MRR will drop in with one-line additions in subsequent PRs.

## [0.0.17.2] - 2026-05-06

### Added
- **Per-connection `request_timeout_seconds`** — every connection-config row (CDR today, MCS in a follow-up PR) carries its own httpx-timeout setting, defaulting to 30 seconds. `ConnectionContext` exposes the value to route handlers so `fhir_client.py` can pass it through to each `httpx.AsyncClient`. Sets up issue #12's "measure runs are long-running synchronous transactions" requirement: connectathon attendees with slow servers can crank the timeout up per-connection without affecting other connections.
- **`ConnectionKind` enum** — `dependencies.ConnectionContext.kind` is now typed as a closed enum (`{cdr}` today, `{mcs, ts, mr, mrr}` added as their PRs land). String-valued so JSON serialization is unchanged. Replaces the free-form `str` from PR #1a's review fixes.
- **Activation-race regression test** — `test_activate_concurrent_raises_integrity_error` asserts the partial unique index `idx_one_active_cdr` rejects a second row with `is_active=True`. The index is now declared in `CDRConfig.__table_args__` so `Base.metadata.create_all` generates it for both Postgres and SQLite, closing the test gap where SQLite previously didn't exercise the constraint.

### Changed
- **Schema migrations** add `request_timeout_seconds INTEGER NOT NULL DEFAULT 30` to `cdr_configs` (idempotent; `ADD COLUMN IF NOT EXISTS` style) and the lifespan partial-index creation now runs on both Postgres and SQLite (with dialect-appropriate WHERE clause: `is_active = TRUE` vs `is_active = 1`). Forward-safe; rollback is code-only.

## [0.0.17.1] - 2026-05-06

### Changed
- **Connection-config models now share a SQLAlchemy mixin** — `CDRConfig` inherits from a new `ConnectionConfigMixin` (`backend/app/models/connection_base.py`) that holds the shared columns (`id`, `name`, `auth_type`, encrypted `auth_credentials`, `is_active`, `is_default`, `created_at`, `updated_at`). CDR-specific fields (`cdr_url`, `is_read_only`) stay on `CDRConfig`. The `cdr_configs` table shape is unchanged on disk — purely a code-organization refactor that paves the way for a parallel `MCSConfig` model in a follow-up PR.
- **`CDRContext` renamed to `ConnectionContext`** in `backend/app/dependencies.py`, with a `kind` field defaulting to `"cdr"`. Existing imports continue to work via the `CDRContext = ConnectionContext` alias. No call-site changes required for the rename to land.
- Removed 5 broken connectathon measures (CMS2, CMS71, CMS165, CMS1017, CMS1218) from seed bundles, manifest, and test suite due to upstream bundle/HAPI issues that cannot be fixed before the connectathon. See issue #278. The 7 remaining strict=true measures (CMS122, CMS124, CMS125, CMS130, CMS506, CMS816, CMS529) are unaffected. Re-add any measure once MADiE/HAPI ships a fix by dropping the refreshed bundle into `seed/connectathon-bundles/` and adding its manifest entry.

## [0.0.17.1] - 2026-05-06

### Removed
- **HAPI async-indexing compensator retired** — the `HAPI_SYNC_AFTER_UPLOAD` flag and `trigger_reindex_and_wait*` functions (609 lines) are gone. The underlying fix — `hibernate.search.indexing.plan.synchronization.strategy=sync` on both HAPI services (PR #214) — makes POST/PUT block until the Lucene index is refreshed, so the Python-side polling and sleep fallbacks are no longer needed. Startup, validation uploads, and job runs are unaffected in behavior.

## [0.0.17.0] - 2026-05-06

### Added
- **Evaluated resources now survive subsequent jobs** — every successful patient evaluation persists a snapshot of its `evaluatedResource` FHIR resources alongside the MeasureReport, so the Results page's "Evaluated resources" section remains viewable after the next job's `wipe_patient_data()` would have cleared the engine-side data. New `evaluated_resources` JSON column on `measure_results`; `GET /results/{id}/evaluated-resources` now returns from the snapshot when present (`source: "snapshot"`) and falls back to live measure-engine resolution for legacy rows (`source: "live"`).

## [0.0.16.1] - 2026-05-06

### Fixed
- **Population membership now displays in the patient detail fly-out** — the Results page's per-patient drawer was always showing an empty "Population membership" section and missing header badges. The component was reading population flags at the top level of the result object, but the API nests them under `result.populations`. Three accesses now read from the correct path, so each patient's Initial population / Denominator / Numerator status renders with the right Yes/No state.

## [0.0.16.0] - 2026-05-05

### Fixed
- **Jobs page hero card now shows the actively-running job** — when multiple jobs are queued, the progress card previously showed the most-recently-created queued job instead of the job actually executing. The hero card now prioritizes a truly-running job and only falls back to showing a queued job when nothing is in progress.
- **Subtitle no longer conflates running and queued counts** — the "N running" counter now shows only jobs in active execution; queued jobs appear separately as "N queued" when present.
- **Hero card badge reflects actual job status** — the badge was hardcoded as "Running" regardless of whether the job was queued or running. It now renders the correct status via `StatusBadge`.
- **Elapsed timer hidden for queued jobs** — the "Elapsed" timer in the hero card was counting from creation time for queued jobs that haven't started yet. It now only appears for jobs that are actively running.

### Added
- **`jobStatus.js` utility** — extracted `isActuallyRunning`, `isRunning`, `isComplete`, and `selectActiveJob` helpers to `frontend/src/utils/jobStatus.js` with 24 unit tests covering the hero-card job selection logic.

## [0.0.15.0] - 2026-05-03

### Added
- **End-to-end Jobs pipeline validation** — New parametrized integration test (`test_full_jobs_pipeline.py`) runs all 11 connectathon measures through the Lenny Jobs API against the prebaked HAPI stack and asserts per-patient numerator/denominator outputs match the ground-truth expected populations from the connectathon bundles. Closes the gap between `test_connectathon_measures.py` (bypasses Lenny) and `test_full_workflow.py` (no count assertions). CMS1017 (HTTP 400 from HAPI) is skipped; all 11 remaining measures pass on fresh prebaked containers. Runs in the nightly `jobs-pipeline-validation` workflow, not the PR gate.
- **`scripts/validate_all_measures.py`** — Standalone script to validate all connectathon measures through the Jobs API against any stack. Produces a per-measure pass/fail table and structured JSON output. Exit codes: 0 = all strict measures pass, 1 = strict-measure failures, 2 = infra error.
- **Jobs pipeline validation CI job** — New `jobs-pipeline-validation` job in the weekly Connectathon Measures workflow validates the full orchestration layer end-to-end using prebaked HAPI images (requires Groups pre-loaded in CDR).

### Fixed
- **Job creation no longer fails with FK violation when no CDR config exists** — `cdr_id` was stored as `0` (a non-existent FK reference) when using the fallback unauthenticated CDR path. Now correctly stores `NULL`, matching the `ON DELETE SET NULL` intent.
- **Orchestrator no longer raises RuntimeError for unauthenticated direct-URL jobs** — `_get_cdr_auth_headers` previously raised unconditionally when `cdr_id` was `NULL`, breaking every job created without a CDR config row. Now returns empty headers when `auth_type` is `none` or unset; only raises when the job required auth credentials that are now unrecoverable.

## [0.0.14.0] - 2026-05-03

### Added
- **Admin settings tab in Settings** — A new "Admin" section in Settings exposes two operator controls: a "Wipe engine" button that deletes all measure-definition resources (Library, Measure, ValueSet, CodeSystem, ConceptMap) from the HAPI measure engine to recover from CQL compilation failures (issue #238 follow-up), and a Validation toggle that enables or disables the bundle validation workflow.
- **Validation feature flag** — Toggling Validation in Settings → Admin hides or shows the Validation nav item in real time without a page reload. The setting persists across restarts via the database.
- **Wipe confirmation dialog** — The "Wipe engine" action requires a confirmation step to prevent accidental destruction; the engine re-seeds automatically on the next job run.

## [0.0.13.1] - 2026-05-03

### Fixed
- **CMS529 measure now evaluates correctly** — The manifest `id` for CMS529 (`CMS529FHIRHybridHospitalWideReadmission`) disagreed with the actual HAPI Measure resource id (`CMSFHIR529HybridHospitalWideReadmission`), causing all 53 CMS529 test patients to fail `$evaluate-measure` with a 404. The manifest id is corrected; rebuilding the prebaked CDR image will produce a FHIR Group with the right id and unblock CMS529 evaluation.

## [0.0.13.0] - 2026-05-03

### Changed
- **Measure names now show as `[CMS125] Name` throughout the app** — Jobs, Results, Measures, and Validation pages all display the CMS ID in brackets alongside the human-readable name, matching Connectathon community vocabulary and reducing the need to memorize CMS IDs separately.
- **Calculate button now opens the New Calculation modal pre-filled** — Clicking Calculate on any measure in the Measures library navigates directly to the Jobs page with that measure already selected in the modal, preserving context instead of dropping you on a blank Jobs page.
- **System Status labels now say "Local"** — All four indicators on the Settings → System Status tab (Backend, Measure Engine, CDR, Database) are prefixed with "Local" to reinforce the bundled-stack context and set up the visual contrast for future remote connections.

### Added
- **"Job run" label on the Results page dropdown** — The job-run selector now has a visible label, removing the unlabeled control that confused testers.

### Removed
- **Placeholder trend line removed from Results page** — The blue sparkline on the Performance Rate card displayed hardcoded data identical for every measure, job, and period. Removed to avoid misleading users until real historical data is available.

## [0.0.12.0] - 2026-05-01

### Added
- **Auto-select patient group when measure is chosen (issue #228)** — Choosing a measure in the "Start Calculation" modal now automatically pre-fills the Patient Group field with the group whose CMS number matches the selected measure (e.g. selecting `CMS122FHIRDiabetes...` auto-selects `CMS122-cohort`). Uses `extractCmsId` as the join key. Manual group selection still works; the field clears if no match is found.
- **FHIR Groups synthesized for all 12 connectathon measures (issue #232)** — Seeding the local stack now produces one FHIR `Group` resource per measure bundle on the CDR, so the Patient Group dropdown is populated on a fresh install without manual setup. CMS1017's curated `artifact-testArtifact` extension is preserved; the other 11 groups are synthesized from each bundle's Patient members.

### Fixed
- **Population counts no longer show 0 when running jobs against a patient group** — The reindex probe that gates CQL evaluation was inadvertently polling a pre-baked phantom patient from the HAPI measure engine Docker image instead of the patients just pushed to the engine. The probe now targets only patients with Encounters from the current batch, so CQL sees correctly indexed data. Batches with no Encounter-bearing patients fall back to a timed sleep rather than probing the wrong patient.

## [0.0.11.0] - 2026-04-29

### Fixed
- **Measure bundle upload no longer fails with HTTP 422 (HAPI-0902)** — Uploading a QI-Core 6 measure bundle now succeeds even when the measure engine already holds a ValueSet under a different resource ID than what the bundle contains. The upload service now queries HAPI by canonical URL before posting and rewrites conflicting IDs in-place, turning a failed create into a clean update.
- **Backend log no longer raises `KeyError` on measure upload** — Using `filename` as a structured log key collided with Python's reserved `LogRecord.filename` field. Fixed by switching to a format-string log call.

## [0.0.10.0] - 2026-04-28

### Added
- **Fernet encryption for CDR auth credentials at rest (issue #219)** — Basic passwords, Bearer tokens, and SMART `client_secret` values are now encrypted in `cdr_configs.auth_credentials` using Fernet (AES-128-CBC + HMAC-SHA256). Each value is stored as a `{"v": 1, "ct": "<token>"}` envelope; the TypeDecorator decrypts transparently on read so no call-site changes are required.
- **`credential_crypto` module** — `EncryptedJSON` SQLAlchemy `TypeDecorator`, lazy Fernet singleton loader (reads `/run/secrets/cdr_fernet_key` first, falls back to `CDR_FERNET_KEY` env var which is immediately `os.environ.pop`'d to prevent subprocess leakage), `self_check()` startup probe.
- **`CDR_FERNET_KEY` in SSM/Docker-secrets pipeline** — `bootstrap-aws.sh` provisions `/leonard/prod/CDR_FERNET_KEY` as a SecureString (generated via Python, never visible as a process arg). `fetch-prod-secrets.sh` fetches it alongside `POSTGRES_PASSWORD`. `deploy-prod.sh` writes it to `/run/leonard/CDR_FERNET_KEY` (mode 0600). `docker-compose.prod.yml` mounts it as a Docker secret. `.env.example` documents the local dev generation command.
- **`cdr_id` FK on `Job`** — replaces the per-job `cdr_auth_credentials` plaintext snapshot. The orchestrator now reads live credentials from `cdr_configs` via the FK, so mid-job CDR credential rotations propagate naturally.
- **Audit logging on CDR connection changes** — `POST`/`PUT`/`DELETE` to `/connections` emit a structured `INFO` log with `event: cdr_credentials_changed`, `action`, `cdr_id`, and `cdr_name`. Credential values are never logged.
- **409 guard on CDR connection delete** — `DELETE /connections/{id}` returns 409 if any `queued` or `running` jobs reference the connection, preventing orphaned in-flight jobs.

### Changed
- **Inline migration** — startup adds `cdr_id INTEGER REFERENCES cdr_configs(id) ON DELETE SET NULL` to `jobs`, backfills from matching `cdr_url + name`, then drops `cdr_auth_credentials`. Guarded by `IF NOT EXISTS`/`IF EXISTS` — idempotent on concurrent restarts.
- **Credential encryption backfill** — on Postgres, any `cdr_configs.auth_credentials` rows lacking the `{v, ct}` envelope are re-saved through the TypeDecorator at startup (uses `flag_modified` to force SQLAlchemy dirty-check).
- **Orchestrator live-lookup** — `_get_cdr_auth_headers` now joins `cdr_configs` via `job.cdr_id` instead of reading the stale job snapshot. Raises `RuntimeError` with a clear message if `cdr_id` is NULL or the CDR row no longer exists.

### Fixed
- **Plaintext CDR credentials at rest** — closes the HIGH finding from the 2026-04-28 security audit. Credentials are no longer recoverable from a DB backup, read replica, or ops console query.

## [0.0.9.0] - 2026-04-28

### Added
- **Structured FHIR error surfacing (PR-2 — $gather + bundle upload + MCS OperationOutcome, issues #75 #76)** — three previously-silent failure classes now surface actionable context to the user.
- **MCS `OperationOutcome` preservation (#76)** — when the Measure Calculation Server returns a non-2xx response or a 200-with-`OperationOutcome` body, `evaluate_measure` raises `FhirOperationError` carrying the full parsed OO. The orchestrator persists the server-returned OO (not a synthetic one) in `measure_report` via a FHIR `Extension`, along with `error_details` (status_code, url, latency_ms, raw_outcome). Per-patient error rows show the MCS OO in the `PatientDetail` drawer alongside existing results.
- **Partial CDR gather surfaced (#75 AT-2)** — when `DataRequirementsStrategy` fails to fetch one or more resource types from the CDR but succeeds on others, `gather_patient_data` now returns a `GatherResult` with `failed_types: list[FailedResourceFetch]`. The orchestrator continues to evaluate with available data (does NOT skip the patient), annotates the `MeasureResult` with `error_phase="gather_partial"` and `error_details` listing failed/succeeded types. The ResultsPage shows an amber **Partial data** badge for these rows so attendees know results may be incomplete.
- **Full CDR gather failure surfaced (#75 AT-1)** — when gather/push raises an exception (CDR unreachable, 401, etc.), the patient is recorded with `error_phase="gather"`, `error_details` with upstream status_code/url/latency, and is skipped in the evaluate phase.
- **Bundle upload partial failures surfaced (#75 AT-3/AT-4)** — `push_resources` now parses the HAPI batch-response Bundle and returns a `BundleUploadResult` with per-entry success/failure breakdown. A 200-OK-with-OperationOutcome body (transaction-level HAPI rejection) is treated as a failure. The ValidationPage upload result shows a per-entry failure list. `BundleUpload.error_details JSONB` column added.
- **`ValidationResult.error_details JSONB` column** — `evaluate_and_compare` catches `FhirOperationError` and persists the MCS OO in `error_details`.
- **New dataclasses** — `GatherResult`, `FailedResourceFetch`, `BundleUploadResult`, `BundleEntryResult` in `fhir_client.py`.

### Changed
- **`PatientDetail` drawer** — when `measure_report.resourceType === "OperationOutcome"`, renders an `OperationOutcomeView` above the raw JSON toggle. Closes #76.
- **ResultsPage patient table** — tri-state: `error` rows show a red **Error** badge with error phase; `gather_partial` rows show an amber **Partial data** badge.
- **ValidationPage upload result** — per-entry failure list when `error_details.failed_entries` is non-empty.
- **`_error_measure_report`** — embeds MCS-returned OO via FHIR Extension instead of synthesizing from `str(exc)`. Deep-copies upstream dict to prevent cross-patient mutation. `populations["error_message"]` still written for back-compat.

### Fixed
- **MCS OO discarded on 4xx/5xx** — `evaluate_measure` now parses and preserves the server-returned OperationOutcome. Closes #76.
- **Partial gather silent pass** — patients with partial CDR data now annotated with `gather_partial` phase instead of evaluating against missing resources. Closes #75.
- **HAPI 200-with-OperationOutcome body** — transaction-level HAPI bundle rejection returning HTTP 200 with OO body now raises `FhirOperationError`.

## [0.0.8.1] - 2026-04-27

### Added
- **Structured FHIR error surfacing (PR-1 — connection/auth, issue #74)** — when a CDR connection test fails, the backend now returns a structured `OperationOutcome` with `error_details` containing the HTTP status code, probed URL, latency, and a user-facing hint (e.g. "Authentication failed. Re-check your bearer token or username/password."). Network errors (unreachable host, TLS/SSL, timeout) include network-layer hints. SSRF attempts return HTTP 400 instead of 502.
- **`fhir_errors` shared module** (`backend/app/services/fhir_errors.py`) — `FhirOperationOutcome`, `FhirOperationError`, `build_error_envelope`, `redact_outcome`, `sanitize_url`, and `HINT_BY_STATUS` hint map. Foundation for PR-2 ($gather and MCS error surfacing).
- **DB schema extensions** — `measure_results.error_details JSONB`, `measure_results.error_phase VARCHAR(32)`, and a unique index on `(job_id, patient_id)` (with dedup pre-flight) added via `_run_schema_migrations`. Populated by PR-2; nullable additive in this PR.
- **`parseFhirError` helper** (`frontend/src/api/fhirError.js`) — parses `detail.issue[]` and `detail.error_details` from API error bodies into `{issues, errorDetails}`. Now consumed by `client.js`; ready for `OperationOutcomeView` in the next PR.
- **Integration tests** (`backend/tests/integration/test_connection_errors.py`) — bearer-token 401, unreachable-URL 502, and success `response_time_ms` scenarios against a live HAPI CDR.

### Changed
- **`/settings/test-connection` response** — success responses now include `response_time_ms` (int) and a sanitized `url` field. Error responses carry the full `error_details` envelope. Closes #74.

## [0.0.8.0] - 2026-04-27

### Changed
- **Bundle-loader CI test now runs against a 2-bundle subset** — `bundle-loader-test` exercises `load_connectathon_bundles()` against CMS122 and CMS124 instead of all 12 connectathon bundles, cutting vanilla-HAPI CI wall-clock from >90 min toward ≤60 min. Structural tests (file presence, SHA256) still cover all 12 bundles. All 12 continue to load nightly via the bake job. Closes #202.

## [0.0.7.0] - 2026-04-27

### Changed
- **Measure IDs now display as short CMS numbers** — the Measures table, job creation form, patient group dropdown, and Results page all show `CMS122` instead of the full raw HAPI ID (`CMS122FHIRDiabetesAssessGT9Pct`). Handles both `CMS{n}FHIR...` and `CMSFHIR{n}...` ID patterns.
- **"FHIR" suffix stripped from measure names** — display names like `"Breast Cancer ScreeningFHIR"` now show as `"Breast Cancer Screening"` across all pages.
- **Job creation dropdown shows formatted labels** — measure options now display as `CMS71 — Anticoagulation Therapy Prescribed at Discharge` instead of raw FHIR IDs. Patient group options show the same format with patient count appended.

## [0.0.6.8] - 2026-04-25

### Added
- **Year picker for reporting period** — the job creation form now defaults to the current calendar year (Jan 1 – Dec 31) with a dropdown showing the last 5 years, so you can say "2026" instead of typing both dates. A "Enter custom dates" toggle is available for non-calendar-year ranges; clicking "← Back to year select" restores the year dropdown. Closes #79.
- **Frontend component tests** — React Testing Library test suite for `PeriodPicker` covering year dropdown, custom date toggle, and `onChange` wiring (10 tests).

## [0.0.7.7] - 2026-04-24

### Fixed
- **Validation runs no longer fail with 409 CONFLICT on first evaluation** — concurrent `$evaluate-measure` calls to HAPI now include a warmup burst that serially evaluates one patient per measure before the concurrent batch. First concurrent batch against a fresh measure engine triggers a race during SearchParameter indexing; some concurrent requests hit 409 CONFLICT affecting ~20-30% of patients. Warmup avoids the race by completing indexing in single-threaded context before concurrent calls start. First run after bundle upload now succeeds; retry workarounds no longer necessary. Fixes #156.

## [0.0.6.7] - 2026-04-22

### Fixed
- **Validation runs no longer fail for EXM FHIR4 measures** — `_resolve_measure_id` now handles relative FHIR references (`Measure/{id}`) in addition to canonical URLs. EXM test bundles store `MeasureReport.measure` as a relative reference, which previously couldn't be resolved because HAPI was queried via `?url=` (which only matches canonical URLs). Fixes #108.

### Removed
- **Obsolete EXM FHIR4 measure bundles** — removed 9 old bundles (EXM104, EXM105, EXM108, EXM124, EXM125, EXM130, EXM165, EXM506, EXM529) that were being auto-loaded on backend startup, causing duplicate measures in production. Kept older CMS placeholder versions (v0.3-v0.5) pending QI-Core 6 dQM v1.0.000 bundles from MADiE (issue #115).

## [0.0.6.6] - 2026-04-22

### Fixed
- **ValueSet compose patch now applies on production bundle upload** (`triage_test_bundle`), not only in test fixtures. MADiE bundles with ValueSets that have sub-ValueSet compose references or bare CodeSystem includes (no explicit codes) are now rewritten to use direct code lists from `expansion.contains` before being POSTed to the HAPI measure engine — preventing all-zero CQL evaluation results. (#99)

## [0.0.6.5] - 2026-04-22

### Changed
- **Measures page UI improvements**: measure display names no longer show the trailing "FHIR" suffix for cleaner presentation, and a new "Measure ID" column displays the measure identifier (e.g., "CMS122") for easier identification and reference.

### Security
- **Upload endpoint hardening** — two unauthenticated upload endpoints now protected against abuse:
  - `POST /measures/upload`: 100 MB size cap (413 OperationOutcome); 10 req/min per-IP rate limit (429 OperationOutcome)
  - `POST /validation/upload-bundle`: same size cap and rate limit; filename sanitization strips null bytes, control characters, and path-traversal sequences; filenames truncated to 255 chars with extension preserved
  - `Caddyfile`: `request_body { max_size 100MB }` on the API vhost for belt-and-suspenders OOM prevention; `header_up X-Forwarded-For {remote_host}` prevents clients from spoofing the rate-limit key
  - `backend/app/limiter.py`: shared slowapi `Limiter` with `X-Forwarded-For`-aware key function (Caddy proxy architecture)
  - `backend/app/config.py`: `MAX_UPLOAD_SIZE` constant shared across both endpoints

## [0.0.6.3] - 2026-04-21

### Added
- Automatic lazy measure loading: validation runs now detect missing measures on the HAPI engine and attempt to reload them from seed bundles automatically before failing. If manual recovery is needed, a recovery script is available: `./scripts/reload-validation-bundles.sh`.
- Comprehensive validation failure recovery guide (`docs/validation-fixes.md`) documenting three complementary recovery strategies for when expected results exist but measure resources are lost.

### Fixed
- **Validation error messages** now provide clear, actionable guidance instead of raw HAPI errors. When the measure engine is unavailable or measures are missing, users see a user-friendly message directing them to the Validation page or manual recovery steps.
- **Bundle upload error handling** — when measures fail to push to HAPI during bundle upload, the system returns a descriptive error instead of a generic failure, helping users diagnose connection issues or measure engine availability problems.

## [0.0.6.2] - 2026-04-21

### Changed
- `docs/architecture.md` and `docs/testing.md` refreshed to match the current repo: HAPI FHIR bumped to v8.8.0-1, `backend/app/dependencies.py` and `ConnectionModal.js` added to the structure maps, integration test file list expanded, PR gate corrected to reflect that `test_connectathon_measures.py` and `test_full_workflow.py` run nightly (not on PRs), and `STRICT_STU6=0` noted as the current CI default during rollout.

### Removed
- `docs/validation-findings-2026-03-27.md` — superseded by `docs/connectathon-measures-status.md`.
- `docs/workflow-proposal.md` — early draft, superseded by `docs/workflow.md`.

## [0.0.6.1] - 2026-04-21

### Added
- `docs/connectathon-measures-status.md` — full status reference for all 12 MADiE connectathon measures, including per-measure pass/fail/skip counts, golden test exclusion rationale for CMS1017/CMS1218, infrastructure bugs fixed in sessions 11–13, remaining failure classes, and next steps.

## [0.0.6.0] - 2026-04-20

### Fixed
- **Golden measure tests now pass reliably against HAPI v8.6.0** — resolved two test failures
  affecting CMS122 ("Betty-Bertha-*" patients showing `denominator-exclusion=0`) and all EXM
  DBCG connectathon bundles. Root causes: HAPI ignores pre-computed ValueSet expansions and
  always re-expands via compose; patient data must be loaded on the measure server (not just
  the CDR) for `$evaluate-measure` to resolve it. Fixes include ValueSet compose patching,
  dual-server patient loading, and post-load `$reindex` with indexed-resource polling.
- **Duplicate ValueSet guard** — before loading each golden bundle's ValueSets, the test
  fixture checks which canonical URLs are already in HAPI and skips any duplicates. Prevents
  "Multiple ValueSets resolved" CQL evaluation errors when bundles share ValueSet URLs.
- **All-bundle reindex wait** — the fixture now collects a probe encounter from every bundle
  and waits until all probes are indexed before running tests, preventing a race where later
  bundles' encounters were not indexed when tests started.
- EXM bundles (DBCG connectathon era, CQL 1.3 syntax) marked `xfail` — HAPI v8.6.0's CQL
  engine no longer supports the `timezone` keyword and old `DateTime()` signatures used in
  these pre-2021 bundles.
- Improved test robustness: `$reindex` failures now emit warnings, HAPI response parsing is
  guarded against non-JSON bodies, nested `expansion.contains` entries are fully flattened,
  and ELM decoding errors are caught rather than propagating as unhandled exceptions.

## [0.0.5.0] - 2026-04-19

### Added
- QI-Core STU6 (v6.0.0) support: HAPI FHIR now installs the QI-Core, US Core 6.1.0, and CQL
  implementation guides on startup via `hapi.fhir.implementationguides.*` env vars in both
  production and test Docker Compose configs.
- 11 of 12 connectathon bundles replaced with QICore6 versions from the cqframework public
  repository (CMS2, CMS71, CMS122, CMS124, CMS125, CMS130, CMS165, CMS506 added; CMS816,
  CMS1017, CMS1218 retained; CMS529 pending MADiE access).
- `seed/connectathon-bundles/manifest.json` with SHA-256 pins, canonical URLs, expected
  test-case counts, 2026 measurement period, and per-measure strictness flag.
- Integration smoke test (`tests/integration/test_smart_load.py`): manifest-driven bundle
  load verification including CDR CapabilityStatement + QI-Core IG assertions.
- Per-test-case integration test (`tests/integration/test_connectathon_measures.py`):
  parametrized by MADiE `ExpectedResult` row; uses `STRICT_STU6` env var for CI gating.
- Connectathon rehearsal script (`scripts/connectathon-rehearsal.sh`): cold-start demo
  workflow with health polling, measure inventory, and a 12-row pass/fail table.

### Fixed
- `wipe_patient_data` now includes `Medication` and `Task` resource types found in QICore6
  connectathon bundles, preventing leftover data from contaminating subsequent evaluations.
- `_KNOWN_CLINICAL_TYPES` in `validation.py` updated to include `Medication` and `Task`,
  eliminating spurious unknown-type log warnings when processing QICore6 bundles.
- Rehearsal script `jq` calls use `-r` flag to prevent quoted string IDs in URLs.
- `bundle_loader.py` now skips `manifest.json` when globbing bundle files, preventing it
  from being loaded as a FHIR bundle.
- Measure push switched from transaction to batch bundle to avoid HAPI-2001 (`Patient ref
  unknown`) when clinical subjects are absent from the measure engine.
- `docker-compose.test.yml` `server_address` corrected from Docker-internal hostname to
  `localhost` so HAPI pagination links resolve from the CI host.
- CMS1218 `expected_test_cases` corrected from 75 to 69 (6 duplicate patient refs in the
  MADiE bundle produce 69 unique DB rows via `ON CONFLICT DO UPDATE`).
- `test_cdr_qicore_implementation_guide_resource` marked skip: HAPI loads the QI-Core IG
  for profile validation but does not persist it as a queryable FHIR resource.
- Added Lucene `io.refresh_interval=100ms` and `reuse_cached_search_results_millis=0` to
  the measure engine test config to eliminate indexing race conditions in integration tests.

### Changed
- Test HAPI FHIR bumped from `v7.4.0` to `v8.6.0-1` in `docker-compose.test.yml`, aligning
  with production and enabling QI-Core STU6 evaluation.
- `STRICT_STU6=0` soft default added to CI workflow for one-week rollout; flip to `1` once
  all 12 connectathon measures pass evaluation.
- Per-patient `$evaluate-measure` tests (`test_connectathon_measures.py`, ~548 cases,
  15-20 min) moved to a dedicated nightly workflow (`connectathon-measures.yml`) with a
  60-minute timeout and manual `STRICT_STU6` override. PR gate now runs in ≤ 20 minutes.

## [0.0.4.0] - 2026-04-19

### Fixed
- Bundle upload now forwards auth headers (Basic or Bearer) when pushing clinical data
  to an external CDR. `push_resources` now accepts an optional `auth_headers` parameter
  that is merged into the POST request, matching the auth behavior already present in
  `run_validation`. External CDRs requiring authentication would previously receive a 401.

## [0.0.3.0] - 2026-04-19

### Added
- **$data-requirements strategy**: Lenny now uses the DEQM-compliant `$data-requirements`
  endpoint to fetch only the clinical resources a measure actually needs, replacing the
  broad `$everything` call. Falls back to `$everything` automatically if the measure engine
  does not support `$data-requirements` or returns an empty list. codeFilter.valueSet entries
  are translated to `code:in={valueSetUrl}` CDR search parameters, and per-resource-type
  failures are isolated so one failing type does not abort all others.
- **Startup bundle loader**: 12 connectathon bundles (9 DBCG FHIR4 + 3 QICore 2025 Hospital Harm)
  load automatically on backend startup. Measures, patients, and expected results are available
  immediately without manual upload. Clinical data now always loads to the active CDR regardless
  of whether it is the default or an external CDR.
- **Comparison view on Results page**: A new comparison table shows each patient's actual
  vs. expected population results side-by-side, including match/mismatch indicators.
  Requires expected results from loaded test bundles.
- **`GET /jobs/{id}/comparison` endpoint**: Returns per-patient comparison data against
  stored expected results for a job.
- **Golden integration test fixtures**: 12 end-to-end regression fixtures under
  `tests/integration/golden/` (EXM104–529, CMS816/1017/1218) assert that each seed bundle
  evaluates to correct population counts after a full HAPI startup.

### Changed
- Orchestrator uses `DataRequirementsStrategy` by default instead of `BatchQueryStrategy`.
- `_fetch_by_requirements` now follows FHIR pagination (`link.next`) for resource type queries,
  preventing silent truncation at 100 resources for patients with large datasets.

### Fixed
- HAPI FHIR 8.6 measure engine now uses Hibernate Search Lucene backend, fixing ValueSet
  expansion failures that caused all-zero population counts.
- Golden integration tests use FHIR batch bundles instead of transaction bundles to avoid
  HAPI reference validation failures when test fixtures contain partial resource graphs.

## [0.0.2.2] - 2026-04-10

### Added
- Multi-CDR connection management: full CRUD for CDR connections with support for
  none/basic/bearer/SMART on FHIR auth, read-only flag, and a default Local CDR row.
  CDR credentials are stamped on each job at creation. Closes #6.

## [0.0.2.1] - 2026-04-10

### Fixed
- Bundle upload path collision: two concurrent uploads of the same filename within the
  same second no longer overwrite each other. A `uuid4` hex token is now embedded in
  the saved filename (`{timestamp}-{uuid4}-{basename}`), guaranteeing unique paths
  regardless of upload timing. Closes #63.

## [0.0.2.0] - 2026-04-09

### Added
- CI/CD gate: new `pr-checks.yml` GitHub Actions workflow runs unit tests (70% coverage
  floor), ruff lint, integration tests, and frontend build on every pull request.
- Deploy workflow now enforces the 70% coverage floor via `--cov-fail-under=70`.
- Golden file integration test pattern: drop a `bundle.json` in `tests/integration/golden/`
  and the measure engine evaluates it automatically on every integration run.
- `docs/testing.md` documents the full testing strategy — coverage targets, test layers,
  golden test format, and CI job reference.
- `backend/requirements-test.txt` separates test-only dependencies from runtime so
  production images stay lean.
- `backend/ruff.toml` enforces consistent import ordering and code style across all
  backend Python files.

### Changed
- CLAUDE.md updated with coverage and lint commands and a reference to `docs/testing.md`.

### Fixed
- Ruff auto-corrected import ordering and formatting across all backend `app/` and
  `tests/` files. No logic changes — cosmetic only.

## [0.0.1.1] - 2026-04-09

### Fixed
- CDR status dot in header now correctly shows green on page load when CDR is connected.
  Previously always showed red because `App.js` read `health.cdr_connected` (a field that
  does not exist) instead of `health.cdr.status`.
- CDR status indicator now propagates the API's three-state value (`connected`,
  `disconnected`, `unknown`) instead of collapsing all non-connected states to `disconnected`.
- System Status section on Settings page now refreshes immediately after a successful
  connection test instead of waiting for the next 30-second poll.

## [0.0.1.0] - 2026-04-09

### Security
- Restrict CORS to explicit origins in production via `ALLOWED_ORIGINS` env var
  (`docker-compose.prod.yml` sets it to `https://${CADDY_HOST}` at deploy time)
- `allow_credentials` is now disabled when origins is wildcard, which is invalid
  per the CORS spec and was previously a misconfiguration in local dev
- Startup warning logged when wildcard CORS is active so accidental production
  deployments are visible in logs

### Added
- `ALLOWED_ORIGINS` environment variable in `backend/app/config.py`; defaults to
  `"*"` so `docker compose up` requires no env changes
- `parse_allowed_origins()` helper in `config.py` — shared by `main.py` and tests
- 8 CORS behavior tests in `backend/tests/test_cors.py` covering wildcard, allowed
  origin, rejected origin, preflight, multi-origin list, empty origins, and negative
  cases

### Fixed
- CORS origin parser now strips trailing slashes to prevent silent mismatches
  (e.g. `https://example.com/` vs `https://example.com`)
- `allow_credentials` guard handles empty origin list correctly
