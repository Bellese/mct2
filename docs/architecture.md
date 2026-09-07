# Architecture — Lenny

## Service Map

| Service | Image (compose default) | Role | Exposed port |
|---------|-------|------|-------------|
| frontend | local build | React web UI | 3001 |
| backend | local build | FastAPI orchestrator | 8000 |
| db | postgres:16-alpine | Job tracking, results, config | internal (5432) |
| hapi-fhir-cdr | hapiproject/hapi:v8.8.0-1 | Default clinical data repository | internal (8080) |
| hapi-fhir-measure | hapiproject/hapi:v8.8.0-1 | Measure calculation engine | internal (8080) |
| seed | local build | One-time data loader (exits after run) | none |

The CDR and Measure Engine are intentionally separate. Both are user-configurable in Settings: multiple CDR and MCS (Measure Calculation Server) connections can be saved, with one of each active at a time. The bundled `hapi-fhir-measure` service is the default MCS — the only one with `hapi.fhir.cr.enabled=true` — but attendees can point at their own MCS via Settings → MCS Connections (issue #12).

### Per-CDR connection settings

**Per-CDR settings.** Each CDR connection carries `request_timeout_seconds` (HTTP timeout) and `max_bundle_entries` (optional cap on entries per push). When `max_bundle_entries` is set, `push_resources()` partitions the entry list into chunks of ≤N and POSTs each chunk sequentially; Patients-first ordering is preserved across chunk boundaries. Use this for CDRs that enforce a per-bundle entry cap (e.g., Firely Sandbox = 200, AWS HealthLake's bundle size limits). Default `null` = single-shot push (current behavior).

### Compose modes: vanilla vs. prebaked

Two compose layouts share these services:

- **Local dev (`docker-compose.yml` alone)** — pulls vanilla `hapiproject/hapi:v8.8.0-1`. HAPI loads QI-Core / US-Core / CQL IGs at runtime via `hapi.fhir.implementationguides.*` env vars (see *HAPI FHIR Configuration* below). Cold-start is slow because IG packages download from the HL7 registry on first boot.
- **CI (`docker-compose.yml` + `docker-compose.prebaked.yml`)** — pulls pre-baked `ghcr.io/bellese/lenny-hapi-{cdr,measure}` images that already contain the IGs and connectathon bundles, skipping the runtime IG load (PR #199, Phase 3). No auth is required: both packages are public (issue #200) — see `docs/runbooks/ghcr-pull-auth.md`.
- **Production** — runs the compose default, `hapiproject/hapi:v8.8.0-1` (same as local dev, pulled from Docker Hub). `scripts/deploy-prod.sh` uses only `docker-compose.yml` + `docker-compose.prod.yml` and never appends `docker-compose.prebaked.yml`, so the `cdrdata`/`measuredata` named volumes mount over `/data/hapi` and shadow whatever H2 store the image carries. The `seed` service runs on every deploy and POSTs the connectathon bundles into HAPI; the volumes persist across redeploys so the re-POST is a no-op for already-loaded resources (`If-None-Exist`). Whether to switch prod to pre-baked is an open question — see `docs/decisions.md` or ask Sutton.

The local fast path is to set `HAPI_CDR_IMAGE` and `HAPI_MEASURE_IMAGE` in `.env` so vanilla compose reuses prebaked images without the prebaked overlay (see `.env.example`).

For a detailed end-to-end comparison of local and prod deploy mechanics, see **`docs/runbooks/local-vs-prod-deploy.md`**.

### Resetting state

To wipe Lenny back to a known-good blank state (before a demo, after a bad test run, etc.), see **`docs/runbooks/factory-reset.md`**. The admin panel (Settings → Admin → Factory Reset) provides a UI-native path; the runbook also documents `curl` and `docker volume rm` fallbacks.

## Backend Structure

```
backend/app/
  main.py           FastAPI app entry point, router registration
  config.py         pydantic-settings configuration (see Environment Variables below)
  db.py             async SQLAlchemy engine + session factory
  dependencies.py   FastAPI dependency providers (DB session, config lookups)

  models/
    job.py          Job, MeasureReport (each Job snapshots cdr_id + mcs_id at creation)
    validation.py   ExpectedResult, ValidationRun
    config.py       CDRConfig + ConnectionConfigMixin (URL, auth, encrypted credentials)
    mcs_config.py   MCSConfig (parallel of CDRConfig; is_read_only comes from the
                    shared mixin as of #396, plus an MCS-only wipe_before_job flag
                    gating the destructive pre-job wipe — see ADR-012)
    base.py         SQLAlchemy declarative base

  routes/
    health.py       GET /health
    jobs.py         POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel,
                    GET /jobs/{id}/measure-report (FHIR Bundle of individual MeasureReports),
                    GET /jobs/{id}/comparison (actual vs. expected population counts)
    measures.py     GET /measures, POST /measures/upload
    results.py      GET /results, GET /results/{job_id}
    groups.py       GET /api/groups, POST /api/groups/{id}/evaluate — experimental.
                    Admin-gated (`groups_enabled`); lists CQL-evaluatable Groups from
                    the active CDR and invokes the CQL IG `Group/<id>/$evaluate` op.
                    Architecturally independent from the measure pipeline; enforced
                    by `tests/test_groups_independence.py`. (#322)
    settings.py     /settings/admin (feature flags), seeds CDR + MCS routers from connection_factory
    connection_factory.py
                    Generic per-kind CRUD + activate + test-connection. Mounted twice:
                    /settings/connections (CDR) and /settings/mcs-connections (MCS).
                    Plus /settings/mcs-connections/{id}/probe (deep $data-requirements probe).
    validation.py   POST /validation/upload, GET /validation/runs

  services/
    orchestrator.py      Core job execution. Pulls patients, builds the job's submission
                         workflow (see workflows.py) once per job, runs it per-patient for
                         phase 1, then runs $evaluate-measure in batches for phase 2, storing
                         MeasureReports. Group filtering via group_id param. Reads live
                         CDR credentials from cdr_configs via job.cdr_id FK; routes
                         $evaluate-measure at the per-job MCS snapshot (job.mcs_url, falling
                         back to settings.MEASURE_ENGINE_URL for legacy rows).
    workflows.py         Per-job submission-workflow strategies (SubmissionWorkflow ABC),
                         selected by `Job.workflow` at job creation and built once per job
                         before any patient work: `direct_load` (today's behavior — env-
                         configured gather via DataAcquisitionStrategy, then a Bundle of PUTs
                         straight to the measure engine) and `deqm_submit_data` (targeted
                         $data-requirements queries → a DEQM Data Exchange MeasureReport
                         (deqm.py) → `Measure/$submit-data` → the same $evaluate-measure phase
                         2). Both label per-patient transfer failures via TransferPhaseError,
                         surfaced as MeasureResult.error_phase ("gather" for direct_load;
                         "gather" or "submit" for deqm_submit_data).
    fhir_client.py       All FHIR server communication. DataAcquisitionStrategy ABC with two
                         implementations: BatchQueryStrategy (paginated /Patient + $everything)
                         and DataRequirementsStrategy (DEQM spec — calls $data-requirements on
                         the measure engine, then fetches only the required resource types from
                         the CDR; falls back to $everything on any failure). $data-requirements
                         is fetched once per job and memoised on the strategy instance: the call
                         compiles the measure's CQL in the engine, and issuing it per patient
                         drove the engine past its container memory limit at 319 patients. Each
                         type is queried with its `code:in=` valueset filter first and then
                         WITHOUT the filter if that query fails — a VSAC canonical the CDR never
                         loaded returns HAPI-2788 rather than an empty set, and treating that as
                         "no such resources" silently changes populations. Also home to the
                         DEQM $submit-data capability probe: detect_submit_data_mode() reads the
                         MCS CapabilityStatement at job creation and stamps `Job.submit_data_mode`
                         as `stu5` or `base-fallback`, which decides which URL shape/envelope
                         submit_data() uses. A mis-probed `stu5` that 400s/404s on the real POST
                         downgrades to base mode at runtime and retries once (workflows.py); the
                         stored `Job.submit_data_mode` still reflects the original probe verdict.
    bundle_loader.py     Startup bundle loader. Called once during FastAPI lifespan. Scans
                         seed/connectathon-bundles/, waits for HAPI readiness, then loads each
                         .json file via triage_test_bundle (Measure/Library → MCS, clinical
                         resources → CDR, test-case MeasureReports → ExpectedResult table).
    credential_crypto.py EncryptedJSON SQLAlchemy TypeDecorator (Fernet/AES-128-CBC + HMAC-SHA256)
                         for CDR auth credentials. Lazy Fernet singleton reads
                         /run/secrets/cdr_fernet_key first, falls back to CDR_FERNET_KEY env var
                         (immediately popped to prevent subprocess leakage). self_check() runs at
                         startup to verify the key is valid.
    validation.py        Test bundle parsing, ExpectedResult comparison, pass/fail logic.
    worker.py            Background task queue, priority ordering, job lifecycle management.
```

## Frontend Structure

```
frontend/src/
  App.js              Main app with react-router-dom v6 routing
  pages/
    JobsPage.js       Create and monitor calculation jobs
    MeasuresPage.js   Upload and view FHIR Measure bundles
    ResultsPage.js    Aggregate population summaries + patient drill-down
    SettingsPage.js   CDR + MCS connection management (two stacked sections), admin tab
    ValidationPage.js Upload test bundles, view pass/fail results
    GroupsPage.js     Experimental: list CQL-evaluatable Groups on the active CDR; per-row
                      `$evaluate` button expands into an accordion of resolved members.
                      Admin-gated; redirects to `/measures` when disabled. (#322)
  components/
    ComparisonView.js  Per-patient actual vs. expected population comparison panel
    ConnectionModal.js Kind-driven connection modal (KIND_SPECS for cdr/mcs); shared by both connection sections
    PatientDetail.js   Per-patient result expansion panel
    ProgressBar.js     Job progress indicator
    Toast.js           Notification component
  api/
    client.js         Axios-based backend API client
```

Each page has a co-located CSS Module (`PageName.module.css`). The app is plain JavaScript — no TypeScript.

## Data Flow

```
User (browser)
  │  HTTP
  ▼
FastAPI (backend:8000)
  │  async httpx
  ├──► CDR (hapi-fhir-cdr or user's external FHIR server)
  │     Patient, Group resources
  │
  ├──► Measure Engine (hapi-fhir-measure)
  │     POST /fhir/$evaluate-measure
  │     Returns MeasureReport resources
  │
  └──► PostgreSQL (db)
        Job status, MeasureReports, ExpectedResults, AppConfig
```

The orchestrator fetches patients from the CDR (all patients, or group members if `group_id` set), batches them, pushes each batch to the Measure Engine via `$evaluate-measure`, and stores the resulting MeasureReports in PostgreSQL. The worker service manages job state and handles background execution.

## HAPI FHIR Configuration

### Implementation Guide Installation (QI-Core STU6)

Both HAPI instances (CDR and Measure Engine) are configured to install the QI-Core 6.0.0 IG and its dependencies on startup via `hapi.fhir.implementationguides.*` environment variables. HAPI downloads the npm packages from the HL7 registry during first boot and caches them in the H2 volume.

The six env vars (identical on both services):

| Variable | Value |
|----------|-------|
| `hapi.fhir.implementationguides.qicore.name` | `hl7.fhir.us.qicore` |
| `hapi.fhir.implementationguides.qicore.version` | `6.0.0` |
| `hapi.fhir.implementationguides.uscore.name` | `hl7.fhir.us.core` |
| `hapi.fhir.implementationguides.uscore.version` | `6.1.0` |
| `hapi.fhir.implementationguides.cql.name` | `hl7.fhir.uv.cql` |
| `hapi.fhir.implementationguides.cql.version` | `1.0.0` |

What this does: once HAPI starts, these IGs are registered in the server's package registry and their profiles, value sets, and code systems become available for resource validation and CQL evaluation. Both the CDR and the Measure Engine carry the same IG set so that profiles are consistent across validation and calculation.

**Verifying IG installation.** After startup you can confirm the IGs loaded correctly:

```bash
# List installed IGs via CapabilityStatement (look for qi-core in implementationGuide[])
curl -s http://localhost:8180/fhir/metadata | jq '.implementationGuide'

# Or query the ImplementationGuide resource directly
curl -s "http://localhost:8180/fhir/ImplementationGuide?name=qicore" | jq '.entry[].resource.version'
```

Port mapping for local dev: CDR is exposed on `8180`, Measure Engine on `8181` (via `docker-compose.test.yml`). In the main stack both run on internal port `8080`.

### Runtime Settings

Critical settings and why they are set:

| Setting | Service | Value | Reason |
|---------|---------|-------|--------|
| `hapi.fhir.cr.enabled` | measure | `true` | Enables CQL/Clinical Reasoning support required for `$evaluate-measure` |
| `hapi.fhir.client_id_strategy` | cdr | `ANY` | Accepts CMS numeric patient IDs (not just UUIDs) |
| `hapi.fhir.allow_external_references` | cdr | `true` | Required for CMS FHIR bundles with cross-resource references |
| `hapi.fhir.defer_indexing_for_codesystems_of_size` | both | `0` | Disables deferred indexing to avoid startup latency |
| `spring.jpa.properties.hibernate.search.enabled` | measure | `true` | Enables Hibernate Search / Lucene full-text indexing (required for `$data-requirements` and value-set expansion lookups) |
| `spring.jpa.properties.hibernate.search.backend.type` | measure | `lucene` | Uses embedded Lucene backend (no external search cluster needed) |

Storage: both instances use H2 file-based storage under `/data/hapi` (mounted as Docker volumes). This is appropriate for local/demo use. Production deployments should use external Postgres.

### Memory Sizing

Both HAPI services are memory-capped to prevent the JVM from consuming host RAM uncontrolled. JVM RSS routinely runs 400–600 MB above the heap cap (`-Xmx`) due to metaspace, NIO/Lucene off-heap buffers, thread stacks, and CQL classloader overhead.

| Environment | Service | `mem_limit` | `-Xmx` | Compose file |
|---|---|---|---|---|
| Dev + CI (prebaked) | CDR | 2 GB | 1 GB | `docker-compose.yml` |
| Dev + CI (prebaked) | Measure Engine | 6 GB | 4 GB | `docker-compose.prebaked.yml` |
| Prod | CDR | 3 GB | 2 GB | `docker-compose.prod.yml` |
| Prod | Measure Engine | 4 GB | 2 GB | `docker-compose.prod.yml` |

Prod runs on a **t3.large** EC2 instance (8 GiB RAM) to fit both containers plus DB, backend, and Caddy.

Both services are configured with `-XX:+ExitOnOutOfMemoryError` so an OOM causes immediate process exit (rather than leaving a degraded JVM alive), and `restart: unless-stopped` so Docker auto-recovers the container without manual intervention. `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/hapi/heapdump.hprof` captures a heap dump in the named volume for post-mortem analysis (`docker cp` to retrieve it).

## Environment Variables

Defined in `backend/app/config.py`. All overridable via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://mct2:mct2@db:5432/mct2` | Async PostgreSQL connection string |
| `MEASURE_ENGINE_URL` | `http://hapi-fhir-measure:8080/fhir` | Measure Engine FHIR base URL |
| `DEFAULT_CDR_URL` | `http://hapi-fhir-cdr:8080/fhir` | Default CDR FHIR base URL |
| `BATCH_SIZE` | `100` | Patients per `$evaluate-measure` batch |
| `MAX_WORKERS` | `4` | Concurrent job worker threads |
| `MAX_RETRIES` | `3` | Retry attempts for failed FHIR requests |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ALLOWED_ORIGINS` | `"*"` | Comma-separated CORS allowed origins; `"*"` for wildcard (local dev default). Set to `https://${CADDY_HOST}` in production via `docker-compose.prod.yml`. |
| `CDR_FERNET_KEY` | _(none)_ | Fernet key for encrypting CDR auth credentials at rest. Required in production. Generate with: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. In prod, injected via Docker secret at `/run/secrets/cdr_fernet_key` (takes priority over env var). See `.env.example`. |

### Prod secrets

Production secrets are stored in **AWS SSM Parameter Store** under `/leonard/prod/`:

| SSM Path | Secret | Consumer |
|---|---|---|
| `/leonard/prod/POSTGRES_PASSWORD` | DB superuser password | backend, db (first-init) |
| `/leonard/prod/CDR_FERNET_KEY` | Fernet key for CDR credential encryption | backend (credential_crypto.py) |

**Instance profile:** `leonard-ec2-prod` (attached to the prod EC2 instance) grants `ssm:GetParametersByPath` on `/leonard/prod/*` with `kms:ViaService` scoped to SSM only.

**Boot flow:** On every deploy, `scripts/fetch-prod-secrets.sh` reads SSM and writes values to `/run/leonard/env` (tmpfs, mode 0600, cleared on reboot). `deploy-prod.sh` extracts `POSTGRES_PASSWORD` to `/run/leonard/POSTGRES_PASSWORD` (mode 0600) and `CDR_FERNET_KEY` to `/run/leonard/CDR_FERNET_KEY` (mode **0644** — see the note below). `docker-compose.prod.yml` mounts both as Docker secrets — the `backend` service reads `POSTGRES_PASSWORD` via `/run/secrets/postgres_password` (assembled into `DATABASE_URL` by `backend/docker-entrypoint.sh`) and `CDR_FERNET_KEY` via `/run/secrets/cdr_fernet_key` (read by `credential_crypto.py` at startup). The `db` service reads `POSTGRES_PASSWORD` via `POSTGRES_PASSWORD_FILE`. `scripts/reconcile-db-password.sh` then runs `ALTER ROLE mct2 PASSWORD :'newpw'` to synchronize the DB volume's embedded password.

**Why the two secret files have different modes.** `POSTGRES_PASSWORD` is read by `backend/docker-entrypoint.sh` **as root**, before `exec gosu app` drops to uid 1000, so `0600` is readable at the moment it matters. `CDR_FERNET_KEY` is read later — by `services/credential_crypto.py` at application startup, i.e. *after* the privilege drop. Compose bind-mounts file-based secrets with host permissions intact, so `0600 root:root` would be unreadable to the app user and the backend would fail to start. The `0644` at `deploy-prod.sh:149` is therefore load-bearing, not an oversight: changing it to `0600` breaks prod. It is still looser than it should be (world-readable on the host); tightening it means granting uid 1000 without granting everyone (`-g 1000 -m 0640`) or reading the key as root in the entrypoint the way the DB password already is.

**Rotation:** Update the SSM param, then run `scripts/deploy-prod.sh`. The backend must be restarted for the new `DATABASE_URL` to take effect (deploy-prod.sh handles this). See `docs/runbooks/rotate-db-password.md`.

**Note:** Restarting the backend without running `deploy-prod.sh` first will cause `InvalidPasswordError` if the SSM param was rotated. Always use `deploy-prod.sh`.

## Test Infrastructure

**Unit tests** (`backend/tests/test_*.py`):
- Use pytest with pytest-asyncio
- Database: SQLite in-memory (via `aiosqlite`)
- FHIR servers: mocked with `respx` (async HTTP mocking)
- Run with: `cd backend && python -m pytest tests/ --ignore=tests/integration -v`

**Integration tests** (`backend/tests/integration/`):
- Require live infrastructure: Postgres (port 5433), HAPI CDR (port 8180), HAPI Measure (port 8181)
- Spun up via `docker-compose.test.yml`
- Run with: `./scripts/run-integration-tests.sh`
- Marked with `@pytest.mark.integration`
