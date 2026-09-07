"""Unit tests for groups-feature fhir_client helpers (issue #322)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.fhir_client import (
    GroupEvaluateError,
    evaluate_group_and_resolve_members,
    list_groups_with_expression,
)

pytestmark = pytest.mark.asyncio

CQL_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/characteristicExpression"

_DUMMY_REQUEST = httpx.Request("GET", "http://test")


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    return httpx.Response(status_code, json=json_data, request=_DUMMY_REQUEST)


def _bundle(entries: list[dict]) -> dict:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in entries], "link": []}


def _patch_async_client(response: httpx.Response):
    """Return a context manager that patches httpx.AsyncClient to return `response`."""
    patcher = patch("app.services.fhir_client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_ctx = AsyncMock()
    mock_ctx.get = AsyncMock(return_value=response)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


async def test_list_filters_to_cql_evaluatable_groups():
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "CQL Group",
        "type": "person",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {
                    "language": "text/cql-expression",
                    "expression": "Patient.active",
                },
            }
        ],
    }
    plain_group = {"resourceType": "Group", "id": "g2", "name": "Plain", "type": "person"}
    wrong_lang = {
        "resourceType": "Group",
        "id": "g3",
        "name": "Wrong Lang",
        "type": "person",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {"language": "text/fhirpath", "expression": "Patient.active"},
            }
        ],
    }
    other_extension = {
        "resourceType": "Group",
        "id": "g4",
        "name": "Other Ext",
        "type": "person",
        "extension": [{"url": "http://example.org/other", "valueString": "noop"}],
    }

    response = _make_response(200, _bundle([cql_group, plain_group, wrong_lang, other_extension]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out) == 1
    g = out[0]
    assert g["id"] == "g1"
    assert g["name"] == "CQL Group"
    assert g["type"] == "person"
    assert g["expression_language"] == "text/cql-expression"
    assert g["expression_preview"].startswith("Patient.active")


async def test_list_truncates_long_expressions():
    long_expr = "Patient." + ("x" * 500)
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Long",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {"language": "text/cql-expression", "expression": long_expr},
            }
        ],
    }
    response = _make_response(200, _bundle([cql_group]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out[0]["expression_preview"]) <= 123  # 120 chars + ellipsis "..."
    assert out[0]["expression_preview"].endswith("...")


async def test_list_accepts_text_cql_identifier_language():
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Ident",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {
                    "language": "text/cql-identifier",
                    "expression": "InEligible",
                },
            }
        ],
    }
    response = _make_response(200, _bundle([cql_group]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out) == 1
    assert out[0]["expression_language"] == "text/cql-identifier"


def _patch_async_client_routed(
    post_response: httpx.Response,
    get_routes: dict[str, httpx.Response],
):
    """Patch httpx.AsyncClient so POST returns post_response and GETs route by URL suffix.

    `get_routes` maps URL substrings (e.g. "Patient/p1") to httpx.Response objects.
    Unmatched GETs return 404.
    """
    patcher = patch("app.services.fhir_client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_ctx = AsyncMock()

    async def _post(url, *args, **kwargs):
        return post_response

    async def _get(url, *args, **kwargs):
        for key, resp in get_routes.items():
            if key in url:
                return resp
        return _make_response(404, {"resourceType": "OperationOutcome"})

    mock_ctx.post = AsyncMock(side_effect=_post)
    mock_ctx.get = AsyncMock(side_effect=_get)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


async def test_evaluate_returns_enriched_members():
    eval_resp = {
        "resourceType": "Group",
        "id": "g1",
        "member": [
            {"entity": {"reference": "Patient/p1"}},
            {"entity": {"reference": "Patient/p2"}},
        ],
    }
    p1 = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"family": "Smith", "given": ["John"]}],
        "gender": "male",
        "birthDate": "1980-04-12",
    }
    p2 = {
        "resourceType": "Patient",
        "id": "p2",
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "gender": "female",
        "birthDate": "1992-08-30",
    }

    patcher = _patch_async_client_routed(
        post_response=_make_response(200, eval_resp),
        get_routes={
            "Patient/p1": _make_response(200, p1),
            "Patient/p2": _make_response(200, p2),
        },
    )
    try:
        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})
    finally:
        patcher.stop()

    assert result["group_id"] == "g1"
    assert result["member_count"] == 2
    assert "evaluated_at" in result and result["evaluated_at"].endswith("Z")

    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["p1"]["name"] == "Smith, John"
    assert by_id["p1"]["gender"] == "male"
    assert by_id["p1"]["birth_date"] == "1980-04-12"
    assert "lookup_error" not in by_id["p1"]
    assert by_id["p2"]["name"] == "Doe, Jane"
    assert by_id["p2"]["gender"] == "female"
    assert by_id["p2"]["birth_date"] == "1992-08-30"


async def test_evaluate_partial_failure_records_lookup_error():
    eval_resp = {
        "resourceType": "Group",
        "id": "g1",
        "member": [
            {"entity": {"reference": "Patient/p1"}},
            {"entity": {"reference": "Patient/missing"}},
        ],
    }
    p1 = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"family": "Smith", "given": ["John"]}],
        "gender": "male",
        "birthDate": "1980-04-12",
    }

    patcher = _patch_async_client_routed(
        post_response=_make_response(200, eval_resp),
        get_routes={
            "Patient/p1": _make_response(200, p1),
            "Patient/missing": _make_response(404, {"resourceType": "OperationOutcome"}),
        },
    )
    try:
        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})
    finally:
        patcher.stop()

    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["p1"]["name"] == "Smith, John"
    assert "lookup_error" not in by_id["p1"]
    assert by_id["missing"]["name"] is None
    assert by_id["missing"]["gender"] is None
    assert by_id["missing"]["birth_date"] is None
    assert "404" in by_id["missing"]["lookup_error"]


async def test_evaluate_raises_on_operation_outcome():
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "invalid", "diagnostics": "bad expression"}],
    }
    patcher = _patch_async_client_routed(
        post_response=_make_response(400, outcome),
        get_routes={},
    )
    try:
        with pytest.raises(GroupEvaluateError) as exc_info:
            await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})
    finally:
        patcher.stop()

    assert exc_info.value.status_code == 400
    assert exc_info.value.operation_outcome == outcome


async def test_evaluate_records_lookup_error_on_httpx_failure():
    """If the per-patient GET raises httpx.ConnectError, the member should be
    returned with a populated lookup_error and null demographic fields."""
    eval_resp = {
        "resourceType": "Group",
        "id": "g1",
        "member": [{"entity": {"reference": "Patient/p1"}}],
    }

    patcher = patch("app.services.fhir_client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_ctx = AsyncMock()

    async def _post(url, *args, **kwargs):
        return _make_response(200, eval_resp)

    async def _get(url, *args, **kwargs):
        raise httpx.ConnectError("connection refused", request=_DUMMY_REQUEST)

    mock_ctx.post = AsyncMock(side_effect=_post)
    mock_ctx.get = AsyncMock(side_effect=_get)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    try:
        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})
    finally:
        patcher.stop()

    assert result["member_count"] == 1
    m = result["members"][0]
    assert m["id"] == "p1"
    assert m["name"] is None
    assert m["gender"] is None
    assert m["birth_date"] is None
    assert "ConnectError" in m["lookup_error"]


async def test_evaluate_zero_members_returns_empty_list():
    eval_resp = {"resourceType": "Group", "id": "g1", "member": []}
    patcher = _patch_async_client_routed(
        post_response=_make_response(200, eval_resp),
        get_routes={},
    )
    try:
        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})
    finally:
        patcher.stop()

    assert result["group_id"] == "g1"
    assert result["member_count"] == 0
    assert result["members"] == []


# ---------------------------------------------------------------------------
# list_groups — the unfiltered listing behind the Patients module (issue #404)
# ---------------------------------------------------------------------------


async def test_list_groups_returns_every_group_unfiltered():
    """Unlike list_groups_with_expression, this returns Groups with no CQL expression."""
    from app.services.fhir_client import list_groups

    plain_group = {"resourceType": "Group", "id": "g1", "name": "Plain Cohort", "type": "person"}
    cql_group = {
        "resourceType": "Group",
        "id": "g2",
        "name": "CQL Cohort",
        "type": "person",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {"language": "text/cql-expression", "expression": "Patient.active"},
            }
        ],
    }

    patcher = _patch_async_client(_make_response(200, _bundle([plain_group, cql_group])))
    try:
        out = await list_groups("http://cdr.example", {})
    finally:
        patcher.stop()

    assert [g["id"] for g in out] == ["g1", "g2"]


async def test_list_groups_returns_quantity():
    """`quantity` is surfaced so a cohort sized only that way is not shown as empty."""
    from app.services.fhir_client import list_groups

    group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Characteristic Cohort",
        "type": "person",
        "quantity": 319,
    }

    patcher = _patch_async_client(_make_response(200, _bundle([group])))
    try:
        out = await list_groups("http://cdr.example", {})
    finally:
        patcher.stop()

    assert out[0]["quantity"] == 319


async def test_list_groups_quantity_is_none_when_absent():
    """A Group with members but no `quantity` reports quantity None, not 0."""
    from app.services.fhir_client import list_groups

    group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Enumerated Cohort",
        "type": "person",
        "member": [{"entity": {"reference": "Patient/p1"}}, {"entity": {"reference": "Patient/p2"}}],
    }

    patcher = _patch_async_client(_make_response(200, _bundle([group])))
    try:
        out = await list_groups("http://cdr.example", {})
    finally:
        patcher.stop()

    assert out[0]["member_count"] == 2
    assert out[0]["quantity"] is None
