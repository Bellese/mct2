"""Tests for the FHIR client service (fhir_client.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.fhir_client import (
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    BatchQueryStrategy,
    DataRequirementsStrategy,
    _acquire_smart_token,
    _build_auth_headers,
    _chunk_request_entries,
    _remap_valueset_ids_for_hapi,
    delete_measure,
    detect_submit_data_mode,
    evaluate_measure,
    get_measure_canonical,
    list_measures,
    measure_exists,
    push_resources,
    resolve_evaluated_resource,
    submit_data,
    upload_measure_bundle,
    wait_for_valueset_expansion,
    wipe_measure_definitions,
    wipe_patient_data,
    wipe_patients_by_id,
)
from app.services.fhir_client import (
    verify_fhir_connection as fhir_test_connection,
)
from app.services.fhir_errors import FhirOperationError

pytestmark = pytest.mark.asyncio

# Dummy request used to construct httpx.Response objects that support raise_for_status()
_DUMMY_REQUEST = httpx.Request("GET", "http://test")


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    """Build an httpx.Response with a request set so raise_for_status() works."""
    return httpx.Response(status_code, json=json_data, request=_DUMMY_REQUEST)


# ---------------------------------------------------------------------------
# _build_auth_headers
# ---------------------------------------------------------------------------


class TestBuildAuthHeaders:
    async def test_no_auth(self):
        result = await _build_auth_headers("none", None)
        assert result == {}

    async def test_no_auth_with_credentials(self):
        """Even with credentials, 'none' auth type returns empty."""
        result = await _build_auth_headers("none", {"username": "u", "password": "p"})
        assert result == {}

    async def test_basic_auth(self):
        result = await _build_auth_headers("basic", {"username": "admin", "password": "secret"})
        assert "Authorization" in result
        assert result["Authorization"].startswith("Basic ")
        import base64

        decoded = base64.b64decode(result["Authorization"].split(" ")[1]).decode()
        assert decoded == "admin:secret"

    async def test_bearer_auth(self):
        result = await _build_auth_headers("bearer", {"token": "my-jwt"})
        assert result == {"Authorization": "Bearer my-jwt"}

    async def test_unknown_auth_type(self):
        result = await _build_auth_headers("oauth2", {"token": "abc"})
        assert result == {}

    async def test_basic_auth_no_credentials(self):
        result = await _build_auth_headers("basic", None)
        assert result == {}

    async def test_smart_auth(self):
        """_build_auth_headers with SMART type calls _acquire_smart_token internally."""
        credentials = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://auth.example.com/token",
        }
        with patch(
            "app.services.fhir_client._acquire_smart_token",
            new=AsyncMock(return_value="smart-token-abc"),
        ):
            result = await _build_auth_headers("smart", credentials)
        assert result == {"Authorization": "Bearer smart-token-abc"}


# ---------------------------------------------------------------------------
# BatchQueryStrategy.gather_patients
# ---------------------------------------------------------------------------


async def test_gather_patients_single_page(mock_patient_bundle):
    """gather_patients returns patient resources from a single page."""
    mock_response = _make_response(200, mock_patient_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 2
    assert patients[0]["id"] == "patient-1"
    assert patients[1]["id"] == "patient-2"


async def test_gather_patients_paginated():
    """gather_patients follows pagination links."""
    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1"}},
        ],
        "link": [
            {"relation": "next", "url": "http://cdr/fhir/Patient?_count=100&page=2"},
        ],
    }
    page2 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p2"}},
        ],
        "link": [],
    }

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(200, page1)
        return _make_response(200, page2)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 2
    assert patients[0]["id"] == "p1"
    assert patients[1]["id"] == "p2"
    assert call_count == 2


async def test_gather_patients_empty():
    """gather_patients returns empty list when no patients found."""
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}
    mock_response = _make_response(200, empty_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert patients == []


# ---------------------------------------------------------------------------
# BatchQueryStrategy.gather_patient_data
# ---------------------------------------------------------------------------


async def test_gather_patient_data():
    """gather_patient_data returns resources from $everything."""
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1"}},
            {"resource": {"resourceType": "Condition", "id": "c1"}},
            {"resource": {"resourceType": "Observation", "id": "o1"}},
        ],
        "link": [],
    }
    mock_response = _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert len(resources) == 3
    assert resources[0]["resourceType"] == "Patient"
    assert resources[1]["resourceType"] == "Condition"


# ---------------------------------------------------------------------------
# push_resources
# ---------------------------------------------------------------------------


async def test_push_resources():
    """push_resources sends a batch bundle to the measure engine."""
    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
    ]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    # Verify post was called
    mock_ctx.post.assert_called_once()
    call_args = mock_ctx.post.call_args
    posted_bundle = call_args.kwargs.get("json") or call_args[1].get("json")
    assert posted_bundle["resourceType"] == "Bundle"
    assert posted_bundle["type"] == "batch"
    assert len(posted_bundle["entry"]) == 2


async def test_push_resources_empty():
    """push_resources with no valid resources does nothing."""
    resources = [{"no_resourceType": True}]  # Invalid -- missing resourceType and id

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    # post should NOT have been called
    mock_ctx.post.assert_not_called()


async def test_push_resources_sorts_patients_first():
    """push_resources MUST place Patient entries before any resource that
    references them. HAPI's bundle import skips writing reference index
    entries for forward-references, so an Encounter appearing in the bundle
    before its referenced Patient persists the Encounter but never indexes
    `Encounter.subject → Patient/{id}`. `Encounter?patient=` then returns 0
    forever. Verified empirically 2026-04-25 (issue #177): same bundle,
    original order = 20/33 indexed; Patients-first = 33/33 at t=0.

    This test pins the sort behavior so we don't regress.
    """
    # Caller passes resources in a "bad" order: Encounter before Patient.
    resources = [
        {"resourceType": "Encounter", "id": "e1", "subject": {"reference": "Patient/p1"}},
        {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}},
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Encounter", "id": "e2", "subject": {"reference": "Patient/p2"}},
        {"resourceType": "Patient", "id": "p2"},
    ]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    posted_bundle = mock_ctx.post.call_args.kwargs.get("json") or mock_ctx.post.call_args[1].get("json")
    types_in_order = [e["resource"]["resourceType"] for e in posted_bundle["entry"]]

    # All Patients come first.
    first_non_patient_idx = next(i for i, t in enumerate(types_in_order) if t != "Patient")
    assert all(t == "Patient" for t in types_in_order[:first_non_patient_idx]), (
        f"Patients must lead; got order: {types_in_order}"
    )
    assert "Patient" not in types_in_order[first_non_patient_idx:], (
        f"No Patient may appear after a non-Patient; got order: {types_in_order}"
    )
    # Stable order is preserved within each group.
    non_patient_types = [t for t in types_in_order if t != "Patient"]
    assert non_patient_types == ["Encounter", "Condition", "Encounter"], (
        f"Non-Patient relative order should be preserved; got: {non_patient_types}"
    )


async def test_push_resources_with_auth_headers():
    """push_resources forwards auth_headers alongside Content-Type."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(
            resources,
            target_url="http://test-measure/",
            auth_headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )

    mock_ctx.post.assert_called_once()
    call_args = mock_ctx.post.call_args
    sent_headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
    assert sent_headers.get("Authorization") == "Basic dXNlcjpwYXNz"
    assert sent_headers.get("Content-Type", "").startswith("application/fhir+json")


# ---------------------------------------------------------------------------
# evaluate_measure
# ---------------------------------------------------------------------------


async def test_evaluate_measure(mock_measure_report):
    """evaluate_measure calls $evaluate-measure and returns the MeasureReport."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    assert result["resourceType"] == "MeasureReport"
    mock_ctx.get.assert_called_once()
    url = mock_ctx.get.call_args[0][0]
    assert "Measure/measure-1/$evaluate-measure" in url
    assert "periodStart=2024-01-01" in url
    assert "subject=Patient/patient-1" in url


async def test_evaluate_measure_retries_transient_5xx(mock_measure_report):
    """Transient HAPI 5xx responses are retried before returning the MeasureReport."""
    responses = [
        _make_response(500, {"resourceType": "OperationOutcome"}),
        _make_response(502, {"resourceType": "OperationOutcome"}),
        _make_response(200, mock_measure_report),
    ]

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=responses)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.fhir_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    assert result["resourceType"] == "MeasureReport"
    assert mock_ctx.get.call_count == 3
    assert mock_sleep.await_count == 2


async def test_evaluate_measure_does_not_retry_4xx():
    """Known HAPI/MADiE 4xx failures surface as FhirOperationError without retrying."""
    response = _make_response(400, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    mock_ctx.get.assert_awaited_once()
    assert exc_info.value.status_code == 400
    assert exc_info.value.operation == "evaluate-measure"


async def test_evaluate_measure_raises_fhir_error_with_outcome_on_4xx():
    """evaluate_measure preserves the MCS OperationOutcome in FhirOperationError on 4xx."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Measure not found"}],
    }
    response = _make_response(404, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    err = exc_info.value
    assert err.status_code == 404
    assert err.outcome is not None
    assert err.outcome.issues[0].diagnostics == "Measure not found"


async def test_evaluate_measure_raises_on_200_with_operation_outcome():
    """evaluate_measure raises FhirOperationError when MCS returns 200 OK with OperationOutcome."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": "CQL evaluation error"}],
    }
    response = _make_response(200, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    err = exc_info.value
    assert err.status_code == 200
    assert err.outcome is not None
    assert "CQL evaluation error" in err.outcome.primary_diagnostic()


async def test_evaluate_measure_raises_on_200_with_error_status_measure_report():
    """evaluate_measure raises FhirOperationError when HAPI returns 200 OK with
    MeasureReport.status == 'error' (e.g. Unknown ValueSet).  Populations are all
    zero in that response but the error must not be silently swallowed."""
    contained_oo = {
        "resourceType": "OperationOutcome",
        "id": "oo-1",
        "issue": [
            {
                "severity": "error",
                "code": "exception",
                "diagnostics": (
                    "Exception for subjectId: Patient/p1, "
                    "Message: HAPI-2788: Unknown ValueSet: "
                    "http%3A%2F%2Fcts.nlm.nih.gov%2Ffhir%2FValueSet%2F2.16.840.1.113762.1.4.1248.208"
                ),
            }
        ],
    }
    measure_report = {
        "resourceType": "MeasureReport",
        "status": "error",
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "contained": [contained_oo],
        "group": [
            {
                "population": [
                    {"code": {"coding": [{"code": "initial-population"}]}, "count": 0},
                ]
            }
        ],
    }
    response = _make_response(200, measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("CMS122FHIRDiabetesAssessGreaterThan9Percent", "p1", "2026-01-01", "2026-12-31")

    err = exc_info.value
    assert err.status_code == 200
    assert err.operation == "evaluate-measure"
    assert err.outcome is not None
    assert "HAPI-2788" in err.outcome.primary_diagnostic()
    assert "2.16.840.1.113762.1.4.1248.208" in err.outcome.primary_diagnostic()


async def test_push_resources_raises_fhir_error_on_http_failure():
    """push_resources raises FhirOperationError on non-2xx response."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    response = _make_response(500, {"resourceType": "OperationOutcome", "issue": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await push_resources(resources)

    assert exc_info.value.status_code == 500
    assert exc_info.value.operation == "push-resources"


async def test_push_resources_raises_on_200_with_operation_outcome():
    """push_resources raises FhirOperationError on 200 OK with OperationOutcome body (entire-batch rejection)."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "invalid"}]}
    response = _make_response(200, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await push_resources(resources)

    assert exc_info.value.status_code == 200
    assert exc_info.value.operation == "push-resources"


async def test_push_resources_returns_bundle_result_with_failed_entries():
    """push_resources returns BundleUploadResult capturing per-entry failures."""
    from app.services.fhir_client import BundleUploadResult

    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
    ]
    # HAPI batch response: one 201, one 422
    batch_response = {
        "resourceType": "Bundle",
        "type": "batch-response",
        "entry": [
            {"response": {"status": "201 Created"}},
            {"response": {"status": "422 Unprocessable Entity"}},
        ],
    }
    response = _make_response(200, batch_response)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await push_resources(resources)

    assert isinstance(result, BundleUploadResult)
    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    assert result.succeeded[0].resource_type == "Patient"
    assert result.failed[0].resource_type == "Condition"
    assert result.has_failures is True


async def test_push_resources_max_bundle_entries_none_posts_once():
    """max_bundle_entries=None preserves the existing single-POST behavior."""
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(5)]
    mock_response = _make_response(200, {"resourceType": "Bundle", "entry": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources, max_bundle_entries=None)

    assert mock_ctx.post.call_count == 1


async def test_push_resources_chunks_when_max_bundle_entries_set():
    """5 resources with max_bundle_entries=2 should POST 3 bundles
    (sizes 2, 2, 1) and aggregate per-entry results across them.
    """
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(5)]

    def make_resp_for(req_bundle_json):
        n = len(req_bundle_json["entry"])
        body = {
            "resourceType": "Bundle",
            "type": "batch-response",
            "entry": [{"response": {"status": "201 Created"}} for _ in range(n)],
        }
        return _make_response(200, body)

    posted_bundles = []

    async def fake_post(url, *, json, headers):
        posted_bundles.append(json)
        return make_resp_for(json)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(side_effect=fake_post)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await push_resources(resources, max_bundle_entries=2)

    assert mock_ctx.post.call_count == 3
    assert [len(b["entry"]) for b in posted_bundles] == [2, 2, 1]
    assert len(result.succeeded) == 5
    assert len(result.failed) == 0


async def test_push_resources_partial_chunk_failure_does_not_raise():
    """If some chunks succeed and one chunk's HTTP request errors with 400,
    push_resources must NOT raise — it returns an aggregated BundleUploadResult
    with the failed chunk's entries marked as failures and earlier successes
    intact. Mirrors the existing 200-with-per-entry-failures semantics.
    """
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(4)]

    call_idx = {"n": 0}

    async def fake_post(url, *, json, headers):
        call_idx["n"] += 1
        if call_idx["n"] == 2:
            body = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "details": {"text": "Too many entries in bundle. Max supported number of entries is 1"},
                    }
                ],
            }
            return _make_response(400, body)
        n = len(json["entry"])
        body = {
            "resourceType": "Bundle",
            "type": "batch-response",
            "entry": [{"response": {"status": "201 Created"}} for _ in range(n)],
        }
        return _make_response(200, body)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(side_effect=fake_post)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await push_resources(resources, max_bundle_entries=2)

    assert mock_ctx.post.call_count == 2
    assert len(result.succeeded) == 2
    assert len(result.failed) == 2
    for fe in result.failed:
        assert fe.outcome is not None


async def test_push_resources_all_chunks_failed_raises():
    """If every chunk's POST fails atomically, surface the first chunk's
    outcome via FhirOperationError so validation.py marks the upload as
    `failed`. Preserves the existing single-shot raise-on-400 contract.
    """
    from app.services.fhir_errors import FhirOperationError

    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(3)]
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "exception",
                "details": {"text": "Too many entries in bundle. Max supported number of entries is 1"},
            }
        ],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=_make_response(400, body))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as excinfo:
            await push_resources(resources, max_bundle_entries=2)

    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# _chunk_request_entries — partition helper
# ---------------------------------------------------------------------------


def _entry(resource_type, resource_id):
    return {
        "resource": {"resourceType": resource_type, "id": resource_id},
        "request": {"method": "PUT", "url": f"{resource_type}/{resource_id}"},
    }


def test_chunk_request_entries_none_returns_single_chunk():
    entries = [_entry("Patient", "p1"), _entry("Encounter", "e1")]
    chunks = _chunk_request_entries(entries, max_size=None)
    assert chunks == [entries]


def test_chunk_request_entries_zero_max_returns_single_chunk():
    # Defensive: treat 0 or negative as "no chunking" (the API layer already
    # rejects these at request time, but the service layer should be safe).
    entries = [_entry("Patient", "p1")]
    assert _chunk_request_entries(entries, max_size=0) == [entries]
    assert _chunk_request_entries(entries, max_size=-1) == [entries]


def test_chunk_request_entries_partitions_evenly():
    entries = [_entry("Patient", f"p{i}") for i in range(6)]
    chunks = _chunk_request_entries(entries, max_size=2)
    assert len(chunks) == 3
    assert all(len(c) == 2 for c in chunks)
    # Order within and across chunks must be preserved.
    assert [e["resource"]["id"] for c in chunks for e in c] == [f"p{i}" for i in range(6)]


def test_chunk_request_entries_handles_remainder():
    entries = [_entry("Patient", f"p{i}") for i in range(5)]
    chunks = _chunk_request_entries(entries, max_size=2)
    assert [len(c) for c in chunks] == [2, 2, 1]


def test_chunk_request_entries_preserves_patients_first_across_chunks():
    """Patients must precede non-Patients in the chunk sequence, so HAPI's
    reference index sees the Patient before any Encounter that references it.
    This protects the invariant from issue #177 across chunk boundaries.
    """
    entries = [
        _entry("Patient", "p1"),
        _entry("Patient", "p2"),
        _entry("Patient", "p3"),
        _entry("Encounter", "e1"),
        _entry("Encounter", "e2"),
        _entry("Condition", "c1"),
    ]
    chunks = _chunk_request_entries(entries, max_size=2)
    flat_types = [e["resource"]["resourceType"] for c in chunks for e in c]
    # Find first non-Patient and assert no Patient appears after it.
    first_non_patient = next(i for i, t in enumerate(flat_types) if t != "Patient")
    assert "Patient" not in flat_types[first_non_patient:], (
        f"Patient appeared after a non-Patient across chunks: {flat_types}"
    )


async def test_gather_result_partial_failure_surfaced():
    """DataRequirementsStrategy returns GatherResult with failed_types on partial CDR failure."""
    from app.services.fhir_client import GatherResult

    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation"}, {"type": "Condition"}],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Observation" in url:
            return _make_response(200, obs_bundle)
        if "Condition" in url:
            raise httpx.ConnectError("CDR unreachable for Condition")
        return _make_response(200, {"resourceType": "Bundle", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert isinstance(gather_result, GatherResult)
    assert gather_result.has_partial_failure
    failed_type_names = [f.resource_type for f in gather_result.failed_types]
    assert "Condition" in failed_type_names
    assert any(r.get("resourceType") == "Observation" for r in gather_result.resources)
    # Should NOT have fallen back to $everything
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0


# ---------------------------------------------------------------------------
# wipe_patient_data
# ---------------------------------------------------------------------------


async def test_wipe_patient_data():
    """wipe_patient_data sends DELETE requests for all clinical resource types."""
    mock_response = _make_response(200, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    # Should have made delete calls for each resource type
    assert mock_ctx.delete.call_count >= 10  # At least 10 resource types


async def test_wipe_patient_data_raises_on_unauthorized_instead_of_silent_noop():
    """A 401 on the conditional delete must abort, not degrade to a silent no-op.

    Regression guard. `httpx` does not raise on status codes and the wipe never
    called `raise_for_status()`, so a 401 fell through to the search-and-delete
    fallback — which was unauthenticated, 401'd on its own GET, `break`ed, and
    let wipe_patient_data return success having deleted nothing. The prior job's
    resources then inflated the next job's populations with no error signal.
    """
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(401, {}))
        mock_ctx.get = AsyncMock(return_value=_make_response(401, {}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Not authorized to wipe"):
            await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={"Authorization": "Bearer x"})


async def test_wipe_patient_data_fallback_carries_credentials():
    """When conditional delete is unsupported, the fallback sweep stays authenticated."""
    headers = {"Authorization": "Bearer tok-1"}
    empty_bundle = _make_response(200, {"resourceType": "Bundle", "entry": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        # 405 = conditional delete unsupported, the legitimate fallback trigger.
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=empty_bundle)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers=headers)

    assert mock_ctx.get.await_count > 0, "fallback sweep never ran"
    for call in mock_ctx.get.await_args_list:
        assert call.kwargs.get("headers") == headers, "fallback GET went out unauthenticated"


async def test_wipe_patient_data_fallback_rejects_cross_origin_next_link():
    """A hostile next link must not steer the fallback sweep at another host."""
    hostile = _make_response(
        200,
        {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
            "link": [{"relation": "next", "url": "http://169.254.169.254/latest/meta-data/"}],
        },
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=hostile)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={})

    for call in mock_ctx.get.await_args_list:
        requested = call.args[0] if call.args else ""
        assert "169.254.169.254" not in requested, "followed a cross-origin next link"


async def test_wipe_patient_data_fallback_rejects_traversal_ids():
    """Server-supplied ids containing path separators must not become DELETE paths."""
    malicious = _make_response(
        200,
        {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "../../Measure/CMS130"}},
                {"resource": {"resourceType": "Patient", "id": "safe-1"}},
            ],
        },
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=malicious)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={})

    for call in mock_ctx.delete.await_args_list:
        requested = call.args[0] if call.args else ""
        assert "Measure/CMS130" not in requested, "traversal id became a DELETE path"


async def test_wipe_patient_data_includes_qi_core_types():
    """wipe_patient_data includes QI-Core clinical types added for STU6 bundles."""
    mock_response = _make_response(200, {})
    deleted_urls: list[str] = []

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()

        async def capture_delete(url, **kwargs):
            deleted_urls.append(url)
            return mock_response

        mock_ctx.delete = AsyncMock(side_effect=capture_delete)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    wiped_types = {url.split("/")[-1].split("?")[0] for url in deleted_urls}
    for expected_type in (
        "DeviceRequest",
        "Medication",
        "Task",
        "MedicationAdministration",
        "AdverseEvent",
        "Location",
        "Practitioner",
        "Organization",
    ):
        assert expected_type in wiped_types, f"{expected_type} missing from wipe list"


async def test_wipe_patient_data_patient_deleted_after_clinical_resources():
    """Patient must be deleted AFTER clinical types to avoid HAPI 409 referential-integrity errors.

    HAPI returns 409 when a DELETE targets Patient while Condition/Encounter/etc.
    still reference it.  Regression test for issue #235.
    """
    mock_response = _make_response(200, {})
    delete_order: list[str] = []

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()

        async def capture_delete(url, **kwargs):
            rt = url.split("/")[-1].split("?")[0]
            delete_order.append(rt)
            return mock_response

        mock_ctx.delete = AsyncMock(side_effect=capture_delete)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    assert "Patient" in delete_order, "Patient must be in the wipe list"
    patient_idx = delete_order.index("Patient")
    clinical_types = {
        "Condition",
        "Observation",
        "Encounter",
        "Procedure",
        "MedicationRequest",
        "MedicationAdministration",
    }
    for rt in clinical_types:
        assert rt in delete_order, f"{rt} missing from wipe list"
        assert delete_order.index(rt) < patient_idx, (
            f"{rt} must be deleted before Patient (got {rt} at {delete_order.index(rt)}, Patient at {patient_idx})"
        )


async def test_wipe_patient_data_strict_raises_after_consecutive_failures():
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="FHIR server unreachable"):
            await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    assert mock_ctx.delete.call_count == 3


async def test_wipe_patient_data_non_strict_raises_after_consecutive_failures():
    """non-strict mode now raises after 3 failures (silent return caused the race condition)."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="FHIR server unreachable"):
            await wipe_patient_data(base_url="http://test-fhir:8080/fhir", strict=False)

    assert mock_ctx.delete.call_count == 3


# ---------------------------------------------------------------------------
# wipe_patients_by_id (issue #392)
# ---------------------------------------------------------------------------


def _delete_urls(mock_ctx) -> list[str]:
    """Every URL passed positionally to client.delete()."""
    return [call.args[0] for call in mock_ctx.delete.await_args_list]


class TestWipePatientsById:
    """The patient-scoped wipe that makes running against a shared MCS safe.

    Issue #392: the full wipe deletes every patient on the target server. Since
    evaluation is per-subject (`$evaluate-measure?subject=Patient/<id>`), deleting
    only the IDs this job is about to push is equivalent for correctness while
    leaving other participants' data alone.
    """

    def _mock_client(self, mock_httpx, response=None):
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=response or _make_response(200, {}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    async def test_every_delete_is_scoped_to_the_given_patients(self):
        """No DELETE may go out without a scoping filter.

        This is the whole point of the issue: an unfiltered conditional delete is
        what wipes a shared server. The three legitimate scoping params are
        `patient=`, `subject=` (AdverseEvent) and `_id=` (Patient itself).
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2"])

        urls = _delete_urls(mock_ctx)
        assert urls, "no deletes issued"
        for url in urls:
            assert any(f"?{p}=" in url for p in ("patient", "subject", "_id")), (
                f"unscoped delete would wipe the server: {url}"
            )
            assert "_lastUpdated" not in url, f"full-wipe filter leaked into the scoped wipe: {url}"

    async def test_deletes_the_patient_resource_itself(self):
        """The Patient resource must go too, scoped by _id.

        Regression guard: the first implementation swept 19 clinical types and left
        the Patient behind. HAPI 400s on `Patient?patient=`, so Patient needs `_id`
        — and it has to come last, because HAPI 409s while clinical resources still
        reference it. Caught by the integration test, not by a mock.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2"])

        urls = _delete_urls(mock_ctx)
        patient_urls = [u for u in urls if "/Patient?" in u]
        assert patient_urls, "the Patient resource was never deleted"
        assert "_id=p1,p2" in patient_urls[0]
        assert urls[-1] in patient_urls, "Patient must be deleted last or HAPI returns 409"

    async def test_covers_the_patient_linked_clinical_types(self):
        """The types that carry measure-relevant data must all be swept."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = _delete_urls(mock_ctx)
        for rt in ("MeasureReport", "Condition", "Observation", "Encounter", "Procedure", "MedicationRequest"):
            assert any(f"/{rt}?" in u for u in urls), f"{rt} not wiped"

    async def test_skips_types_with_no_patient_link(self):
        """Medication/Location/Practitioner/Organization have no patient search param.

        Verified against HAPI: both `patient=` and `subject=` return HTTP 400 for
        these. They are also shared infrastructure on a multi-tenant server, and
        re-push restores them by ID, so skipping them is correct as well as required.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = _delete_urls(mock_ctx)
        for rt in ("Medication", "Location", "Practitioner", "Organization"):
            assert not any(f"/{rt}?" in u for u in urls), f"{rt} has no patient link — delete would be unscoped"

    async def test_adverse_event_uses_subject_not_patient(self):
        """AdverseEvent is the one type where `patient=` is a 400 on HAPI."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        adverse = [u for u in _delete_urls(mock_ctx) if "/AdverseEvent?" in u]
        assert len(adverse) == 1
        assert "subject=" in adverse[0]
        assert "patient=" not in adverse[0]

    async def test_batches_ids_into_one_request_per_type(self):
        """IDs are OR'd in a single param so the sweep is 19 requests, not 19*N."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2", "p3"])

        condition = [u for u in _delete_urls(mock_ctx) if "/Condition?" in u]
        assert len(condition) == 1, "one request per type expected for a small ID list"
        assert "p1,p2,p3" in condition[0]

    async def test_chunks_large_id_lists(self):
        """A 460-patient job must not produce a multi-kilobyte URL."""
        ids = [f"p{i}" for i in range(250)]
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=ids)

        condition = [u for u in _delete_urls(mock_ctx) if "/Condition?" in u]
        assert len(condition) > 1, "large ID list was not chunked"
        for url in condition:
            assert len(url) < 2000, f"URL too long for a GET/DELETE line: {len(url)}"
        # Every ID must appear somewhere — chunking must not silently drop any.
        joined = " ".join(condition)
        for pid in ids:
            assert f"{pid}," in joined or f"{pid} " in joined or joined.endswith(pid) or f"{pid}&" in joined, (
                f"{pid} dropped by chunking"
            )

    async def test_no_patient_ids_issues_no_deletes(self):
        """An empty gather must not fall through to an unscoped sweep."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=[])

        assert mock_ctx.delete.await_count == 0

    async def test_carries_auth_headers(self):
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=headers)

        for call in mock_ctx.delete.await_args_list:
            assert call.kwargs.get("headers") == headers, "scoped delete went out unauthenticated"

    async def test_raises_on_unauthorized(self):
        """Same fail-loud rule as the full wipe: a 401 must not report success.

        Silently failing here leaves the prior run's resources attached to the
        patients this job is about to evaluate, which corrupts its populations.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._mock_client(mock_httpx, response=_make_response(401, {}))
            with pytest.raises(RuntimeError, match="Not authorized"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_raises_after_consecutive_transport_failures(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="FHIR server unreachable"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.delete.await_count == 3

    async def test_tolerates_404_on_absent_types(self):
        """A server that doesn't stock a type must not fail the whole wipe."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx, response=_make_response(404, {}))
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.delete.await_count > 0

    async def test_falls_back_to_one_by_one_when_multiple_delete_is_disabled(self):
        """A server with allow_multiple_delete=false must still get wiped.

        This is the deployment the whole feature targets: our own containers set
        `hapi.fhir.allow_multiple_delete=true`, but a shared remote MCS may not.
        HAPI then rejects a conditional delete whose search matches more than one
        resource. Without a fallback the wipe logs a warning, deletes nothing, and
        the prior run's resources stay attached to the patients this job is about
        to evaluate — silently corrupting its populations.
        """
        # 412 = HAPI's "search matched multiple resources and multiple delete is
        # disabled" response.
        rejected = _make_response(412, {})
        found = _make_response(
            200,
            {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Condition", "id": "cond-1"}}],
            },
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()

            # Conditional delete (has a "?") is rejected; delete by id succeeds.
            async def _delete(url, **kwargs):
                return rejected if "?" in url else _make_response(200, {})

            mock_ctx.delete = AsyncMock(side_effect=_delete)
            mock_ctx.get = AsyncMock(return_value=found)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        # The fallback searched...
        assert mock_ctx.get.await_count > 0, "no fallback search was issued"
        # ...and every search it issued stayed scoped to the patient.
        for call in mock_ctx.get.await_args_list:
            url = call.args[0]
            assert any(f"&{p}=" in url for p in ("patient", "subject", "_id")), f"fallback search was unscoped: {url}"
        # ...and deleted by id.
        by_id = [u for u in _delete_urls(mock_ctx) if "?" not in u]
        assert any("/Condition/cond-1" in u for u in by_id), "fallback never deleted the matched resource"

    async def test_fallback_search_stays_authenticated(self):
        """The fallback must carry credentials or it 401s and silently no-ops."""
        headers = {"Authorization": "Bearer tok-1"}
        found = _make_response(200, {"resourceType": "Bundle", "entry": []})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.delete = AsyncMock(return_value=_make_response(412, {}))
            mock_ctx.get = AsyncMock(return_value=found)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=headers)

        assert mock_ctx.get.await_count > 0
        for call in mock_ctx.get.await_args_list:
            assert call.kwargs.get("headers") == headers, "fallback search went out unauthenticated"


# ---------------------------------------------------------------------------
# wipe_measure_definitions (issue #397)
# ---------------------------------------------------------------------------


class TestWipeMeasureDefinitions:
    """The admin 'wipe measure engine' primitive, scoped to a caller-supplied server.

    Issue #397: this read `settings.MEASURE_ENGINE_URL` directly, so an admin
    connected to a remote MCS who clicked the control wiped Lenny's LOCAL engine
    and got a success response, while the server they were looking at was
    untouched. The inverse is the real hazard — a naive fix makes it capable of
    wiping a shared remote server — so `base_url` is required and the callers own
    the read-only guard.
    """

    def _mock_client(self, mock_httpx, response=None):
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=response or _make_response(200, {}))
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "entry": []}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    async def test_base_url_is_required(self):
        """No env-var default. A default is how this bug class stayed invisible."""
        with pytest.raises(TypeError):
            await wipe_measure_definitions()  # type: ignore[call-arg]

    async def test_targets_the_given_server(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

        urls = [call.args[0] for call in mock_ctx.delete.await_args_list]
        assert urls, "no deletes issued"
        for url in urls:
            assert url.startswith("https://mcs.example.org/fhir/"), f"wrong server: {url}"

    async def test_covers_the_definition_types_and_not_clinical_data(self):
        """Definitions only — this control must not delete patients."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

        urls = [call.args[0] for call in mock_ctx.delete.await_args_list]
        for rt in ("Library", "Measure", "ValueSet", "CodeSystem", "ConceptMap"):
            assert any(f"/{rt}?" in u for u in urls), f"{rt} not wiped"
        for rt in ("Patient", "Encounter", "Condition", "Observation"):
            assert not any(f"/{rt}?" in u for u in urls), f"{rt} must not be touched by a definitions wipe"

    async def test_carries_auth_headers(self):
        """A remote MCS rejects every DELETE without credentials."""
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir", auth_headers=headers)

        for call in mock_ctx.delete.await_args_list:
            assert call.kwargs.get("headers") == headers, "definitions delete went out unauthenticated"

    async def test_raises_on_unauthorized(self):
        """A 401 must abort, naming the wipe — same rule as the other two wipes.

        Without the explicit check this still failed, but via the fallback sweep's
        "not authorized to enumerate" error, which names the wrong operation.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._mock_client(mock_httpx, response=_make_response(401, {}))
            with pytest.raises(RuntimeError, match="Not authorized to wipe"):
                await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

    async def test_fallback_sweep_carries_auth_headers(self):
        """When conditional delete is unsupported, the per-resource sweep stays authenticated."""
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx, response=_make_response(405, {}))
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir", auth_headers=headers)

        assert mock_ctx.get.await_count > 0, "fallback sweep never ran"
        for call in mock_ctx.get.await_args_list:
            assert call.kwargs.get("headers") == headers, "fallback GET went out unauthenticated"


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


async def test_fhir_test_connection_success(mock_fhir_metadata):
    """test_connection returns connected status with FHIR version."""
    mock_response = _make_response(200, mock_fhir_metadata)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await fhir_test_connection("https://example.com/fhir")

    assert result["status"] == "connected"
    assert result["fhir_version"] == "4.0.1"
    assert result["software"] == "HAPI FHIR Test"


async def test_fhir_test_connection_failed():
    """test_connection raises FhirOperationError when the server is unreachable."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://bad-server/fhir")
        assert exc_info.value.status_code is None
        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


async def test_fhir_test_connection_401():
    """test_connection raises FhirOperationError with status_code=401."""
    mock_response = httpx.Response(
        401,
        json={"error": "unauthorized"},
        request=httpx.Request("GET", "https://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://example.com/fhir")
        assert exc_info.value.status_code == 401


async def test_fhir_test_connection_500():
    """test_connection raises FhirOperationError with status_code=500."""
    mock_response = httpx.Response(
        500,
        json={"error": "server error"},
        request=httpx.Request("GET", "https://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://example.com/fhir")
        assert exc_info.value.status_code == 500


async def test_fhir_test_connection_timeout():
    """test_connection raises FhirOperationError wrapping a timeout."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://slow-server/fhir")
        assert exc_info.value.status_code is None
        assert isinstance(exc_info.value.__cause__, httpx.TimeoutException)


# ---------------------------------------------------------------------------
# resolve_evaluated_resource
# ---------------------------------------------------------------------------


async def test_resolve_evaluated_resource():
    """resolve_evaluated_resource fetches a resource by reference."""
    resource = {"resourceType": "Patient", "id": "p1"}
    mock_response = _make_response(200, resource)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolve_evaluated_resource("Patient/p1", "http://mcs/fhir")

    assert result == resource


async def test_resolve_evaluated_resource_requires_base_url():
    """The env-var fallback is gone (issue #397).

    It defaulted to settings.MEASURE_ENGINE_URL with no credentials, which only
    ever worked for a local unauthenticated HAPI. Every caller now names the server.
    """
    with pytest.raises(TypeError):
        await resolve_evaluated_resource("Patient/p1")  # type: ignore[call-arg]


async def test_resolve_evaluated_resource_targets_the_given_server():
    resource = {"resourceType": "Patient", "id": "p1"}
    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _make_response(200, resource)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await resolve_evaluated_resource(
            "Patient/p1", "https://mcs.example.org/fhir", {"Authorization": "Bearer tok-r"}
        )

    assert seen["url"] == "https://mcs.example.org/fhir/Patient/p1"
    assert seen["headers"] == {"Authorization": "Bearer tok-r"}


# ---------------------------------------------------------------------------
# snapshot_evaluated_resources
# ---------------------------------------------------------------------------


async def test_snapshot_evaluated_resources_returns_resolved_list():
    """Each evaluatedResource reference is resolved and returned in order."""
    from app.services.fhir_client import snapshot_evaluated_resources

    measure_report = {
        "resourceType": "MeasureReport",
        "evaluatedResource": [
            {"reference": "Patient/p1"},
            {"reference": "Encounter/e1"},
        ],
    }
    fake_resources = {
        "Patient/p1": {"resourceType": "Patient", "id": "p1"},
        "Encounter/e1": {"resourceType": "Encounter", "id": "e1"},
    }

    async def fake_resolve(ref, base_url=None, auth_headers=None):
        return fake_resources[ref]

    with patch("app.services.fhir_client.resolve_evaluated_resource", side_effect=fake_resolve):
        result = await snapshot_evaluated_resources(measure_report)

    assert result == [fake_resources["Patient/p1"], fake_resources["Encounter/e1"]]


async def test_snapshot_evaluated_resources_skips_failed_refs():
    """Per-reference failures are logged and skipped, not raised — partial snapshots
    are still useful and the caller has already persisted the MeasureReport."""
    from app.services.fhir_client import snapshot_evaluated_resources

    measure_report = {
        "evaluatedResource": [
            {"reference": "Patient/p1"},
            {"reference": "Encounter/e1"},
            {"reference": "Condition/c1"},
        ],
    }

    async def fake_resolve(ref, base_url=None, auth_headers=None):
        if ref == "Encounter/e1":
            raise RuntimeError("404 not found")
        return {"resourceType": ref.split("/")[0], "id": ref.split("/")[1]}

    with patch("app.services.fhir_client.resolve_evaluated_resource", side_effect=fake_resolve):
        result = await snapshot_evaluated_resources(measure_report)

    assert len(result) == 2
    assert {r["id"] for r in result} == {"p1", "c1"}


async def test_snapshot_evaluated_resources_returns_none_when_no_refs():
    """No evaluatedResource entries → helper returns None.

    The orchestrator coalesces this to [] before storing so the column distinguishes
    'legacy row, never snapshotted' (NULL) from 'new row, no refs to snapshot' ([])."""
    from app.services.fhir_client import snapshot_evaluated_resources

    assert await snapshot_evaluated_resources({"resourceType": "MeasureReport"}) is None
    assert await snapshot_evaluated_resources({"evaluatedResource": []}) is None
    assert await snapshot_evaluated_resources({}) is None


# ---------------------------------------------------------------------------
# Measure-management functions: base_url targeting (issue #396)
#
# These four tests are the regression guard for the whole bug. Every measure-
# management call must hit the base_url it was handed and never
# settings.MEASURE_ENGINE_URL — that env-var read is what made the measure list
# ignore the connected MCS. `_ACTIVE_MCS` is deliberately different from the
# env-var default (`http://hapi-fhir-measure:8080/fhir`) so a regression shows
# up as a failed URL assertion rather than an accidental pass.
# ---------------------------------------------------------------------------

_ACTIVE_MCS = "https://attendee-mcs.example.com/fhir"


async def test_list_measures(mock_measure_bundle):
    """list_measures queries the passed base_url, not settings.MEASURE_ENGINE_URL."""
    from app.config import settings

    mock_response = _make_response(200, mock_measure_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await list_measures(_ACTIVE_MCS, auth_headers={"Authorization": "Bearer tok"})

    assert result["resourceType"] == "Bundle"
    assert len(result["entry"]) == 1
    called_url = mock_ctx.get.await_args.args[0]
    assert called_url.startswith(_ACTIVE_MCS)
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert mock_ctx.get.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


async def test_upload_measure_bundle():
    """upload_measure_bundle POSTs to the passed base_url, not the env-var engine."""
    from app.config import settings

    input_bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    response_bundle = {"resourceType": "Bundle", "type": "transaction-response"}
    mock_response = _make_response(200, response_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await upload_measure_bundle(
            input_bundle,
            _ACTIVE_MCS,
            auth_headers={"Authorization": "Bearer tok"},
        )

    assert result["type"] == "transaction-response"
    posted_url = mock_ctx.post.await_args.args[0]
    assert posted_url == _ACTIVE_MCS
    assert posted_url != settings.MEASURE_ENGINE_URL
    headers = mock_ctx.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Content-Type"] == "application/fhir+json"


async def test_upload_measure_bundle_threads_base_url_into_remap():
    """upload_measure_bundle hands its base_url + credentials to the ValueSet remap.

    `_remap_valueset_ids_for_hapi` is tested directly elsewhere, so only the
    wiring is at risk — but "the passed base_url is used everywhere" is the
    central guard of issue #396, and an untested wire is where it would rot.
    Asserted with the collaborator mocked so the assertion can't be satisfied
    by the remap silently no-opping on a non-200 lookup.
    """
    from app.config import settings

    input_bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [{"resource": {"resourceType": "ValueSet", "id": "1014", "url": "http://vs.example.com/1014"}}],
    }
    mock_response = _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

    with (
        patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx,
        patch(
            "app.services.fhir_client._remap_valueset_ids_for_hapi",
            new_callable=AsyncMock,
            side_effect=lambda entries, client, base_url, auth_headers=None: entries,
        ) as mock_remap,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await upload_measure_bundle(
            input_bundle,
            _ACTIVE_MCS,
            auth_headers={"Authorization": "Bearer tok"},
        )

    mock_remap.assert_awaited_once()
    remap_base = mock_remap.await_args.args[2]
    assert remap_base == _ACTIVE_MCS
    assert remap_base != settings.MEASURE_ENGINE_URL
    assert mock_remap.await_args.args[3] == {"Authorization": "Bearer tok"}


async def test_delete_measure_targets_passed_base_url():
    """delete_measure DELETEs against the passed base_url with its auth headers."""
    from app.config import settings

    mock_response = _make_response(204, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await delete_measure("CMS122", _ACTIVE_MCS, auth_headers={"Authorization": "Bearer tok"})

    called_url = mock_ctx.delete.await_args.args[0]
    assert called_url == f"{_ACTIVE_MCS}/Measure/CMS122"
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert mock_ctx.delete.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


async def test_remap_valueset_ids_queries_passed_base_url():
    """_remap_valueset_ids_for_hapi resolves existing ValueSets on the upload target.

    Querying the env-var engine here would rewrite ids to values that don't
    exist on the server the bundle is about to be POSTed to.
    """
    from app.config import settings

    entries = [
        {
            "resource": {"resourceType": "ValueSet", "id": "1014", "url": "http://vs.example.com/1014"},
            "request": {"method": "PUT", "url": "ValueSet/1014"},
        }
    ]
    existing = {"entry": [{"resource": {"id": "1014-20240112"}}]}
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(200, existing))

    out = await _remap_valueset_ids_for_hapi(entries, client, _ACTIVE_MCS, {"Authorization": "Bearer tok"})

    called_url = client.get.await_args.args[0]
    assert called_url == f"{_ACTIVE_MCS}/ValueSet"
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert client.get.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}
    # And the remap itself still happened.
    assert out[0]["resource"]["id"] == "1014-20240112"
    assert out[0]["request"]["url"] == "ValueSet/1014-20240112"


# ---------------------------------------------------------------------------
# measure_exists
# ---------------------------------------------------------------------------


async def test_measure_exists_true_when_total_positive():
    """total > 0 in the count bundle means the measure is present."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "total": 1}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        assert await measure_exists("CMS122", _ACTIVE_MCS) is True

    assert mock_ctx.get.await_args.args[0] == f"{_ACTIVE_MCS}/Measure"
    assert mock_ctx.get.await_args.kwargs["params"] == {"_id": "CMS122", "_summary": "count"}


async def test_measure_exists_false_when_total_zero():
    """total == 0 means the measure is absent — a normal answer, not an error."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "total": 0}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        assert await measure_exists("CMS122", _ACTIVE_MCS) is False


async def test_measure_exists_propagates_transport_errors():
    """Connection failures must NOT be swallowed into False.

    POST /jobs distinguishes "measure absent" (400) from "MCS unreachable"
    (502); collapsing the two here would make that impossible.
    """
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.ConnectError):
            await measure_exists("CMS122", _ACTIVE_MCS)


async def test_measure_exists_propagates_http_status_errors():
    """A 500 from the MCS raises rather than reporting the measure as missing."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(500, {"resourceType": "OperationOutcome"}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await measure_exists("CMS122", _ACTIVE_MCS)


# ---------------------------------------------------------------------------
# _acquire_smart_token
# ---------------------------------------------------------------------------


_SMART_CREDENTIALS = {
    "client_id": "c1",
    "client_secret": "s1",
    "token_endpoint": "https://auth.example.com/token",
}


class TestAcquireSmartToken:
    async def test_success(self):
        """_acquire_smart_token returns the access_token on success."""
        token_response = httpx.Response(
            200,
            json={"access_token": "tok123", "token_type": "bearer"},
            request=httpx.Request("POST", "https://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(_SMART_CREDENTIALS)

        assert token == "tok123"
        call_args = mock_ctx.post.call_args
        assert call_args[0][0] == "https://auth.example.com/token"
        posted_data = call_args.kwargs.get("data") or call_args[1].get("data")
        assert posted_data["grant_type"] == "client_credentials"
        assert posted_data["client_id"] == "c1"
        assert posted_data["client_secret"] == "s1"

    async def test_401_raises(self):
        """_acquire_smart_token raises HTTPStatusError on 401."""
        error_response = httpx.Response(
            401,
            json={"error": "unauthorized"},
            request=httpx.Request("POST", "http://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=error_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_500_raises(self):
        """_acquire_smart_token raises HTTPStatusError on 500."""
        error_response = httpx.Response(
            500,
            json={"error": "server error"},
            request=httpx.Request("POST", "http://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=error_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_network_error_raises(self):
        """_acquire_smart_token propagates network errors."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.ConnectError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_ssrf_blocked_http_external(self):
        """_acquire_smart_token rejects plain http for non-localhost token_endpoint."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://evil.example.com/token",
        }
        with pytest.raises(ValueError, match="SSRF protection"):
            await _acquire_smart_token(creds)

    async def test_ssrf_blocked_rfc1918(self):
        """_acquire_smart_token rejects RFC-1918 addresses."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "https://192.168.1.1/token",
        }
        with pytest.raises(ValueError, match="SSRF protection"):
            await _acquire_smart_token(creds)

    async def test_ssrf_allowed_localhost_http(self):
        """_acquire_smart_token allows http://localhost for local dev."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://localhost:9090/token",
        }
        token_response = httpx.Response(
            200,
            json={"access_token": "local-tok"},
            request=httpx.Request("POST", "http://localhost:9090/token"),
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(creds)
        assert token == "local-tok"

    async def test_ssrf_allowed_127_http(self):
        """_acquire_smart_token allows http://127.0.0.1 for local dev."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://127.0.0.1:8080/token",
        }
        token_response = httpx.Response(
            200,
            json={"access_token": "loopback-tok"},
            request=httpx.Request("POST", "http://127.0.0.1:8080/token"),
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(creds)
        assert token == "loopback-tok"


# ---------------------------------------------------------------------------
# _validate_ssrf_url
# ---------------------------------------------------------------------------


class TestValidateSsrfUrl:
    def test_https_external_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("https://fhir.example.com/token")  # should not raise

    def test_http_localhost_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://localhost:8080/fhir")  # should not raise

    def test_http_127_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://127.0.0.1/fhir")  # should not raise

    def test_http_external_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="must use https"):
            _validate_ssrf_url("http://external.example.com/fhir")

    def test_ftp_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="not allowed"):
            _validate_ssrf_url("ftp://example.com/file")

    def test_rfc1918_10_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://10.0.0.1/fhir")

    def test_rfc1918_172_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://172.16.0.1/fhir")

    def test_rfc1918_192_168_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://192.168.100.200/fhir")

    def test_imds_endpoint_http_blocked(self):
        """Classic AWS IMDSv1 endpoint — http with non-local host is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="must use https"):
            _validate_ssrf_url("http://169.254.169.254/latest/meta-data/")

    def test_imds_endpoint_https_blocked(self):
        """AWS IMDS link-local over https is blocked by IP range check."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_allowed(self):
        """::1 is in the local dev allowlist."""
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://[::1]:8080/fhir")  # should not raise

    def test_ipv6_link_local_blocked(self):
        """fe80:: link-local IPv6 is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://[fe80::1]/fhir")

    def test_ipv6_ula_blocked(self):
        """fc00::/7 Unique Local Address IPv6 is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://[fd00::1]/fhir")


async def test_verify_fhir_connection_ssrf_blocked():
    """verify_fhir_connection raises ValueError for http non-localhost URLs."""
    with pytest.raises(ValueError, match="SSRF protection"):
        await fhir_test_connection("http://internal.corp.example.com/fhir")


# ---------------------------------------------------------------------------
# DataRequirementsStrategy
# ---------------------------------------------------------------------------


async def test_data_requirements_strategy_uses_requirements():
    """DataRequirementsStrategy fetches resources per $data-requirements entries."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Patient"},
            {"type": "Observation"},
        ],
    }
    patient_resource = {"resourceType": "Patient", "id": "p1"}
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    get_responses = {
        "Measure/m1/$data-requirements": _make_response(200, data_req_response),
        "Observation?subject=Patient/p1": _make_response(200, obs_bundle),
        "Patient/p1": _make_response(200, patient_resource),
    }

    async def mock_get(url, **kwargs):
        for key, resp in get_responses.items():
            if key in url:
                return resp
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert len(resources) == 2
    types = {r["resourceType"] for r in resources}
    assert types == {"Patient", "Observation"}


async def test_data_requirements_targets_the_jobs_mcs_with_credentials():
    """$data-requirements goes to the job's MCS, authenticated (issue #397).

    It previously read settings.MEASURE_ENGINE_URL with no credentials, so a job
    against a remote MCS asked the LOCAL engine what data the measure needs — and
    silently fell back to $everything when that answer was wrong or the call 401'd.
    """
    seen: dict[str, object] = {}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Patient"}]})
        return _make_response(200, {"resourceType": "Patient", "id": "p1"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy(
            "m1",
            mcs_url="https://mcs.example.org/fhir",
            mcs_auth_headers={"Authorization": "Bearer tok-dr"},
        )
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert seen["url"] == "https://mcs.example.org/fhir/Measure/m1/$data-requirements"
    assert seen["headers"] == {"Authorization": "Bearer tok-dr"}


async def test_data_requirements_requires_an_mcs_url():
    """No env-var default — the caller must say which engine to ask."""
    with pytest.raises(TypeError):
        DataRequirementsStrategy("m1")  # type: ignore[call-arg]


async def test_data_requirements_strategy_falls_back_on_empty():
    """DataRequirementsStrategy falls back to $everything when $data-requirements returns no entries."""
    empty_lib = {"resourceType": "Library", "dataRequirement": []}
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    call_count = {"n": 0}

    async def mock_get(url, **kwargs):
        call_count["n"] += 1
        if "$data-requirements" in url:
            return _make_response(200, empty_lib)
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert len(resources) == 1
    assert resources[0]["resourceType"] == "Patient"
    assert call_count["n"] >= 2


async def test_data_requirements_strategy_falls_back_on_error():
    """DataRequirementsStrategy falls back to $everything when $data-requirements raises."""
    import httpx as _httpx_module

    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            raise _httpx_module.ConnectError("MCS unreachable")
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert len(resources) == 1


async def test_data_requirements_strategy_fetch_fails_falls_back_to_everything():
    """DataRequirementsStrategy falls back to $everything when CDR fetch raises."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    call_count = {"n": 0}

    async def mock_get(url, **kwargs):
        call_count["n"] += 1
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Patient/p1" in url and "$everything" not in url:
            raise httpx.ConnectError("CDR unreachable")
        # fallback $everything call
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # Should have fallen back to $everything and returned the patient
    assert any(r.get("resourceType") == "Patient" for r in resources)


async def test_data_requirements_strategy_dedup_skips_duplicate_types():
    """DataRequirementsStrategy skips a resource type that appears twice in requirements."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation"},
            {"type": "Observation"},  # duplicate — should only query once
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            side_effect=lambda url, **kw: (
                _make_response(200, data_req_response)
                if "$data-requirements" in url
                else _make_response(200, obs_bundle)
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # Only one Observation even though type appeared twice
    obs_resources = [r for r in resources if r.get("resourceType") == "Observation"]
    assert len(obs_resources) == 1


async def test_data_requirements_strategy_non_200_patient_not_appended():
    """DataRequirementsStrategy skips Patient resource when CDR returns non-200."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            side_effect=lambda url, **kw: (
                _make_response(200, data_req_response)
                if "$data-requirements" in url
                else _make_response(404, {"resourceType": "OperationOutcome"})
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # 404 from CDR means no resources returned (no fallback for non-200 within _fetch_by_requirements)
    assert resources == []


async def test_data_requirements_strategy_patient_failure_reports_partial_gather():
    """F2: a non-200 Patient read must be recorded as a FailedResourceFetch so
    the gather is reported as partial (has_partial_failure) — previously it
    was swallowed with no failure record and no way to attribute a later
    failure to the missing Patient. Patient must still NOT be added to
    required_types: the declared Condition requirement succeeds, so no
    $everything fallback should fire."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    cond_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/p1" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        if "Condition" in url:
            return _make_response(200, cond_bundle)
        return _make_response(200, {"resourceType": "Bundle", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert gather_result.resources == [{"resourceType": "Condition", "id": "c1"}]
    assert gather_result.has_partial_failure is True
    assert [f.resource_type for f in gather_result.failed_types] == ["Patient"]
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0  # Condition (the only declared requirement) succeeded


async def test_data_requirements_strategy_non_200_resource_entries_skipped():
    """When ALL required types return non-200, the $everything fallback is triggered."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "$everything" in url:
            return _make_response(200, empty_bundle)
        # CDR per-type query fails
        return _make_response(500, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # All types failed → fell back to $everything which returned empty bundle
    assert resources == []
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 1


async def test_fetch_by_requirements_code_filter_appends_code_in():
    """codeFilter.valueSet is translated to code:in= search parameter (AC2)."""
    vs_url = "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.198.12.1134"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "Observation",
                "codeFilter": [{"valueSet": vs_url}],
            }
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            # Patient is now always fetched too (I3) — simulate "not found" so
            # this test's resource count stays scoped to the declared Observation
            # requirement, which is what it's actually exercising.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_url = next((u for u in captured_urls if "Observation" in u), None)
    assert obs_url is not None
    assert "code:in=" in obs_url
    assert vs_url in obs_url
    assert len(resources) == 1


async def test_fetch_by_requirements_date_filter_does_not_add_params():
    """dateFilter entries do not modify the URL — type-only query used (AC2)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "Observation",
                "dateFilter": [{"path": "effective", "valuePeriod": {"start": "2024-01-01", "end": "2024-12-31"}}],
            }
        ],
    }
    obs_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    obs_url = next((u for u in captured_urls if "Observation" in u), None)
    assert obs_url is not None
    assert "code:in" not in obs_url


async def test_fetch_by_requirements_no_filter_type_only():
    """dataRequirement with no codeFilter generates plain type+subject query (AC2 baseline)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Encounter"}],
    }
    enc_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Encounter", "id": "e1"}}],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            # Patient is now always fetched too (I3) — simulate "not found" so
            # this test's resource count stays scoped to the declared
            # Encounter requirement, which is what it's actually exercising.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, enc_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    enc_url = next((u for u in captured_urls if "Encounter" in u), None)
    assert enc_url is not None
    assert "code:in" not in enc_url
    assert len(resources) == 1


async def test_fetch_by_requirements_one_type_fails_partial_result_no_fallback():
    """One type fails CDR fetch — others succeed; partial result returned without $everything (AC5)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation"},
            {"type": "Condition"},
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Observation" in url:
            return _make_response(200, obs_bundle)
        if "Condition" in url:
            raise httpx.ConnectError("CDR unreachable for Condition")
        return _make_response(200, {"resourceType": "Bundle", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # Observation fetched; Condition skipped; no $everything fallback
    assert any(r.get("resourceType") == "Observation" for r in resources)
    assert not any(r.get("resourceType") == "Condition" for r in resources)
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0


async def test_fetch_by_requirements_multiple_valuesets_drop_filter_and_overfetch():
    """Two requirements of the same type with DIFFERENT valuesets must not be
    ANDed via repeated code:in= params (I3) — that would narrow, not union,
    the result. The type is fetched unfiltered instead."""
    vs1 = "http://example.org/ValueSet/vs1"
    vs2 = "http://example.org/ValueSet/vs2"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs1}]},
            {"type": "Observation", "codeFilter": [{"valueSet": vs2}]},
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Observation", "id": "o1"}},
            {"resource": {"resourceType": "Observation", "id": "o2"}},
        ],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1, f"Observation should be fetched exactly once, got {obs_urls}"
    assert "code:in" not in obs_urls[0]
    assert len(resources) == 2


async def test_fetch_by_requirements_single_valueset_filter_preserved():
    """A single distinct valueset across all of a type's requirements still
    keeps the code:in= filter (I3 — the conservative path is only for
    disagreeing/mixed requirements)."""
    vs_url = "http://example.org/ValueSet/vs1"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},  # same valueset, repeated
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1
    assert f"code:in={vs_url}" in obs_urls[0]
    assert len(resources) == 1


async def test_fetch_by_requirements_mixed_filtered_and_unfiltered_drops_filter():
    """One requirement for a type carries a codeFilter, another for the SAME
    type has none — the mix must drop the filter and over-fetch, same as the
    multiple-distinct-valuesets case (coverage-audit gap fill for the
    `has_unfiltered` branch of the grouping logic in I3)."""
    vs_url = "http://example.org/ValueSet/vs1"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},
            {"type": "Observation"},  # same type, no codeFilter at all
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Observation", "id": "o1"}},
            {"resource": {"resourceType": "Observation", "id": "o2"}},
        ],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1, f"Observation should be fetched exactly once, got {obs_urls}"
    assert "code:in" not in obs_urls[0]
    assert len(resources) == 2


async def test_fetch_by_requirements_patient_always_fetched_even_when_absent():
    """Patient is fetched by direct read even when it's not a declared
    dataRequirement — $everything always includes it, and its absence fails
    evaluation regardless (I3)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation"}],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }
    patient_resource = {"resourceType": "Patient", "id": "p1"}

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/p1" in url:
            return _make_response(200, patient_resource)
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert any(u.endswith("/Patient/p1") for u in captured_urls), (
        f"Patient should always be fetched by direct read, got {captured_urls}"
    )
    assert any(r.get("resourceType") == "Patient" and r.get("id") == "p1" for r in resources)
    assert any(r.get("resourceType") == "Observation" for r in resources)


async def test_fetch_by_requirements_all_required_types_fail_triggers_fallback_despite_patient():
    """A forced Patient-fetch success/failure must not mask "all REQUIRED types
    failed" — the fallback trigger only counts declared dataRequirement types."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "$everything" in url:
            return _make_response(200, empty_bundle)
        if "/Patient/p1" in url:
            # Patient direct-read failure — must not count toward "required
            # types" and must not itself block the fallback below.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        # Condition search fails
        return _make_response(500, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert resources == []
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 1


async def test_data_requirements_strategy_gather_patients_delegates_to_batch():
    """DataRequirementsStrategy.gather_patients uses the same BatchQuery logic."""
    patient_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, patient_bundle))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 1
    assert patients[0]["id"] == "p1"


class FakeSyncResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSyncClient:
    def __init__(self, *, get_responses: list[FakeSyncResponse], post_responses: list[FakeSyncResponse]) -> None:
        self.get_responses = get_responses
        self.post_responses = post_responses
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, **kwargs):
        self.get_calls.append(url)
        return self.get_responses.pop(0)

    def post(self, url: str, json: dict | None = None, **kwargs):
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)


def test_wait_for_valueset_expansion_returns_per_url_status(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[
            FakeSyncResponse(200, {"entry": []}),
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "vs-ok"}}]}),
        ],
        post_responses=[
            FakeSyncResponse(200, {"expansion": {"total": 42, "contains": [{"code": "a"}]}}),
        ],
    )
    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("app.services.fhir_client.time.sleep", lambda seconds: None)

    with caplog.at_level("WARNING"):
        expanded = wait_for_valueset_expansion("http://hapi/fhir", ["http://vs/ok", "http://vs/missing"], timeout_s=1)

    assert expanded == {"http://vs/ok": 42}
    assert client.post_calls == [("http://hapi/fhir/ValueSet/vs-ok/$expand?count=2", None)]
    assert "ValueSet not found for expansion wait" in caplog.text


def test_wait_for_valueset_expansion_logs_timeout(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[FakeSyncResponse(200, {"entry": [{"resource": {"id": "vs-timeout"}}]})],
        post_responses=[],
    )
    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)

    with caplog.at_level("WARNING"):
        expanded = wait_for_valueset_expansion("http://hapi/fhir", ["http://vs/timeout"], timeout_s=0)

    assert expanded == {}
    assert "ValueSet expansion timed out" in caplog.text


# ---------------------------------------------------------------------------
# MCS auth wiring — evaluate_measure / resolve_evaluated_resource
# (regression: remote MCS connections got 401 because no auth was ever sent)
# ---------------------------------------------------------------------------


async def test_evaluate_measure_sends_auth_headers(mock_measure_report):
    """evaluate_measure forwards auth headers to the MCS."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await evaluate_measure(
            "measure-1",
            "patient-1",
            "2024-01-01",
            "2024-12-31",
            measure_engine_url="https://mcs.example.org/fhir",
            auth_headers={"Authorization": "Bearer tok-123"},
        )

    sent = mock_ctx.get.call_args.kwargs.get("headers")
    assert sent is not None, "evaluate_measure must pass headers to the MCS"
    assert sent.get("Authorization") == "Bearer tok-123"


async def test_evaluate_measure_without_auth_sends_no_authorization(mock_measure_report):
    """Unauthenticated MCS (local HAPI) keeps working — no Authorization header."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    sent = mock_ctx.get.call_args.kwargs.get("headers") or {}
    assert "Authorization" not in sent


async def test_resolve_evaluated_resource_uses_given_base_and_auth():
    """Snapshot reads target the job's MCS with its credentials, not the env default."""
    mock_response = _make_response(200, {"resourceType": "Condition", "id": "c1"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await resolve_evaluated_resource(
            "Condition/c1",
            base_url="https://mcs.example.org/fhir",
            auth_headers={"Authorization": "Bearer tok-123"},
        )

    assert mock_ctx.get.call_args[0][0] == "https://mcs.example.org/fhir/Condition/c1"
    assert mock_ctx.get.call_args.kwargs.get("headers", {}).get("Authorization") == "Bearer tok-123"


# ---------------------------------------------------------------------------
# McsTarget (issue #397 slice 3)
# ---------------------------------------------------------------------------


def test_mcs_target_is_frozen():
    """Immutable on purpose: it is threaded through ~16 call sites, and a helper
    mutating the shared target would silently re-point every later call."""
    from dataclasses import FrozenInstanceError

    from app.services.fhir_client import McsTarget

    t = McsTarget(url="https://mcs.example.org/fhir", auth_headers={}, is_read_only=False, wipe_before_job=False)
    with pytest.raises(FrozenInstanceError):
        t.url = "https://elsewhere.example.org/fhir"  # type: ignore[misc]


def test_mcs_target_requires_every_field():
    """No defaults. A default target is exactly how issue #397 stayed invisible."""
    from app.services.fhir_client import McsTarget

    with pytest.raises(TypeError):
        McsTarget(url="https://mcs.example.org/fhir")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# DEQM $submit-data support (spec: 2026-08-21-deqm-submit-data-workflow)
# ---------------------------------------------------------------------------


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
            _mock_async_client(
                mock_httpx, get=AsyncMock(return_value=_make_response(404, {"resourceType": "OperationOutcome"}))
            )
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
        cap = self._capability(
            [{"name": "submit-data", "definition": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"}]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, cap)))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_stu5_when_deqm_canonical_has_version_suffix(self):
        """I4: `canonical|version` must still match — not just the bare canonical."""
        cap = self._capability(
            [
                {
                    "name": "whatever",
                    "definition": "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data|5.0.0",
                }
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, cap)))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5

    async def test_base_when_definition_only_a_prefix_of_the_canonical(self):
        """I4: tightened match. A server that cites the DEQM canonical as a mere
        PREFIX (e.g. appends a path segment instead of a `|version`) — not an
        exact match or `|version` suffix — must not be classified stu5. A loose
        startswith() here 400s every $deqm-submit-data POST with no downgrade
        for a server that never actually implements it."""
        cap = self._capability(
            [
                {
                    "name": "whatever",
                    "definition": "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data-extended",
                }
            ]
        )
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
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_STU5,
                measure_id="M1",
            )
        assert post.call_args[0][0] == "http://mcs/Measure/$deqm-submit-data"

    async def test_posts_to_instance_level_operation_in_base_mode(self):
        # Empirically verified against a local prebaked HAPI measure server: the
        # type-level POST /Measure/$submit-data returns 400 not-supported ("does
        # not know how to handle POST operation[Measure/$submit-data]"), while the
        # instance-level POST /Measure/{id}/$submit-data returns 200 and performs a
        # real upsert. Base-fallback mode must use the instance-level shape.
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
        assert post.call_args[0][0] == "http://mcs/Measure/M1/$submit-data"

    async def test_base_and_stu5_modes_produce_different_url_shapes(self):
        # Regression guard for the ruling this test file encodes: base-fallback
        # is instance-level (Measure/{id}/$submit-data) because that's the only
        # shape HAPI accepts; STU5 is type-level (Measure/$deqm-submit-data)
        # because STU5's OperationDefinition formally declares the operation
        # type-level (`instance: false`). The two must never collapse to one shape.
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
            base_url = post.call_args[0][0]

            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_STU5,
                measure_id="M1",
            )
            stu5_url = post.call_args[0][0]

        assert base_url == "http://mcs/Measure/M1/$submit-data"
        assert stu5_url == "http://mcs/Measure/$deqm-submit-data"
        assert base_url != stu5_url

    async def test_409_conflict_retries_and_succeeds(self):
        """F12: an incidental HAPI ResourceVersionConflictException (409) —
        e.g. a patient's own resources racing a concurrent read/write on the
        same server — is retried once rather than failing the patient
        outright. (The primary source of these conflicts, every patient
        upserting the same shared Organization/lenny-reporter, is fixed
        upstream: that resource is now PUT once per job, not inlined per
        patient — see workflows.build_submission_workflow.)"""
        post = AsyncMock(
            side_effect=[
                _make_response(409, {"resourceType": "OperationOutcome"}),
                _make_response(200, {"resourceType": "Bundle"}),
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()) as sleep:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert post.await_count == 2
        sleep.assert_awaited_once()

    async def test_412_conflict_also_retries(self):
        post = AsyncMock(
            side_effect=[
                _make_response(412, {"resourceType": "OperationOutcome"}),
                _make_response(200, {"resourceType": "Bundle"}),
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()):
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert post.await_count == 2

    async def test_409_conflict_exhausts_retries_and_raises(self):
        """After 1 retry (2 total attempts) a persistent 409 still raises."""
        post = AsyncMock(return_value=_make_response(409, {"resourceType": "OperationOutcome"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(FhirOperationError) as exc_info:
                    await submit_data(
                        mcs_url="http://mcs",
                        parameters={"resourceType": "Parameters"},
                        mode=SUBMIT_DATA_MODE_BASE,
                        measure_id="M1",
                    )
        assert exc_info.value.status_code == 409
        assert post.await_count == 2

    async def test_raises_fhir_operation_error_on_4xx(self):
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "invalid", "diagnostics": "bad payload"}],
        }
        post = AsyncMock(return_value=_make_response(400, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError) as exc_info:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert exc_info.value.status_code == 400
        assert exc_info.value.operation == "submit-data"


async def test_data_requirements_cached_across_patients():
    """$data-requirements is called once per job, not once per patient.

    Calling it per patient compiles the measure's CQL in the engine for every
    patient; at 319 patients that drove the measure engine past its container
    memory limit and got it OOM-killed mid-job.
    """
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    calls: list[str] = []

    async def mock_get(url, **kwargs):
        calls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        for pid in ("p1", "p2", "p3"):
            await strategy.gather_patient_data("http://cdr/fhir", pid, {})

    assert sum("$data-requirements" in c for c in calls) == 1


async def test_data_requirements_cached_under_concurrent_first_callers():
    """Concurrent first patients do not each issue the same expensive call."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    calls: list[str] = []

    async def mock_get(url, **kwargs):
        calls.append(url)
        if "$data-requirements" in url:
            await asyncio.sleep(0.01)  # widen the window a stampede would use
            return _make_response(200, data_req_response)
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await asyncio.gather(*(strategy.gather_patient_data("http://cdr/fhir", f"p{i}", {}) for i in range(8)))

    assert sum("$data-requirements" in c for c in calls) == 1


async def test_data_requirements_failure_is_not_cached():
    """A transient $data-requirements failure must not pin the job to $everything."""
    attempts = {"n": 0}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _make_response(500, {})
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Patient"}]})
        if "$everything" in url:
            return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})  # fails -> $everything
        await strategy.gather_patient_data("http://cdr/fhir", "p2", {})  # retries, succeeds

    assert attempts["n"] == 2


async def test_filtered_query_failure_retries_unfiltered():
    """A code:in= filter the CDR cannot resolve must not silently drop the type.

    HAPI returns HAPI-2788 "Unknown ValueSet" for VSAC canonicals it never
    loaded. Treating that as "no resources of this type" cost nine patients
    their CMS130 denominator-exclusion while the job still reported success.
    """
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "ServiceRequest",
                "codeFilter": [{"path": "code", "valueSet": "http://cts.nlm.nih.gov/fhir/ValueSet/1.2.3"}],
            }
        ],
    }
    seen: list[str] = []

    async def mock_get(url, **kwargs):
        seen.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "ServiceRequest" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "ServiceRequest" in url:
            if "code:in=" in url:
                return _make_response(400, {"resourceType": "OperationOutcome"})
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "ServiceRequest", "id": "sr1"}}],
                    "link": [],
                },
            )
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    types = {r["resourceType"] for r in result.resources}
    assert "ServiceRequest" in types, "unfiltered retry should have recovered the resource"
    assert result.failed_types == [], "a recovered type must not be reported as a partial failure"
    assert any("code:in=" in u for u in seen), "filtered query should be attempted first"


async def test_filtered_retry_does_not_duplicate_on_partial_pagination():
    """A failure part-way through pagination must not leave half a page behind."""
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation", "codeFilter": [{"path": "code", "valueSet": "http://vs/1"}]}],
    }
    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [{"relation": "next", "url": "http://cdr/fhir/Observation?page=2&code:in=http://vs/1"}],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "Observation" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "Observation" in url:
            if "code:in=" in url:
                # first page succeeds, second page blows up mid-pagination
                return _make_response(200, page1) if "page=2" not in url else _make_response(500, {})
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
                    "link": [],
                },
            )
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    obs = [r for r in result.resources if r["resourceType"] == "Observation"]
    assert len(obs) == 1, f"expected the partial page to be discarded, got {obs}"


async def test_unfiltered_query_failure_still_reported():
    """When even the unfiltered retry fails, the type is still a partial failure."""
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Condition", "codeFilter": [{"path": "code", "valueSet": "http://vs/1"}]},
            {"type": "Encounter"},
        ],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "Condition" not in url and "Encounter" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "Condition" in url:
            return _make_response(500, {})
        if "Encounter" in url:
            return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert [f.resource_type for f in result.failed_types] == ["Condition"]
