"""Health check endpoint."""

import logging
import time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import ConnectionContext, get_active_cdr, get_active_mcs
from app.services.fhir_errors import HINT_BY_STATUS, hint_for_network_exception, sanitize_url
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


def _http_error_details(url: str, status_code: int, latency_ms: int) -> dict:
    return {
        "operation": "health-check",
        "url": sanitize_url(url),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "hint": HINT_BY_STATUS.get(status_code),
    }


def _network_error_details(url: str, exc: Exception, latency_ms: int) -> dict:
    return {
        "operation": "health-check",
        "url": sanitize_url(url),
        "status_code": None,
        "latency_ms": latency_ms,
        "hint": hint_for_network_exception(exc),
    }


@router.get("/health")
async def health_check(
    session: AsyncSession = Depends(get_session),
    cdr: ConnectionContext = Depends(get_active_cdr),
    mcs: ConnectionContext = Depends(get_active_mcs),
) -> dict:
    """Check connectivity to database, measure engine, and CDR."""
    status: dict = {
        "status": "healthy",
        "database": {"status": "unknown"},
        "measure_engine": {"status": "unknown"},
        "cdr": {"status": "unknown"},
    }

    # Database check
    try:
        await session.execute(text("SELECT 1"))
        status["database"] = {"status": "connected"}
    except Exception as exc:
        status["database"] = {
            "status": "disconnected",
            "error": sanitize_error(exc)[:200],
            "error_details": {
                "operation": "health-check",
                "url": None,
                "status_code": None,
                "latency_ms": None,
                "hint": (
                    "Cannot connect to the database. Check that it is running and the connection settings are correct."
                ),
            },
        }
        status["status"] = "degraded"

    # Measure engine check (active MCS connection)
    me_url = f"{mcs.mcs_url}/metadata"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(me_url)
            latency_ms = round((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                status["measure_engine"] = {
                    "status": "connected",
                    "id": mcs.id,
                    "name": mcs.name,
                    "is_read_only": mcs.is_read_only,
                }
            else:
                status["measure_engine"] = {
                    "status": "disconnected",
                    "id": mcs.id,
                    "name": mcs.name,
                    "is_read_only": mcs.is_read_only,
                    "error": f"HTTP {resp.status_code}",
                    "error_details": _http_error_details(me_url, resp.status_code, latency_ms),
                }
                status["status"] = "degraded"
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        status["measure_engine"] = {
            "status": "disconnected",
            "id": mcs.id,
            "name": mcs.name,
            "is_read_only": mcs.is_read_only,
            "error": sanitize_error(exc)[:200],
            "error_details": _network_error_details(me_url, exc, latency_ms),
        }
        status["status"] = "degraded"

    # CDR check
    cdr_url = f"{cdr.cdr_url}/metadata"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(cdr_url)
            latency_ms = round((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                status["cdr"] = {
                    "status": "connected",
                    "id": cdr.id,
                    "name": cdr.name,
                    "is_read_only": cdr.is_read_only,
                }
            else:
                status["cdr"] = {
                    "status": "disconnected",
                    "id": cdr.id,
                    "name": cdr.name,
                    "is_read_only": cdr.is_read_only,
                    "error": f"HTTP {resp.status_code}",
                    "error_details": _http_error_details(cdr_url, resp.status_code, latency_ms),
                }
                status["status"] = "degraded"
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        status["cdr"] = {
            "status": "disconnected",
            "id": cdr.id,
            "name": cdr.name,
            "is_read_only": cdr.is_read_only,
            "error": sanitize_error(exc)[:200],
            "error_details": _network_error_details(cdr_url, exc, latency_ms),
        }
        status["status"] = "degraded"

    return status
