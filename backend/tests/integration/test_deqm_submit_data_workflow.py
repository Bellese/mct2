"""End-to-end test: deqm_submit_data workflow against real HAPI (base-fallback path)."""

import os
from contextlib import ExitStack
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from app.models.job import MeasureResult
from app.services.orchestrator import run_job
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _require_prebaked_stack():
    """Skip this module unless the prebaked HAPI images are in use.

    Two independent reasons, both pre-existing in spirit:

    - The vanilla seed (`seed/measure-bundle.json`) carries only CMS122, so the
      CMS130 parametrisation below has never been able to run without prebaked
      images -- it fails with "Measure does not exist on the active measure
      calculation server". This module was already prebaked-only in practice; it
      just failed confusingly instead of skipping.
    - The jobs here are now scoped to each measure's Group (see `_create_and_run`)
      and the vanilla seed contains no Group resources at all, so those jobs would
      404. `test_full_jobs_pipeline.py` carries this same guard for exactly this
      reason.

    Skipping loudly beats a 404 that reads like a product bug.
    """
    if os.environ.get("HAPI_PREBAKED") != "1":
        pytest.skip(
            "test_deqm_submit_data_workflow requires HAPI_PREBAKED=1 (prebaked images "
            "carry the connectathon measures and their Groups). Run via: "
            "USE_PREBAKED=1 ./scripts/run-integration-tests.sh "
            "tests/integration/test_deqm_submit_data_workflow.py"
        )


MEASURE_ID = "CMS122FHIRDiabetesAssessGreaterThan9Percent"
DEFAULT_PERIOD = ("2025-01-01", "2025-12-31")

# Carries a denominator-exclusion behind a ValueSet the CDR cannot resolve --
# the case that exposed the silent under-fetch in targeted gathering. The
# period is 2026 deliberately: the hospice ServiceRequest driving the exclusion
# is authoredOn 2026-01-02, so under the default 2025 period it falls outside
# the measurement period, the exclusion never fires, and this case passes just
# as happily with the bug present as without it.
EXCLUSION_MEASURE_ID = "CMS130FHIRColorectalCancerScreening"
EXCLUSION_PERIOD = ("2026-01-01", "2026-12-31")
# The patient carrying that hospice ServiceRequest. Asserted present in the
# CMS130 results below so that scoping the job to a Group can never quietly
# drop the one patient this parametrisation exists to cover.
EXCLUSION_PATIENT_ID = "b70f2fc0-3254-4240-af70-793cd1bc90b2"


@pytest.fixture(scope="module", autouse=True)
def _warm_hapi_search_parameters():
    """Force HAPI's lazy SearchParameter registration to finish before concurrency.

    HAPI registers its DEQM SearchParameters ~40s after startup (see
    `scripts/smoke_connectathon.py`). The first *concurrent* batch of work against
    a fresh measure engine races that indexing, and the losing requests come back
    `409 HAPI-0550: HAPI-0825 ... client-assigned ID constraint failure`.
    `app/services/validation.py` documents the same race and fixes it the same
    way: run one serial evaluation per measure so indexing completes in a
    single-threaded context before any concurrency starts.

    This module needs the guard more than most, because `_run_patches`
    deliberately sets BATCH_SIZE=25 to force concurrent submission. Without the
    warmup the race is live for whichever job runs first, and that need not be the
    DEQM one: it surfaced as a `direct_load` transfer failure that broke
    `test_deqm_job_matches_direct_load_populations` on the CMS122 case, while DEQM
    itself evaluated the same patient cleanly.
    """
    import httpx as _httpx

    try:
        resp = _httpx.get(f"{TEST_MEASURE_URL}/Patient", params={"_count": "1"}, timeout=30)
        resp.raise_for_status()
        entries = resp.json().get("entry", [])
    except _httpx.HTTPError:
        # Asserting infrastructure health is `_require_infrastructure`'s job, not
        # this fixture's. A warmup that cannot run must not mask that error.
        return
    if not entries:
        return
    patient_id = entries[0]["resource"]["id"]

    for measure_id, (period_start, period_end) in (
        (MEASURE_ID, DEFAULT_PERIOD),
        (EXCLUSION_MEASURE_ID, EXCLUSION_PERIOD),
    ):
        try:
            _httpx.get(
                f"{TEST_MEASURE_URL}/Measure/{measure_id}/$evaluate-measure",
                params={
                    "periodStart": period_start,
                    "periodEnd": period_end,
                    "subject": f"Patient/{patient_id}",
                },
                timeout=120,
            )
        except _httpx.HTTPError:
            # Whether this particular evaluation succeeds is irrelevant. The only
            # job here is to make HAPI finish SearchParameter indexing serially;
            # the real assertions live in the tests below.
            continue


async def _group_member_count(group_id: str) -> int:
    """Members enumerated on the CDR's synthesized Group for this measure."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{TEST_CDR_URL}/Group/{group_id}")
        resp.raise_for_status()
        return len(resp.json().get("member", []))


def _run_patches(integration_session_factory):
    return (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.orchestrator.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.fhir_client.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.fhir_client.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MAX_RETRIES", 1),
        # 25, not 100: with a real 300+-patient connectathon panel this puts
        # every patient into ONE batch at 100, so no concurrency ever occurs
        # and the population-parity test can't catch concurrent-submission
        # bugs (it didn't — see the reporter-Organization fix in this
        # commit). A small batch size forces multiple batches to run under
        # asyncio.Semaphore(MAX_WORKERS) + asyncio.gather concurrently
        # against real HAPI.
        patch("app.services.orchestrator.settings.BATCH_SIZE", 25),
        patch("app.services.orchestrator.async_session", integration_session_factory),
    )


async def _create_and_run(
    integration_client,
    integration_session_factory,
    workflow: str,
    measure_id: str = MEASURE_ID,
    period: tuple[str, str] = DEFAULT_PERIOD,
) -> dict:
    # group_id, not an unscoped job. Without it `run_job` falls through to
    # BatchQueryStrategy and evaluates the ENTIRE 319-patient CDR against one
    # measure -- ~260 of those patients come from other measures' bundles and
    # contribute nothing but runtime. `seed/load_seed_data.py` synthesizes one
    # Group per bundle with `Group.id == measure_id`, so the measure's own
    # cohort is addressable by the measure id itself.
    #
    # This is what made the Integration job exceed its CI ceiling (#418): five
    # full-panel jobs in this file were 1,595 patient evaluations where 296 do
    # the same work. Parity is unaffected -- both sides of the comparison run
    # the identical scoped panel.
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": measure_id,
            "period_start": period[0],
            "period_end": period[1],
            "cdr_url": TEST_CDR_URL,
            "group_id": measure_id,
            "workflow": workflow,
        },
    )
    assert resp.status_code == 201, f"Job creation failed: {resp.text}"
    job = resp.json()

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


@pytest.mark.parametrize(
    ("measure_id", "period"),
    [(MEASURE_ID, DEFAULT_PERIOD), (EXCLUSION_MEASURE_ID, EXCLUSION_PERIOD)],
)
async def test_deqm_job_matches_direct_load_populations(
    integration_client, integration_session_factory, measure_id, period
):
    """The DEQM workflow must produce the same populations as direct load.

    Parameterised over two measures deliberately. CMS122 passed this test while
    CMS130 silently diverged: CMS130's hospice denominator-exclusion is carried by
    a ServiceRequest whose dataRequirement names a VSAC ValueSet the CDR has never
    loaded, so the `code:in=` query failed and the resource was dropped. One
    measure is not a parity suite.

    The CMS130 case only bites inside EXCLUSION_PERIOD -- see the comment there.
    """
    direct = await _create_and_run(integration_client, integration_session_factory, "direct_load", measure_id, period)
    deqm = await _create_and_run(
        integration_client, integration_session_factory, "deqm_submit_data", measure_id, period
    )

    assert direct["status"] == "complete", f"direct job failed: {direct.get('error_message')}"
    assert deqm["status"] == "complete", f"DEQM job failed: {deqm.get('error_message')}"
    # Assert BOTH sides. Only the DEQM side was checked originally, so a
    # direct_load patient failure fell through to the population diff below and
    # reported as an opaque "Population mismatch" instead of naming the job that
    # actually broke.
    assert direct["failed_patients"] == 0, f"direct_load job had failed patients: {direct.get('error_message')}"
    assert deqm["failed_patients"] == 0, "DEQM job had failed patients"

    # Scoping to a Group (see _create_and_run) is a runtime optimisation, and the
    # way an optimisation like this goes wrong is silently: a Group that lost its
    # members would still pass every assertion below, on a panel of nothing. Pin
    # the panel to the Group the seed actually built.
    expected_panel = await _group_member_count(measure_id)
    assert direct["total_patients"] == expected_panel, (
        f"direct_load evaluated {direct['total_patients']} patients, Group has {expected_panel}"
    )
    assert deqm["total_patients"] == expected_panel, (
        f"deqm evaluated {deqm['total_patients']} patients, Group has {expected_panel}"
    )
    # _run_patches pins BATCH_SIZE=25 specifically so submission runs concurrently
    # across multiple batches; a panel that fits in one batch silently retires that
    # coverage rather than failing. Assert the property instead of assuming it.
    assert deqm["total_batches"] > 1, (
        f"panel of {expected_panel} fits in one batch -- concurrent submission is no longer exercised"
    )

    async with integration_session_factory() as session:
        rows_direct = (
            (await session.execute(select(MeasureResult).where(MeasureResult.job_id == direct["id"]))).scalars().all()
        )
        rows_deqm = (
            (await session.execute(select(MeasureResult).where(MeasureResult.job_id == deqm["id"]))).scalars().all()
        )

    # A parity test that passes because both jobs produced zero patients proves nothing.
    assert rows_direct, "direct_load job produced no patient results"
    assert rows_deqm, "deqm_submit_data job produced no patient results"

    pops_direct = {r.patient_id: r.populations for r in rows_direct}
    pops_deqm = {r.patient_id: r.populations for r in rows_deqm}
    assert set(pops_deqm) == set(pops_direct)
    for pid, pops in pops_direct.items():
        assert pops_deqm[pid] == pops, f"Population mismatch for {pid}"

    # The whole point of the CMS130 parametrisation is one patient: the hospice
    # ServiceRequest behind a ValueSet the CDR cannot resolve. Group-scoping keeps
    # them (they are in CMS130's own bundle), but "keeps them" is exactly the kind
    # of claim that should be checked rather than reasoned about.
    if measure_id == EXCLUSION_MEASURE_ID:
        assert EXCLUSION_PATIENT_ID in pops_direct, (
            f"exclusion patient {EXCLUSION_PATIENT_ID} absent from the scoped panel -- "
            "this parametrisation no longer covers the ValueSet under-fetch it was written for"
        )


async def test_deqm_submission_stored_on_mcs(integration_client, integration_session_factory):
    """The base $submit-data path stores the DEQM MeasureReport on the MCS."""
    deqm = await _create_and_run(integration_client, integration_session_factory, "deqm_submit_data")
    assert deqm["status"] == "complete", deqm.get("error_message")

    async with integration_session_factory() as session:
        row = (await session.execute(select(MeasureResult).where(MeasureResult.job_id == deqm["id"]))).scalars().first()
    assert row is not None, "deqm_submit_data job produced no patient results"

    # Client-assigned MeasureReport id is deqm-{job_id}-{patient_id}; read one
    # back directly (direct reads bypass any index lag — CLAUDE.md triage rule).
    async with httpx.AsyncClient(timeout=30.0) as client:
        direct_read = await client.get(f"{TEST_MEASURE_URL}/MeasureReport/deqm-{deqm['id']}-{row.patient_id}")
    assert direct_read.status_code == 200, direct_read.text
    stored = direct_read.json()
    assert stored["type"] == "data-collection"
