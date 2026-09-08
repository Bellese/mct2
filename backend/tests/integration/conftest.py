"""Integration test fixtures — run against real HAPI FHIR instances via Docker.

Prerequisites:
    docker compose -f docker-compose.test.yml up -d
"""

import json
import os
import pathlib
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from tests.integration._helpers import fix_valueset_compose_for_hapi
from tests.integration._setup_budget import GATE_TIMINGS, SetupBudget, record_gate

# ---------------------------------------------------------------------------
# Test infrastructure URLs
# ---------------------------------------------------------------------------

TEST_CDR_URL = "http://localhost:8180/fhir"
TEST_MEASURE_URL = "http://localhost:8181/fhir"
TEST_DATABASE_URL = "postgresql+asyncpg://mct2:mct2@localhost:5433/mct2"

SEED_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed"

SKIP_MESSAGE = (
    "Integration test infrastructure not running. Start with: docker compose -f docker-compose.test.yml up -d"
)

# HAPI v8.6.0 with CR enabled registers a DEQM SearchParameter ~40 seconds
# after startup, which triggers an async REINDEX job.  Resources written to HAPI
# (via batch bundle OR individual PUT) during that reindex do not get their
# reference-type search parameters indexed, causing Encounter?patient=… to
# return 0 results and $evaluate-measure to produce all-zero populations.
#
# Fix: after each bulk data load, trigger a fresh $reindex for Encounter,
# Observation, and Condition, then poll until reference-param searches for
# all three return results before allowing tests to proceed.
_REINDEX_POLL_INTERVAL = 1  # seconds between probe checks
_REINDEX_TIMEOUT = 300  # per-gate cap; the shared budget below is the real bound
# CDR has no persistent Lucene; full reindex takes ~4 min on typical hardware.
_CDR_REINDEX_TIMEOUT = 600  # per-gate cap; the shared budget below is the real bound

# One shared wall-clock budget for every blocking gate in setup (#425).
#
# These caps used to be independent, so their worst cases were ADDITIVE:
# 600 (CDR) + 300 (Encounter) + 300 (Observation) + 300 (Condition) = 1500s = 25 min,
# against an Integration job `timeout-minutes` of 20. A slow run therefore could not
# fail cleanly -- GitHub killed the job before any gate hit its own deadline, leaving
# only `The job has exceeded the maximum execution time`. That is the same opaque
# signal that sent #418's investigation to the wrong place.
#
# Sizing, against the 1200s job ceiling:
#     HAPI container readiness         ~160s
#     tests themselves (post-#424)     ~410s
#     checkout / python / docker        ~60s
#     -> setup may claim at most       ~570s
# 480s leaves margin under that and is still ~1.65x the ~290s a healthy run needs
# (the CDR full reindex is ~240s of it). Override for slow hardware rather than
# editing this: INTEGRATION_SETUP_BUDGET_SECONDS.
_SETUP_BUDGET_SECONDS = float(os.environ.get("INTEGRATION_SETUP_BUDGET_SECONDS", "480"))

# HAPI's in-memory ValueSet expansion is capped at 1000 codes (HAPI-0831).  ValueSets
# with >1000 codes must be pre-expanded by HAPI's background scheduler before the CQL
# engine can use them for FHIR searches; otherwise every CQL retrieve returns empty and
# $evaluate-measure produces IP=0 for all patients.
#
# Fix: after loading ValueSets, identify large ones (>900 compose concepts) and poll
# $expand until HAPI's background task completes the pre-expansion.
_VALUESET_EXPANSION_POLL_INTERVAL = 2  # seconds between probe checks
_VALUESET_EXPANSION_TIMEOUT = 600  # seconds before giving up (background task can be slow)


def _wait_for_valueset_expansion(base_url: str, large_valueset_ids: list[str]) -> None:
    """Block until HAPI has background-pre-expanded all specified large ValueSets.

    HAPI's in-memory expansion is capped at 1000 codes.  ValueSets with >1000 codes
    are queued for async pre-expansion by a background scheduler.  After pre-expansion
    completes, ``$expand`` returns HTTP 200 instead of HAPI-0831 500.

    Args:
        base_url: FHIR base URL (e.g. ``http://localhost:8181/fhir``).
        large_valueset_ids: HAPI resource IDs of ValueSets that need pre-expansion.
    """
    import warnings as _warnings

    import httpx as _httpx

    if not large_valueset_ids:
        return

    pending = set(large_valueset_ids)
    deadline = time.monotonic() + _VALUESET_EXPANSION_TIMEOUT

    while pending and time.monotonic() < deadline:
        newly_done: set[str] = set()
        for vs_id in list(pending):
            try:
                # count=1 short-circuits without full expansion; use count=2 so
                # HAPI-0831 fires for any VS with >1 code until background
                # pre-expansion completes and HAPI can serve from the DB.
                resp = _httpx.get(f"{base_url}/ValueSet/{vs_id}/$expand?count=2", timeout=15)
                if resp.status_code == 200:
                    newly_done.add(vs_id)
            except _httpx.RequestError:
                pass
        pending -= newly_done
        if pending:
            time.sleep(_VALUESET_EXPANSION_POLL_INTERVAL)

    if pending:
        _warnings.warn(
            f"HAPI at {base_url} ValueSet pre-expansion did not complete within "
            f"{_VALUESET_EXPANSION_TIMEOUT}s for {len(pending)} ValueSet(s): {sorted(pending)[:5]}. "
            f"Tests may fail with IP=0 if large ValueSets are still unexpanded."
        )


def _trigger_cdr_full_reindex_and_wait(cdr_url: str, budget: SetupBudget) -> None:
    """Trigger a full $reindex on CDR and wait for the job to complete.

    CDR uses an in-memory Lucene backend (no persistent directory) so patient-reference
    searches — required by $everything — return 0 on startup until the index is rebuilt.
    Unlike the measure engine, CDR has no CR/DEQM jobs, so reindexing all types at once
    (including Procedure, MedicationRequest, MedicationAdministration) is safe.

    Completion is detected by the $hapi.fhir.reindex-status endpoint returning a Bundle
    (job report) instead of an OperationOutcome (in-progress).
    """
    import re as _re

    import httpx as _httpx

    headers = {"Content-Type": "application/fhir+json"}

    allotted = budget.allot("cdr-full-reindex", cap=_CDR_REINDEX_TIMEOUT)
    started = time.monotonic()

    r = _httpx.post(f"{cdr_url}/$reindex", json={"resourceType": "Parameters"}, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Full $reindex at {cdr_url} returned {r.status_code}: {r.text[:200]}")

    try:
        diag = r.json().get("issue", [{}])[0].get("diagnostics", "")
        m = _re.search(r"_jobId=([a-f0-9-]+)", diag)
        if not m:
            raise RuntimeError(
                f"No job ID in $reindex response at {cdr_url}, so completion cannot be awaited; "
                "the CDR reference-param index would be left unverified."
            )
        job_id = m.group(1)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not parse $reindex response at {cdr_url}: {exc}") from exc

    deadline = started + allotted
    while time.monotonic() < deadline:
        try:
            status = _httpx.get(f"{cdr_url}/$hapi.fhir.reindex-status?_jobId={job_id}", timeout=10)
            if status.status_code == 200 and status.json().get("resourceType") == "Bundle":
                record_gate("cdr-full-reindex", time.monotonic() - started)
                return
        except Exception:
            pass
        time.sleep(5)

    # Raise, do not warn. An incomplete CDR reference-param index makes $everything
    # return partial data, which surfaces as wrong populations rather than as an
    # error -- the exact misdiagnosis CLAUDE.md's async-indexing section exists to
    # prevent. Failing here names the cause; warning here hides it (#425).
    raise RuntimeError(
        f"CDR full $reindex at {cdr_url} did not complete within its {allotted:.0f}s allotment "
        f"({budget.spent():.0f}s of the {_SETUP_BUDGET_SECONDS:.0f}s setup budget spent). "
        "Continuing would hand the suite a CDR whose reference-param index is known to be "
        "incomplete, producing wrong populations instead of a clear failure."
    )


def _trigger_reindex_and_wait(
    base_url: str, probe_patient_id: str, probe_encounter_id: str, budget: SetupBudget
) -> None:
    """Trigger HAPI $reindex and block until reference search indexes are ready.

    Reindexes Encounter, Observation, and Condition only — the types that caused
    CMS122/124/125 to produce all-zero populations when missing from the Lucene index.
    Two gates must pass before returning:
      1. Encounter?patient= returns results (reference-param indexing ready).
      2. Observation?patient= and Condition?patient= return results (CMS122/124/125
         initial-population criteria depend on these types).

    Procedure, MedicationRequest, and MedicationAdministration are intentionally NOT
    reindexed here.  Triggering $reindex on those types restarts HAPI's async reindex
    jobs; any CMS measure that queries those types shortly afterwards will see 500 errors
    from HAPI mid-evaluation, causing all patient evals to fail.

    The probe resources must already exist in HAPI (written before calling this function).
    """
    import warnings as _warnings

    import httpx as _httpx

    headers = {"Content-Type": "application/fhir+json"}

    # Reindex only the types we gate on (Encounter, Observation, Condition).  Previously only
    # Encounter was reindexed; Observation/Condition were added because CMS122/124/125 depend
    # on them and produce IP=0 when they are missing from the Lucene index.
    #
    # Do NOT reindex Procedure, MedicationRequest, or MedicationAdministration here.
    # Explicitly triggering $reindex for those types causes HAPI to restart their async
    # reindex jobs, returning 500 errors on internal CQL searches until the jobs finish.
    # The startup Lucene rebuild handles them without interference.
    _REINDEX_TYPES = ("Encounter", "Observation", "Condition")
    for resource_type in _REINDEX_TYPES:
        params = {"resourceType": "Parameters", "parameter": [{"name": "type", "valueString": resource_type}]}
        r = _httpx.post(f"{base_url}/$reindex", json=params, headers=headers, timeout=30)
        if r.status_code >= 400:
            # Stays a warning, unlike the gate timeouts below (#425). A failed $reindex
            # POST is not itself evidence of an unusable index -- HAPI's startup Lucene
            # rebuild may already have covered these types -- and the gates that follow
            # verify readiness directly. Raising here would fail runs whose index is
            # actually fine. The gates are the check; this is just context for them.
            _warnings.warn(f"$reindex({resource_type}) at {base_url} returned {r.status_code}: {r.text[:200]}")

    # Gate 1: Encounter reference-param indexing (patient-scoped probe)
    allotted = budget.allot("encounter-gate", cap=_REINDEX_TIMEOUT)
    started = time.monotonic()
    deadline = started + allotted
    while time.monotonic() < deadline:
        resp = _httpx.get(f"{base_url}/Encounter?patient={probe_patient_id}&_count=1", timeout=10)
        if resp.status_code == 200:
            try:
                if resp.json().get("entry"):
                    break
            except Exception:
                pass
        time.sleep(_REINDEX_POLL_INTERVAL)
    else:
        raise RuntimeError(
            f"HAPI at {base_url} reference-param indexing did not complete within its "
            f"{allotted:.0f}s allotment (probe: Encounter?patient={probe_patient_id}); "
            f"{budget.spent():.0f}s of the {_SETUP_BUDGET_SECONDS:.0f}s setup budget spent"
        )
    record_gate("encounter-gate", time.monotonic() - started)

    # Gate 2: Observation and Condition reference-param indexing must be ready.
    # $everything uses patient-reference searches to retrieve these; CMS122/124/125
    # produce IP=0 when their Observation/Condition data is missing.
    # Use the same probe patient as Gate 1 — connectathon patients all have
    # clinical data so a patient?= probe is a genuine reference-param index test
    # (unlike ?_summary=count which HAPI may serve from JPA row-counts, not Lucene).
    for resource_type, search_param in (("Observation", "patient"), ("Condition", "patient")):
        gate = f"{resource_type.lower()}-gate"
        allotted = budget.allot(gate, cap=_REINDEX_TIMEOUT)
        started = time.monotonic()
        deadline = started + allotted
        while time.monotonic() < deadline:
            resp = _httpx.get(
                f"{base_url}/{resource_type}?{search_param}={probe_patient_id}&_count=1",
                timeout=10,
            )
            if resp.status_code == 200:
                try:
                    if resp.json().get("entry"):
                        break
                except Exception:
                    pass
            time.sleep(_REINDEX_POLL_INTERVAL)
        else:
            # Raise, do not warn (#425). This used to warn "CMS measures depending on
            # {type} may return IP=0" and then proceed into exactly that -- naming the
            # consequence and causing it in the same breath. A pytest warning is lost in
            # a 700-line CI log, and the all-zero populations that follow read as a
            # product bug, which is the misdiagnosis CLAUDE.md's async-indexing triage
            # section exists to prevent. Gate 1 already raised for the same class of
            # failure; the asymmetry was not defensible.
            raise RuntimeError(
                f"HAPI at {base_url} {resource_type} reference-param index not ready within its "
                f"{allotted:.0f}s allotment (probe: {resource_type}?{search_param}="
                f"{probe_patient_id}); {budget.spent():.0f}s of the {_SETUP_BUDGET_SECONDS:.0f}s "
                f"setup budget spent. CMS measures depending on {resource_type} would return IP=0."
            )
        record_gate(gate, time.monotonic() - started)


# ---------------------------------------------------------------------------
# Session-scoped: verify infrastructure is reachable
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _require_infrastructure():
    """Skip the entire integration test session if HAPI FHIR instances are unreachable."""
    import httpx as _httpx  # sync for setup check

    for url in (
        f"{TEST_CDR_URL}/metadata",
        f"{TEST_MEASURE_URL}/metadata",
    ):
        try:
            resp = _httpx.get(url, timeout=10)
            resp.raise_for_status()
        except Exception:
            pytest.skip(SKIP_MESSAGE, allow_module_level=True)


# ---------------------------------------------------------------------------
# FHIR URL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cdr_url() -> str:
    return TEST_CDR_URL


@pytest.fixture(scope="session")
def measure_url() -> str:
    return TEST_MEASURE_URL


# ---------------------------------------------------------------------------
# Seed data loading (once per session)
# ---------------------------------------------------------------------------


def _make_seed_tx_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap seed resources in a FHIR batch bundle with PUT entries."""
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": r,
                "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
            }
            for r in resources
            if "resourceType" in r and "id" in r
        ],
    }


@pytest.fixture(scope="session", autouse=True)
def _load_seed_data(_require_infrastructure):
    """Load measure-bundle.json and patient-bundle.json into the test HAPI instances.

    Loading strategy:
    - measure-bundle.json → measure server only (Measure + Libraries + ValueSets)
      ValueSets with compose sub-ValueSet references are patched to use direct code
      lists so HAPI can expand them without missing sub-ValueSets (HAPI v8.6.0
      ignores the pre-computed expansion element).
    - patient-bundle.json → CDR (canonical home) AND measure server ($evaluate-measure
      resolves patient data from the same HAPI instance it runs on)

    After loading, triggers a HAPI $reindex on both servers and waits for
    reference-type search parameters to be indexed.  HAPI v8.6.0 with CR enabled
    registers a DEQM SearchParameter shortly after startup, causing a background
    REINDEX that prevents patient-reference searches from working until the reindex
    settles.
    """
    import httpx as _httpx

    # One budget for both paths below. Opened here, before the prebaked branch, so
    # every gate in either path draws down the same allowance (#425).
    budget = SetupBudget(_SETUP_BUDGET_SECONDS)

    if os.environ.get("HAPI_PREBAKED") == "1":
        # Images are pre-seeded; skip loading + reindex + ValueSet expansion.
        # Smoke probe: if Patient count is 0 the baked image is corrupt — fail fast.
        for base_url, label in [(TEST_CDR_URL, "CDR"), (TEST_MEASURE_URL, "measure")]:
            try:
                resp = _httpx.get(f"{base_url}/Patient?_summary=count", timeout=15)
                total = resp.json().get("total", 0) if resp.status_code == 200 else 0
            except Exception:
                total = 0
            if total == 0:
                raise RuntimeError(
                    f"HAPI_PREBAKED=1 but {label} HAPI at {base_url} has no Patient resources. "
                    f"The pre-baked image may be corrupt or the wrong image was pulled."
                )

        # HAPI's /fhir/metadata responds before Hibernate Search finishes opening
        # its Lucene index for writes.  Resources written too early (e.g. in
        # test_upload_and_list_measure) won't appear in search results.  Trigger
        # an explicit $reindex and wait for it to settle — the same stabilization
        # the non-prebaked path gets from _trigger_reindex_and_wait after seeding.
        probe_patient_id = None
        probe_encounter_id = None
        try:
            enc_resp = _httpx.get(f"{TEST_CDR_URL}/Encounter?_count=1", timeout=15)
            if enc_resp.status_code == 200:
                entries = enc_resp.json().get("entry", [])
                if entries:
                    enc = entries[0]["resource"]
                    probe_encounter_id = enc.get("id")
                    probe_patient_id = enc.get("subject", {}).get("reference", "").removeprefix("Patient/")
        except Exception:
            pass

        # CDR: full reindex with job-completion gating — covers all types including
        # Procedure, MedReq, MedAdmin which $everything needs. CDR has no cr.enabled
        # so reindexing all types at once is safe here.
        _trigger_cdr_full_reindex_and_wait(TEST_CDR_URL, budget)
        # MEASURE: Enc/Obs/Cond only — explicit reindex of Proc/MedReq/MedAdmin causes
        # HAPI to restart async jobs, producing 500 errors on in-flight CQL evaluations.
        if probe_patient_id and probe_encounter_id:
            _trigger_reindex_and_wait(TEST_MEASURE_URL, probe_patient_id, probe_encounter_id, budget)
        return

    measure_bundle_path = SEED_DIR / "measure-bundle.json"
    patient_bundle_path = SEED_DIR / "patient-bundle.json"

    headers = {"Content-Type": "application/fhir+json"}

    # Load measure bundle into measure engine, patching ValueSets first
    if measure_bundle_path.exists():
        with open(measure_bundle_path) as f:
            raw_bundle = json.load(f)
        resources = [e["resource"] for e in raw_bundle.get("entry", []) if "resource" in e]
        # Separate Measures (need individual PUT to preserve backbone element IDs)
        # from everything else (load via batch bundle)
        measures = [r for r in resources if r.get("resourceType") == "Measure"]
        non_measures = [r for r in resources if r.get("resourceType") != "Measure"]
        # Patch ValueSets with sub-ValueSet compose refs before loading
        non_measures = fix_valueset_compose_for_hapi(non_measures)
        if non_measures:
            tx = _make_seed_tx_bundle(non_measures)
            resp = _httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=120)
            resp.raise_for_status()
        for m in measures:
            url = f"{TEST_MEASURE_URL}/{m['resourceType']}/{m['id']}"
            resp = _httpx.put(url, json=m, headers=headers, timeout=60)
            resp.raise_for_status()

    # Load patient bundle into CDR and measure server
    probe_patient_id = None
    probe_encounter_id = None
    if patient_bundle_path.exists():
        with open(patient_bundle_path) as f:
            patient_bundle = json.load(f)
        for target, label in [(TEST_CDR_URL, "CDR"), (TEST_MEASURE_URL, "measure server")]:
            resp = _httpx.post(target, json=patient_bundle, headers=headers, timeout=120)
            resp.raise_for_status()
        # Find a patient+encounter pair to use as the reindex probe
        entries = patient_bundle.get("entry", [])
        encounter_entries = [e["resource"] for e in entries if e.get("resource", {}).get("resourceType") == "Encounter"]
        if encounter_entries:
            first_enc = encounter_entries[0]
            probe_encounter_id = first_enc.get("id")
            probe_patient_id = first_enc.get("subject", {}).get("reference", "").removeprefix("Patient/")

    # Trigger $reindex on both servers and wait for reference search params to settle
    if probe_patient_id and probe_encounter_id:
        for target in (TEST_CDR_URL, TEST_MEASURE_URL):
            _trigger_reindex_and_wait(target, probe_patient_id, probe_encounter_id, budget)


# ---------------------------------------------------------------------------
# Database engine & session (session-scoped engine, function-scoped sessions)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def integration_engine():
    """Create an async engine pointing at the test PostgreSQL instance."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    # Drop all tables on teardown
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def integration_session_factory(integration_engine):
    """Return a session factory bound to the integration database."""
    return async_sessionmaker(integration_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(integration_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session for a single test, with cleanup."""
    async with integration_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def truncate_tables(integration_session_factory):
    """Truncate job/batch/result tables between tests (keep HAPI data loaded).

    Opt-in: only add this fixture to tests that write backend DB rows (jobs,
    batches, results, etc.).  Read-only tests do not need it.
    """
    yield
    async with integration_session_factory() as session:
        for table in (
            "validation_results",
            "validation_runs",
            "expected_results",
            "bundle_uploads",
            "measure_results",
            "batches",
            "jobs",
            "cdr_configs",
            "mcs_configs",
        ):
            await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        await session.commit()


# ---------------------------------------------------------------------------
# FastAPI test client wired to real HAPI + test PostgreSQL
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def integration_client(integration_session_factory):
    """Provide an httpx AsyncClient talking to the FastAPI app.

    The database dependency is overridden to use the test PostgreSQL.
    Environment variables are patched so fhir_client talks to test HAPI instances.
    Background tasks that call async_session() directly (e.g. _run_factory_reset)
    are also redirected to the test DB so they don't try to resolve 'db:5432'.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.db import get_session
    from app.routes import groups, health, jobs, measures, results, settings, validation

    test_app = FastAPI()
    test_app.include_router(groups.router)
    test_app.include_router(health.router)
    test_app.include_router(jobs.router)
    test_app.include_router(measures.router)
    test_app.include_router(results.router)
    test_app.include_router(settings.router)
    test_app.include_router(validation.router)

    async def _override_get_session():
        async with integration_session_factory() as session:
            yield session

    test_app.dependency_overrides[get_session] = _override_get_session

    @asynccontextmanager
    async def _bg_session():
        async with integration_session_factory() as session:
            yield session

    # Patch settings so fhir_client uses the test HAPI instances.
    # Also patch async_session so background tasks (which bypass DI) hit the test DB.
    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.routes.settings.async_session", _bg_session),
    ):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Report per-gate setup timings at end of run.

    Written through the terminal reporter rather than print() because CI does not
    pass -s: a print during fixture setup is captured and never surfaces on the
    runs where a stalled gate actually needs diagnosing (#425).
    """
    if not GATE_TIMINGS:
        return
    terminalreporter.write_sep("=", "integration setup gates")
    total = 0.0
    for name, seconds in GATE_TIMINGS:
        terminalreporter.write_line(f"{seconds:8.0f}s  {name}")
        total += seconds
    terminalreporter.write_line(
        f"{total:8.0f}s  total gate wait, budget {_SETUP_BUDGET_SECONDS:.0f}s "
        f"(override: INTEGRATION_SETUP_BUDGET_SECONDS)"
    )
