"""Tests for GET /health endpoint."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_health_response_includes_cdr_name(client, test_session):
    """GET /health includes cdr name in the cdr section."""
    from app.models.config import AuthType, CDRConfig

    cdr = CDRConfig(
        cdr_url="http://my-cdr.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="My Test CDR",
        is_default=False,
        is_read_only=False,
    )
    test_session.add(cdr)
    await test_session.commit()

    import httpx as _httpx

    mock_response = _httpx.Response(200, json={"resourceType": "CapabilityStatement"})
    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cdr"]["status"] == "connected"
    assert data["cdr"]["name"] == "My Test CDR"


async def test_health_all_healthy(client, mock_fhir_metadata):
    """All three services (db, measure engine, CDR) report healthy."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["database"]["status"] == "connected"
    assert data["measure_engine"]["status"] == "connected"
    assert data["cdr"]["status"] == "connected"


async def test_health_database_unreachable(client, mock_fhir_metadata):
    """When the database query fails, status is degraded."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)

    with (
        patch("app.routes.health.httpx.AsyncClient") as mock_httpx,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Override the session execute to raise an exception
        from sqlalchemy.ext.asyncio import AsyncSession

        original_execute = AsyncSession.execute

        async def failing_execute(self, stmt, *args, **kwargs):
            # Only fail for the health check "SELECT 1" query
            stmt_str = str(stmt)
            if "SELECT 1" in stmt_str or "1" == str(getattr(stmt, "text", "")):
                raise ConnectionError("Database unreachable")
            return await original_execute(self, stmt, *args, **kwargs)

        with patch.object(AsyncSession, "execute", failing_execute):
            resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"]["status"] == "disconnected"
    assert "error" in data["database"]


async def test_health_measure_engine_unreachable(client, mock_fhir_metadata):
    """When the measure engine is down, status is degraded."""
    cdr_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is measure engine /metadata, second is CDR /metadata
        if call_count == 1:
            raise httpx.ConnectError("Connection refused")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["measure_engine"]["status"] == "disconnected"
    assert "error" in data["measure_engine"]


async def test_health_measure_engine_down_has_error_details(client, mock_fhir_metadata):
    """error_details is present and has a hint when the measure engine is unreachable."""
    cdr_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection refused")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    me = data["measure_engine"]
    assert me["status"] == "disconnected"
    assert "error_details" in me
    ed = me["error_details"]
    assert ed["operation"] == "health-check"
    assert ed["hint"] is not None
    assert ed["status_code"] is None  # network error, no HTTP status


async def test_health_cdr_http_error_has_error_details(client, mock_fhir_metadata):
    """error_details has status_code and hint when CDR returns non-200."""
    engine_response = httpx.Response(200, json=mock_fhir_metadata)
    cdr_401 = httpx.Response(401, json={"resourceType": "OperationOutcome", "issue": []})

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return engine_response
        return cdr_401

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    cdr = data["cdr"]
    assert cdr["status"] == "disconnected"
    assert "error_details" in cdr
    ed = cdr["error_details"]
    assert ed["status_code"] == 401
    assert ed["hint"] is not None
    assert "token" in ed["hint"].lower() or "authentication" in ed["hint"].lower()


async def test_health_cdr_unreachable(client, mock_fhir_metadata):
    """When the CDR is down, status is degraded."""
    engine_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is measure engine /metadata, second is CDR /metadata
        if call_count == 2:
            raise httpx.ConnectError("Connection refused")
        return engine_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["cdr"]["status"] == "disconnected"
    assert "error" in data["cdr"]
    assert data["measure_engine"]["status"] == "connected"


async def test_health_error_does_not_leak_internal_hostname(client, mock_fhir_metadata):
    """Regression: internal hostnames must not appear in HTTP response bodies.

    When the measure engine raises an exception whose message contains an
    internal Docker-network hostname (hapi-fhir-measure:8080), sanitize_error()
    must strip it before it reaches the client.
    """
    cdr_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection refused connecting to http://hapi-fhir-measure:8080/fhir/metadata")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.text
    assert "hapi-fhir-measure" not in body
    assert "8080" not in body
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["measure_engine"]["status"] == "disconnected"
    assert "error" in data["measure_engine"]


# ---------------------------------------------------------------------------
# measure_engine identity block (issue #396)
# ---------------------------------------------------------------------------


async def _activate_mcs(test_session, *, name: str, url: str, read_only: bool = False):
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name=name,
        mcs_url=url,
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        is_read_only=read_only,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


async def test_health_measure_engine_connected_includes_id(client, test_session, mock_fhir_metadata):
    """The connected branch carries the MCS id + read-only flag, not just the name."""
    cfg = await _activate_mcs(test_session, name="Attendee MCS", url="https://attendee-mcs.example.com/fhir")

    mock_response = httpx.Response(200, json=mock_fhir_metadata)
    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    me = resp.json()["measure_engine"]
    assert me["status"] == "connected"
    assert me["id"] == cfg.id
    assert me["name"] == "Attendee MCS"
    assert me["is_read_only"] is False


async def test_health_measure_engine_http_error_includes_id(client, test_session, mock_fhir_metadata):
    """The non-200 branch carries the id too, so the frontend can always key on it."""
    cfg = await _activate_mcs(test_session, name="Read Only MCS", url="https://ro-mcs.example.com/fhir", read_only=True)

    cdr_response = httpx.Response(200, json=mock_fhir_metadata)
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, json={"resourceType": "OperationOutcome", "issue": []})
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    me = resp.json()["measure_engine"]
    assert me["status"] == "disconnected"
    assert me["id"] == cfg.id
    assert me["is_read_only"] is True


async def test_health_measure_engine_exception_includes_id(client, test_session, mock_fhir_metadata):
    """The exception branch carries the id too."""
    cfg = await _activate_mcs(test_session, name="Attendee MCS", url="https://attendee-mcs.example.com/fhir")

    cdr_response = httpx.Response(200, json=mock_fhir_metadata)
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection refused")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    me = resp.json()["measure_engine"]
    assert me["status"] == "disconnected"
    assert me["id"] == cfg.id
    assert me["name"] == "Attendee MCS"


async def test_health_measure_engine_id_present_without_mcs_row(client, mock_fhir_metadata):
    """No MCS row → the fallback context still yields an id (0) and a writable flag."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)
    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    me = resp.json()["measure_engine"]
    assert me["id"] == 0
    assert me["is_read_only"] is False


# ---------------------------------------------------------------------------
# cdr identity block (issue #404)
#
# Mirrors the measure_engine block above. Without `cdr.id` no React effect can
# key on "the CDR changed", which is what leaves the Jobs patient-group
# dropdown showing the previous CDR's Groups.
# ---------------------------------------------------------------------------


async def _activate_cdr(test_session, *, name: str, url: str, read_only: bool = False):
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    await test_session.execute(sa_update(CDRConfig).values(is_active=False))
    cfg = CDRConfig(
        name=name,
        cdr_url=url,
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        is_read_only=read_only,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


async def test_health_cdr_connected_includes_id(client, test_session, mock_fhir_metadata):
    """The connected branch carries the CDR id, not just the name."""
    cfg = await _activate_cdr(test_session, name="Attendee CDR", url="https://attendee-cdr.example.com/fhir")

    mock_response = httpx.Response(200, json=mock_fhir_metadata)
    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    cdr = resp.json()["cdr"]
    assert cdr["status"] == "connected"
    assert cdr["id"] == cfg.id
    assert cdr["name"] == "Attendee CDR"
    assert cdr["is_read_only"] is False


async def test_health_cdr_http_error_includes_id(client, test_session, mock_fhir_metadata):
    """The non-200 branch carries the id too, so the frontend can always key on it."""
    cfg = await _activate_cdr(test_session, name="Read Only CDR", url="https://ro-cdr.example.com/fhir", read_only=True)

    me_response = httpx.Response(200, json=mock_fhir_metadata)
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is the measure engine, second is the CDR.
        if call_count == 1:
            return me_response
        return httpx.Response(503, json={"resourceType": "OperationOutcome", "issue": []})

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    cdr = resp.json()["cdr"]
    assert cdr["status"] == "disconnected"
    assert cdr["id"] == cfg.id
    assert cdr["is_read_only"] is True


async def test_health_cdr_exception_includes_id(client, test_session, mock_fhir_metadata):
    """The exception branch carries the id too."""
    cfg = await _activate_cdr(test_session, name="Attendee CDR", url="https://attendee-cdr.example.com/fhir")

    me_response = httpx.Response(200, json=mock_fhir_metadata)
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return me_response
        raise httpx.ConnectError("Connection refused")

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    cdr = resp.json()["cdr"]
    assert cdr["status"] == "disconnected"
    assert cdr["id"] == cfg.id
    assert cdr["name"] == "Attendee CDR"


async def test_health_cdr_id_present_without_cdr_row(client, mock_fhir_metadata):
    """No CDR row → the fallback context still yields an id (0) and a writable flag."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)
    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = await client.get("/health")

    cdr = resp.json()["cdr"]
    assert cdr["id"] == 0
    assert cdr["is_read_only"] is False
