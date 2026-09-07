"""Unit tests for /api/groups endpoints (issue #322)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _enable_groups(client: AsyncClient) -> None:
    await client.put("/settings/admin", json={"groups_enabled": True})


@pytest.mark.asyncio
async def test_list_groups_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_groups = [
        {
            "id": "g1",
            "name": "Active Adults",
            "type": "person",
            "expression_language": "text/cql-expression",
            "expression_preview": "Patient.active",
        }
    ]
    with patch(
        "app.routes.groups.list_groups",
        new=AsyncMock(return_value=fake_groups),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == {"groups": fake_groups}


@pytest.mark.asyncio
async def test_list_groups_502_when_cdr_unreachable(client: AsyncClient):
    await _enable_groups(client)
    with patch(
        "app.routes.groups.list_groups",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_evaluate_404_when_feature_disabled(client: AsyncClient):
    resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_result = {
        "group_id": "g1",
        "evaluated_at": "2026-05-17T14:32:01Z",
        "member_count": 1,
        "members": [
            {
                "id": "p1",
                "name": "Smith, John",
                "gender": "male",
                "birth_date": "1980-04-12",
            }
        ],
    }
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 200
    assert resp.json() == fake_result


@pytest.mark.asyncio
async def test_evaluate_passes_operation_outcome_through(client: AsyncClient):
    from app.services.fhir_client import GroupEvaluateError

    await _enable_groups(client)
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-supported", "diagnostics": "no $evaluate"}],
    }
    err = GroupEvaluateError("nope", status_code=400, operation_outcome=outcome)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=err),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 502
    assert resp.json()["detail"]["operation_outcome"] == outcome


@pytest.mark.asyncio
async def test_evaluate_timeout_returns_504(client: AsyncClient):
    import httpx

    await _enable_groups(client)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_group_id_must_be_safe(client: AsyncClient):
    await _enable_groups(client)
    # An ID with characters outside [A-Za-z0-9_\-\.] must be rejected by the
    # route's validator before any CDR call. (We can't use ``..%2Fevil`` here
    # because httpx/Starlette decode ``%2F`` before path matching, so the
    # route doesn't even match and we get a 404 from the router.)
    resp = await client.post("/api/groups/bad$id/evaluate")
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("bad_id", [".", "..", "..."])
@pytest.mark.asyncio
async def test_group_id_rejects_dot_only_segments(client, bad_id):
    """Dot-only ``group_id`` values must be rejected as path-traversal vectors.

    Two layers of defense exist:

    1. httpx (our test client AND the server-side outbound client) normalizes
       RFC 3986 dot-segments at URL-construction time. A literal request to
       ``/api/groups/./evaluate`` collapses to ``/api/groups/evaluate``
       (no match → 404); ``/api/groups/../evaluate`` collapses to
       ``/api/evaluate`` (also 404). That short-circuits the vulnerability
       before our regex ever runs.
    2. ``...`` (three or more dots) is *not* a dot-segment under RFC 3986, so
       it survives normalization and reaches the handler — where the regex
       ``^(?!\\.+$)...`` rejects it with 400.

    Either response is safe; assert both are non-2xx and the ``...`` case
    specifically hits the regex (400). This guards against future changes
    that might bypass httpx normalization (e.g., raw ASGI clients).
    """
    await _enable_groups(client)
    resp = await client.post(f"/api/groups/{bad_id}/evaluate")
    # All dot-only IDs must be rejected; 400 (regex) or 404 (URL normalization)
    # are both acceptable safe outcomes.
    assert resp.status_code in (400, 404)
    # The pure-dot regex itself must reject any all-dots input.
    from app.routes.groups import _GROUP_ID_RE

    assert _GROUP_ID_RE.match(bad_id) is None


# ---------------------------------------------------------------------------
# Patients module: the list endpoint is ungated and unfiltered (issue #404)
#
# A connectathon participant pointing Lenny at their own CDR needs to see every
# cohort on it, not only the CQL-evaluatable ones. $evaluate stays gated.
# ---------------------------------------------------------------------------


async def _set_groups_flag(client: AsyncClient, enabled: bool) -> None:
    await client.put("/settings/admin", json={"groups_enabled": enabled})


@pytest.mark.asyncio
async def test_list_groups_200_when_feature_disabled(client: AsyncClient):
    """The Patients list must work with groups_enabled false — it is always on."""
    await _set_groups_flag(client, False)
    with patch("app.routes.groups.list_groups", new=AsyncMock(return_value=[])):
        resp = await client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == {"groups": []}


@pytest.mark.asyncio
async def test_list_groups_200_when_feature_enabled(client: AsyncClient):
    """...and with the flag on, too. The flag no longer gates this endpoint at all."""
    await _set_groups_flag(client, True)
    with patch("app.routes.groups.list_groups", new=AsyncMock(return_value=[])):
        resp = await client.get("/api/groups")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_groups_returns_unfiltered_groups(client: AsyncClient):
    """The endpoint serves list_groups (all Groups), not the CQL-filtered variant."""
    all_groups = [
        {"id": "g1", "name": "Plain Cohort", "type": "person", "member_count": 42, "quantity": None},
        {"id": "g2", "name": "Characteristic Cohort", "type": "person", "member_count": 0, "quantity": 319},
    ]
    with patch("app.routes.groups.list_groups", new=AsyncMock(return_value=all_groups)) as mocked:
        resp = await client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == {"groups": all_groups}
    assert mocked.await_count == 1


@pytest.mark.asyncio
async def test_list_groups_502_when_cdr_unreachable_ungated(client: AsyncClient):
    """The 502 path survives the switch to the unfiltered lister."""
    with patch("app.routes.groups.list_groups", new=AsyncMock(side_effect=Exception("connection refused"))):
        resp = await client.get("/api/groups")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_evaluate_still_404s_when_feature_disabled(client: AsyncClient):
    """$evaluate keeps its groups_enabled gate — parked, unchanged from main."""
    await _set_groups_flag(client, False)
    resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 404
