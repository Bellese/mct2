"""Integration tests: Patient Group dropdown — 7 Groups present after seed.

Verifies that after a full seed run all 7 connectathon measures have a
corresponding FHIR Group resource on the CDR, and that the synthesized Groups
are not polluting the measure engine.

Run against a local stack that has completed its seed cycle:
    docker compose down -v && docker compose up -d
    cd backend && python -m pytest tests/integration/test_groups_dropdown.py -v
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

pytestmark = pytest.mark.integration

_BUNDLE_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"
_MANIFEST = _BUNDLE_DIR / "manifest.json"


def _patient_count_from_bundle(bundle_file: str) -> int:
    path = _BUNDLE_DIR / bundle_file
    with open(path) as f:
        bundle = json.load(f)
    return sum(
        1
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == "Patient" and e.get("resource", {}).get("id")
    )


def _fetch_all_groups(base_url: str) -> dict[str, dict]:
    """Return {group_id: group_resource} for all Groups on the given server."""
    resp = httpx.get(f"{base_url}/Group", params={"_count": "100"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    groups: dict[str, dict] = {}
    for entry in data.get("entry", []):
        resource = entry.get("resource", {})
        gid = resource.get("id")
        if gid:
            groups[gid] = resource
    return groups


def test_all_groups_present_on_cdr(cdr_url: str) -> None:
    """CDR must expose one Group per connectathon measure after seeding."""
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    expected_ids = {m["id"] for m in manifest["measures"]}
    assert len(expected_ids) == 7, f"Manifest should list 7 measures, got {len(expected_ids)}"

    cdr_groups = _fetch_all_groups(cdr_url)
    missing = expected_ids - set(cdr_groups)
    assert not missing, f"Missing Groups on CDR for: {sorted(missing)}. Present: {sorted(cdr_groups)}"


def test_group_member_counts_match_bundle_patient_counts(cdr_url: str) -> None:
    """Each Group's member count must equal the Patient count in its source bundle."""
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    cdr_groups = _fetch_all_groups(cdr_url)

    mismatches = []
    for m in manifest["measures"]:
        measure_id = m["id"]
        expected = _patient_count_from_bundle(m["bundle_file"])
        group = cdr_groups.get(measure_id, {})
        actual = len(group.get("member", []))
        if expected != actual:
            mismatches.append(f"{measure_id}: bundle has {expected} Patients, Group has {actual} members")

    assert not mismatches, "Group member count mismatches:\n" + "\n".join(mismatches)


def test_synthesized_groups_not_on_measure_engine(measure_url: str) -> None:
    """Synthesized Groups should not appear on the measure engine.

    The measure engine doesn't need Groups; polluting it would be unexpected noise.
    All 7 surviving bundles use the synthesized-Group code path — none ship their own
    Group resource — so all 7 Group IDs must be absent from the measure engine.
    """
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    synthesized_ids = {m["id"] for m in manifest["measures"]}
    measure_groups = _fetch_all_groups(measure_url)
    leaked = synthesized_ids & set(measure_groups)
    assert not leaked, f"Synthesized Groups unexpectedly present on measure engine: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# The Patients module reads these same Groups through GET /api/groups (#404).
#
# NOTE: this file is in CI's --ignore list, so CI will silently skip everything
# below. It must be run locally (pre-push checklist step 5):
#     ./scripts/run-integration-tests.sh tests/integration/test_groups_dropdown.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_groups_returns_every_seeded_group_unfiltered(integration_client, cdr_url: str) -> None:
    """GET /api/groups must expose all 7 seeded Groups, not only CQL-evaluatable ones.

    The seeded connectathon Groups are synthesized and carry no CQL
    valueExpression, so under the pre-#404 CQL-filtered lister this endpoint
    returned an empty list — the exact opposite of what a participant
    surveying a CDR needs.
    """
    with open(_MANIFEST) as f:
        manifest = json.load(f)
    expected_ids = {m["id"] for m in manifest["measures"]}

    resp = await integration_client.get("/api/groups")
    assert resp.status_code == 200, resp.text

    returned = {g["id"] for g in resp.json()["groups"]}
    missing = expected_ids - returned
    assert not missing, f"GET /api/groups omitted seeded Groups: {sorted(missing)}"

    # And the payload carries what the Patients rows render.
    sample = next(g for g in resp.json()["groups"] if g["id"] in expected_ids)
    assert set(sample) >= {"id", "name", "type", "member_count", "quantity"}


@pytest.mark.asyncio
async def test_api_groups_follows_the_active_cdr(
    integration_client, db_session, truncate_tables, measure_url: str
) -> None:
    """Activating a different CDR changes what GET /api/groups returns.

    Criterion (b): run a measure against the Groups on the CDR you are actually
    pointed at. The measure engine deliberately holds none of the synthesized
    Groups (asserted above), so activating it as the CDR must yield a list that
    no longer contains them — proving the endpoint follows the active row
    rather than a process-wide default.

    `truncate_tables` is required, not decorative: this test writes an active
    cdr_configs row, and db_session does not roll back despite its docstring.
    Without the truncate, every later test in the run would silently be pointed
    at the measure engine as its CDR.
    """
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    before = {g["id"] for g in (await integration_client.get("/api/groups")).json()["groups"]}
    assert before, "precondition: the seeded CDR should expose Groups"

    # Point the active CDR at the measure engine, which has no synthesized Groups.
    await db_session.execute(sa_update(CDRConfig).values(is_active=False))
    db_session.add(
        CDRConfig(
            name="Groupless CDR",
            cdr_url=measure_url,
            auth_type=AuthType.none,
            is_active=True,
            is_default=False,
            is_read_only=False,
        )
    )
    await db_session.commit()

    after = {g["id"] for g in (await integration_client.get("/api/groups")).json()["groups"]}
    assert not (before & after), f"GET /api/groups still returned the previous CDR's Groups: {sorted(before & after)}"
