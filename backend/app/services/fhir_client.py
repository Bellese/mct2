"""FHIR client with pluggable data acquisition strategy.

Handles all HTTP communication with HAPI FHIR servers (CDR and measure engine).
"""

from __future__ import annotations

import abc
import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.config import AuthType
from app.services.fhir_errors import (
    FhirIssue,
    FhirOperationError,
    FhirOperationOutcome,
    sanitize_url,
)

logger = logging.getLogger(__name__)

# Consecutive transport failures that abort a wipe. Shared by `wipe_patient_data`
# and `wipe_patients_by_id`: a timed-out DELETE leaves HAPI's server-side
# operation running, and pushing new data over it lets the in-flight DELETE wipe
# the freshly-pushed resources.
_MAX_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class McsTarget:
    """Which measure-calculation server a pipeline is working against (issue #397).

    Carries RESOLVED auth headers rather than raw credentials so no helper deep in
    the validation pipeline re-derives them, and carries the two connection flags so
    no helper re-queries the database.

    Frozen because it is threaded through roughly sixteen call sites: a helper
    mutating a shared target would silently re-point every call after it.

    Every field is required. The absence of a default is the point — the #397 bug
    class is precisely a target that defaults to something the caller did not mean.
    """

    url: str
    auth_headers: dict[str, str]
    is_read_only: bool
    wipe_before_job: bool


@dataclass
class FailedResourceFetch:
    resource_type: str
    error: str


@dataclass
class GatherResult:
    """Outcome of a per-patient data gather from the CDR."""

    resources: list[dict] = field(default_factory=list)
    failed_types: list[FailedResourceFetch] = field(default_factory=list)

    @property
    def has_partial_failure(self) -> bool:
        return bool(self.failed_types)


@dataclass
class BundleEntryResult:
    resource_type: str
    resource_id: str | None
    status: str
    outcome: FhirOperationOutcome | None


@dataclass
class BundleUploadResult:
    """Per-entry outcome from a HAPI batch Bundle upload."""

    succeeded: list[BundleEntryResult] = field(default_factory=list)
    failed: list[BundleEntryResult] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return bool(self.failed)


# Hosts explicitly allowed for local dev even though they're loopback/private.
# Includes Docker service hostnames parsed from the configured default CDR and
# MCS URLs so the seeded "Local CDR" / "Local Measure Engine" connections pass
# the http+non-localhost guard. Set is built once at import time — settings are
# immutable for the process lifetime.
def _seeded_internal_hosts() -> set[str]:
    hosts: set[str] = set()
    for url in (settings.DEFAULT_CDR_URL, settings.MEASURE_ENGINE_URL):
        host = urlparse(url or "").hostname
        if host:
            hosts.add(host)
    return hosts


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"} | _seeded_internal_hosts()


def _is_blocked_ip(host: str) -> bool:
    """Return True if host is a private/reserved IP (RFC-1918, loopback, link-local, ULA).

    Covers IPv4 (including 169.254.0.0/16 AWS IMDS) and IPv6 private ranges.
    Returns False for hostnames that aren't raw IP literals.
    """
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False  # Not a raw IP literal — hostname, can't resolve statically


def _validate_ssrf_url(url: str, label: str = "URL") -> None:
    """Reject URLs that could be used for SSRF attacks.

    Rules:
    - Only https is allowed unless the host is in `_LOCAL_HOSTS` — that's
      localhost/127.0.0.1/::1 plus the Docker service hostnames parsed from
      `settings.DEFAULT_CDR_URL` and `settings.MEASURE_ENGINE_URL` (so the
      seeded local CDR/MCS connections work without TLS).
    - Raw IP literals that are private, loopback, or link-local are rejected unless
      the host is in the local dev allowlist.  This covers RFC-1918, 169.254.0.0/16
      (AWS IMDS), IPv6 loopback, link-local, and ULA ranges.

    Raises ValueError with a descriptive message on rejection.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""

    is_local = host in _LOCAL_HOSTS

    if scheme not in ("http", "https"):
        raise ValueError(
            f"SSRF protection: {label} scheme '{scheme}' is not allowed. "
            "Only https (or http for localhost) is permitted."
        )

    if scheme == "http" and not is_local:
        raise ValueError(f"SSRF protection: {label} must use https for non-localhost hosts (got http://{host}).")

    if not is_local and _is_blocked_ip(host):
        raise ValueError(
            f"SSRF protection: {label} resolves to a private/reserved address "
            f"({host}). Use a publicly routable host or localhost."
        )


def _same_origin(base_url: str, next_url: str) -> bool:
    """Return True if next_url shares the same scheme, host, and port as base_url.

    Pagination next links must always point back to the same CDR server.
    A malicious CDR returning a next link to a different host (e.g. an internal
    Docker service) is the SSRF vector this check blocks.
    """
    a = urlparse(base_url)
    b = urlparse(next_url)
    return a.scheme == b.scheme and a.hostname == b.hostname and a.port == b.port


async def _build_auth_headers(auth_type: str, auth_credentials: Optional[dict]) -> dict[str, str]:
    """Build HTTP auth headers from CDR config. Async to support SMART token acquisition."""
    if auth_type == AuthType.none or not auth_credentials:
        return {}
    if auth_type == AuthType.basic:
        import base64

        username = auth_credentials.get("username", "")
        password = auth_credentials.get("password", "")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    if auth_type == AuthType.bearer:
        token = auth_credentials.get("token", "")
        return {"Authorization": f"Bearer {token}"}
    if auth_type == AuthType.smart:
        token = await _acquire_smart_token(auth_credentials)
        return {"Authorization": f"Bearer {token}"}
    return {}


async def _acquire_smart_token(credentials: dict) -> str:
    """Exchange client_credentials grant for a bearer token at token_endpoint.

    No token caching — fresh token per request is intentional for the connectathon.
    Add TTL-based caching post-connectathon if token endpoint rate-limiting becomes an issue.
    """
    required = {"token_endpoint", "client_id", "client_secret"}
    missing = required - credentials.keys()
    if missing:
        raise ValueError(f"SMART credentials missing required fields: {', '.join(sorted(missing))}")

    _validate_ssrf_url(credentials["token_endpoint"], label="token_endpoint")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            credentials["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
            },
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("SMART token endpoint response missing 'access_token'")
        return token


# ---------------------------------------------------------------------------
# Data Acquisition Strategy (ABC + BatchQuery implementation)
# ---------------------------------------------------------------------------


class DataAcquisitionStrategy(abc.ABC):
    """Abstract base class for patient data acquisition from a CDR."""

    @abc.abstractmethod
    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Return a list of Patient resources from the CDR.

        Each item is a FHIR Patient resource dict with at least 'id'.
        """
        ...

    @abc.abstractmethod
    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> GatherResult:
        """Return all clinical resources for a single patient.

        Returns a GatherResult with resources and any partial-failure metadata.
        """
        ...


class BatchQueryStrategy(DataAcquisitionStrategy):
    """Fetch patients using paginated FHIR REST queries."""

    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all Patient resources from the CDR, following pagination."""
        patients: list[dict[str, Any]] = []
        url: Optional[str] = f"{cdr_url}/Patient?_count=100"
        async with httpx.AsyncClient(timeout=60.0) as client:
            while url:
                logger.info("Fetching patients", extra={"url": sanitize_url(url)})
                resp = await client.get(url, headers=auth_headers)
                resp.raise_for_status()
                bundle = resp.json()
                for entry in bundle.get("entry", []):
                    resource = entry.get("resource", {})
                    if resource.get("resourceType") == "Patient":
                        patients.append(resource)
                # Follow next link only when it stays on the same CDR origin.
                # A malicious CDR returning a next link to a different host is the
                # SSRF vector we are blocking here.
                url = None
                for link in bundle.get("link", []):
                    if link.get("relation") == "next":
                        next_url = link.get("url")
                        if next_url and _same_origin(cdr_url, next_url):
                            url = next_url
                        elif next_url:
                            logger.warning(
                                "SSRF: pagination next link rejected (origin mismatch)",
                                extra={"url": sanitize_url(next_url)},
                            )
                        break
        logger.info("Gathered patients", extra={"count": len(patients)})
        return patients

    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> GatherResult:
        """Fetch all resources for a patient using $everything."""
        resources: list[dict[str, Any]] = []
        url: Optional[str] = f"{cdr_url}/Patient/{patient_id}/$everything?_count=200"
        async with httpx.AsyncClient(timeout=120.0) as client:
            while url:
                logger.info(
                    "Fetching patient data",
                    extra={"patient_id": patient_id, "url": sanitize_url(url)},
                )
                resp = await client.get(url, headers=auth_headers)
                resp.raise_for_status()
                bundle = resp.json()
                # Group: population container that references other patients — not
                # a clinical resource for this patient.
                # MeasureReport: test-case expected-result artifact, not clinical data.
                _SKIP_TYPES = {"Group", "MeasureReport"}
                for entry in bundle.get("entry", []):
                    resource = entry.get("resource")
                    if resource and resource.get("resourceType") not in _SKIP_TYPES:
                        resources.append(resource)
                url = None
                for link in bundle.get("link", []):
                    if link.get("relation") == "next":
                        next_url = link.get("url")
                        if next_url and _same_origin(cdr_url, next_url):
                            url = next_url
                        elif next_url:
                            logger.warning(
                                "SSRF: pagination next link rejected (origin mismatch)",
                                extra={"url": sanitize_url(next_url)},
                            )
                        break
        logger.info(
            "Gathered patient data",
            extra={"patient_id": patient_id, "resource_count": len(resources)},
        )
        return GatherResult(resources=resources)


class DataRequirementsStrategy(DataAcquisitionStrategy):
    """DEQM spec-compliant data acquisition using $data-requirements.

    Calls GET /Measure/{id}/$data-requirements on the measure engine,
    translates each dataRequirement entry into a CDR REST query, and
    collects only the resources the measure actually needs.

    Falls back to BatchQueryStrategy ($everything) if $data-requirements
    returns an empty list or raises any exception.
    """

    def __init__(
        self,
        measure_id: str,
        mcs_url: str,
        mcs_auth_headers: dict[str, str] | None = None,
    ) -> None:
        """`mcs_url` is REQUIRED — it names the engine to ask for data requirements.

        It previously read `settings.MEASURE_ENGINE_URL`, so a job against a remote
        MCS asked the LOCAL engine what data its measure needs (issue #397). The
        answer was either wrong or a 401, and the strategy then quietly fell back to
        `$everything` — a silent downgrade with no error surfaced.
        """
        self._measure_id = measure_id
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers or {}
        self._fallback = BatchQueryStrategy()
        # One strategy instance per job, so this memoises $data-requirements for
        # the whole run. The answer depends only on the measure, but the call
        # compiles the measure's CQL in the engine's R4DataRequirementsService --
        # calling it per patient drove the engine's heap past its container limit
        # and got it OOM-killed mid-job at 319 patients.
        self._requirements: list[dict[str, Any]] | None = None
        self._requirements_lock = asyncio.Lock()

    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Delegate patient listing to BatchQueryStrategy (CDR search is the same)."""
        return await self._fallback.gather_patients(cdr_url, auth_headers)

    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> GatherResult:
        """Fetch only the resources the measure needs, using $data-requirements."""
        try:
            requirements = await self._get_data_requirements()
        except Exception as exc:
            logger.warning(
                "$data-requirements failed, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id, "error": str(exc)},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

        if not requirements:
            logger.info(
                "$data-requirements returned no entries, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

        try:
            return await self._fetch_by_requirements(cdr_url, patient_id, auth_headers, requirements)
        except RuntimeError as exc:
            logger.warning(
                "All CDR resource-type fetches failed, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id, "error": str(exc)},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

    async def _get_data_requirements(self) -> list[dict[str, Any]]:
        """Call $data-requirements on MCS once per job and cache the entries.

        Patients are gathered concurrently, so the lock keeps the first wave of
        callers from each issuing the same expensive call; the double check means
        every later patient is served from memory. Failures are not cached — the
        caller falls back to `$everything`, and a transient error should not pin
        the whole job to the fallback path.
        """
        if self._requirements is not None:
            return self._requirements

        async with self._requirements_lock:
            if self._requirements is not None:
                return self._requirements

            url = f"{self._mcs_url}/Measure/{self._measure_id}/$data-requirements"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._mcs_auth_headers)
                resp.raise_for_status()
                library = resp.json()
                self._requirements = library.get("dataRequirement", [])
                return self._requirements

    async def _fetch_by_requirements(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
        requirements: list[dict[str, Any]],
    ) -> GatherResult:
        """Translate dataRequirement entries to CDR REST queries and collect resources.

        Requirements are grouped by resource type first. A type with exactly one
        distinct valueset across all its requirements is queried with a
        `code:in=` filter, same as before. A type with MULTIPLE distinct
        valuesets — or a mix of filtered and unfiltered requirements — drops
        the code filter entirely and fetches all resources of that type for
        the patient: `code:in=vs1&code:in=vs2` is an AND in FHIR search, so
        unioning as repeated params would narrow the result rather than widen
        it. Over-fetching is safe for measure evaluation (CQL filters again
        anyway); under-fetching silently changes populations with no error
        surfaced.

        Patient is always fetched by direct read, whether or not it appears in
        the dataRequirement list — `$everything` always includes it, and its
        absence fails evaluation regardless.

        Per-type failures are captured in GatherResult.failed_types.
        Raises RuntimeError only when ALL types fail (triggers outer $everything fallback).
        """
        resources: list[dict[str, Any]] = []
        failed_types: list[FailedResourceFetch] = []
        failed_type_names: set[str] = set()

        # Group requirements by resource type, preserving first-seen order,
        # and collect each requirement's valueset (None when unfiltered).
        types_in_order: list[str] = []
        valuesets_by_type: dict[str, list[str | None]] = {}
        for req in requirements:
            resource_type = req.get("type", "")
            if not resource_type or not re.match(r"^[A-Za-z][A-Za-z0-9]{0,127}$", resource_type):
                continue
            if resource_type not in valuesets_by_type:
                valuesets_by_type[resource_type] = []
                types_in_order.append(resource_type)
            vs: str | None = None
            for cf in req.get("codeFilter", []):
                candidate = cf.get("valueSet")
                if candidate:
                    vs = candidate
                    break
            valuesets_by_type[resource_type].append(vs)

        # `required_types` drives the "did everything we actually needed fail"
        # check below. Patient is fetched unconditionally as a safety net (see
        # docstring) but is not itself a declared data requirement, so a
        # Patient-only failure must not, by itself, count as "all required
        # types failed" and must not prevent that check from firing when the
        # declared types genuinely all fail.
        required_types: set[str] = set(types_in_order)
        if "Patient" not in required_types:
            types_in_order.append("Patient")

        async with httpx.AsyncClient(timeout=60.0) as client:
            for resource_type in types_in_order:
                try:
                    if resource_type == "Patient":
                        resp = await client.get(f"{cdr_url}/Patient/{patient_id}", headers=auth_headers)
                        if resp.status_code == 200:
                            resources.append(resp.json())
                        else:
                            # Recorded in `failed_types` only (drives has_partial_failure /
                            # partial-gather reporting) — deliberately NOT added to
                            # `failed_type_names`, which drives the "all REQUIRED types
                            # failed" check below. That check must fire only on declared
                            # dataRequirement types genuinely all failing; a forced
                            # Patient-read failure must never trip it, since Patient is
                            # always fetched via this special-cased direct read (never
                            # the generic per-type loop) regardless of whether it also
                            # happens to appear in the dataRequirement list.
                            failed_types.append(
                                FailedResourceFetch(
                                    resource_type=resource_type,
                                    error=f"CDR returned {resp.status_code} for Patient/{patient_id}",
                                )
                            )
                    else:
                        # Only keep a code:in= filter when every requirement for
                        # this type agrees on a single valueset. Otherwise
                        # over-fetch the whole type — see docstring.
                        type_valuesets = valuesets_by_type.get(resource_type, [])
                        distinct_valuesets = {vs for vs in type_valuesets if vs}
                        has_unfiltered = any(vs is None for vs in type_valuesets)
                        base_params = f"subject=Patient/{patient_id}&_count=100"

                        # Try the narrow query first, then the same query without
                        # the code filter. A `code:in=` against a ValueSet the CDR
                        # cannot resolve fails the search outright (HAPI-2788
                        # "Unknown ValueSet" for VSAC canonicals that were never
                        # loaded), and treating that as "this patient has no
                        # resources of this type" silently changes populations:
                        # a hospice ServiceRequest dropped this way cost nine
                        # patients their CMS130 denominator-exclusion while the
                        # job still reported success. Over-fetching is safe —
                        # CQL filters again anyway.
                        attempts = []
                        if len(distinct_valuesets) == 1 and not has_unfiltered:
                            (single_vs,) = distinct_valuesets
                            attempts.append(f"{base_params}&code:in={single_vs}")
                        attempts.append(base_params)

                        for attempt_index, params in enumerate(attempts):
                            is_last_attempt = attempt_index == len(attempts) - 1
                            # Stage into a local list so a failure part-way through
                            # pagination cannot leave half a page in `resources`
                            # and then duplicate it on the retry.
                            collected: list[dict[str, Any]] = []
                            try:
                                page_url: Optional[str] = f"{cdr_url}/{resource_type}?{params}"
                                while page_url:
                                    resp = await client.get(page_url, headers=auth_headers)
                                    if resp.status_code != 200:
                                        raise httpx.HTTPStatusError(
                                            f"CDR returned {resp.status_code} for {resource_type}",
                                            request=resp.request,
                                            response=resp,
                                        )
                                    bundle = resp.json()
                                    for entry in bundle.get("entry", []):
                                        resource = entry.get("resource")
                                        if resource:
                                            collected.append(resource)
                                    page_url = None
                                    for link in bundle.get("link", []):
                                        if link.get("relation") == "next":
                                            next_url = link.get("url")
                                            if next_url and _same_origin(cdr_url, next_url):
                                                page_url = next_url
                                            elif next_url:
                                                logger.warning(
                                                    "SSRF: pagination next link rejected (origin mismatch)",
                                                    extra={"url": sanitize_url(next_url)},
                                                )
                                            break
                            except Exception as exc:
                                if is_last_attempt:
                                    raise
                                logger.warning(
                                    "Filtered CDR query failed for %s, retrying without the code filter — %s",
                                    resource_type,
                                    str(exc),
                                    extra={
                                        "resource_type": resource_type,
                                        "patient_id": patient_id,
                                        "error": str(exc),
                                    },
                                )
                                continue
                            resources.extend(collected)
                            break
                except Exception as exc:
                    failed_type_names.add(resource_type)
                    failed_types.append(FailedResourceFetch(resource_type=resource_type, error=str(exc)))
                    logger.warning(
                        "CDR fetch failed for resource type %s, skipping — %s",
                        resource_type,
                        str(exc),
                        extra={"resource_type": resource_type, "patient_id": patient_id, "error": str(exc)},
                    )

        # Only propagate failure (triggering outer $everything fallback) when all
        # REQUIRED types fail — a forced Patient-fetch failure alone doesn't count.
        if required_types and (failed_type_names & required_types) == required_types:
            raise RuntimeError(f"All resource types failed CDR fetch: {sorted(failed_type_names)}")

        if failed_types:
            logger.warning(
                "Partial CDR fetch — some resource types failed",
                extra={
                    "measure_id": self._measure_id,
                    "patient_id": patient_id,
                    "failed_types": [f.resource_type for f in failed_types],
                },
            )
        else:
            logger.info(
                "Fetched patient data via $data-requirements",
                extra={
                    "measure_id": self._measure_id,
                    "patient_id": patient_id,
                    "resource_count": len(resources),
                    "requirement_types": list(required_types),
                },
            )
        return GatherResult(resources=resources, failed_types=failed_types)


# ---------------------------------------------------------------------------
# Direct FHIR helper functions (measure engine interaction)
# ---------------------------------------------------------------------------

_VALUESET_EXPANSION_POLL_INTERVAL = 10


def wait_for_valueset_expansion(base_url: str, valueset_urls: list[str], timeout_s: int = 600) -> dict[str, int]:
    """Wait until HAPI can serve $expand for each ValueSet URL and return expansion totals."""
    expanded: dict[str, int] = {}
    if not valueset_urls:
        return expanded

    unique_urls = sorted({url for url in valueset_urls if url})
    pending: dict[str, str] = {}
    with httpx.Client(timeout=30.0) as client:
        for valueset_url in unique_urls:
            try:
                lookup = client.get(
                    f"{base_url}/ValueSet",
                    params={"url": valueset_url, "_count": 1, "_elements": "id,url"},
                    headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json"},
                )
                lookup.raise_for_status()
                entries = lookup.json().get("entry", [])
                if not entries:
                    logger.warning(
                        "ValueSet not found for expansion wait", extra={"valueset_url": sanitize_url(valueset_url)}
                    )
                    continue
                valueset_id = entries[0].get("resource", {}).get("id")
                if not valueset_id:
                    logger.warning("ValueSet lookup returned no id", extra={"valueset_url": sanitize_url(valueset_url)})
                    continue
                pending[valueset_url] = valueset_id
            except Exception as exc:
                logger.warning(
                    "ValueSet lookup failed before expansion wait",
                    extra={"valueset_url": sanitize_url(valueset_url), "error": str(exc)},
                )

        deadline = time.monotonic() + timeout_s
        polls = 0
        while pending and time.monotonic() < deadline:
            polls += 1
            newly_done: set[str] = set()
            for valueset_url, valueset_id in list(pending.items()):
                try:
                    resp = client.post(f"{base_url}/ValueSet/{valueset_id}/$expand?count=2", timeout=15.0)
                    if resp.status_code == 200:
                        body = resp.json()
                        expanded[valueset_url] = int(
                            body.get("expansion", {}).get("total", len(body.get("expansion", {}).get("contains", [])))
                        )
                        newly_done.add(valueset_url)
                except Exception:
                    pass
            for valueset_url in newly_done:
                pending.pop(valueset_url, None)
            if pending:
                time.sleep(_VALUESET_EXPANSION_POLL_INTERVAL)

        for valueset_url, valueset_id in pending.items():
            logger.warning(
                "ValueSet expansion timed out",
                extra={"valueset_url": sanitize_url(valueset_url), "valueset_id": valueset_id, "polls": polls},
            )

    return expanded


def _normalize_measure_def(r: dict[str, Any]) -> dict[str, Any]:
    """Ensure Library resources have a url so HAPI 8.x can resolve canonical refs.

    DBCG FHIR4 bundles omit Library.url; HAPI resolves Measure.library references
    by canonical URL search, so we backfill url = "Library/{id}" when absent.
    """
    if r.get("resourceType") == "Library" and not r.get("url") and r.get("id"):
        r = {**r, "url": f"Library/{r['id']}"}
    return r


def _chunk_request_entries(
    entries: list[dict[str, Any]],
    max_size: int | None,
) -> list[list[dict[str, Any]]]:
    """Partition the Patients-first ordered request entries into chunks of
    at most `max_size`. Returns a single chunk when `max_size` is None or ≤ 0.

    Callers MUST sort the entry list Patients-first BEFORE invoking this
    helper. Partitioning preserves the input order, which preserves the
    invariant: every Patient referenced by a non-Patient entry has already
    been POSTed (in an earlier chunk or earlier in the same chunk) before
    HAPI tries to index the reference. See issue #177 + test_push_resources_
    sorts_patients_first.
    """
    if not max_size or max_size <= 0:
        return [entries]
    return [entries[i : i + max_size] for i in range(0, len(entries), max_size)]


def _parse_bundle_upload_result(
    response_bundle: dict[str, Any],
    request_entries: list[dict[str, Any]],
) -> BundleUploadResult:
    """Parse a HAPI batch-response Bundle into succeeded/failed entry lists."""
    succeeded: list[BundleEntryResult] = []
    failed: list[BundleEntryResult] = []
    response_entries = response_bundle.get("entry", []) or []
    for i, resp_entry in enumerate(response_entries):
        req_resource = request_entries[i]["resource"] if i < len(request_entries) else {}
        resource_type = req_resource.get("resourceType", "Unknown")
        resource_id = req_resource.get("id")
        resp_status = (resp_entry.get("response") or {}).get("status", "")
        outcome_body = (resp_entry.get("response") or {}).get("outcome")
        outcome = FhirOperationOutcome.from_dict(outcome_body) if isinstance(outcome_body, dict) else None
        entry_result = BundleEntryResult(
            resource_type=resource_type,
            resource_id=resource_id,
            status=resp_status,
            outcome=outcome,
        )
        # Status starts with "2" = success; anything else = failure
        if resp_status.startswith("2"):
            succeeded.append(entry_result)
        else:
            failed.append(entry_result)
    return BundleUploadResult(succeeded=succeeded, failed=failed)


async def push_resources(
    resources: list[dict[str, Any]],
    target_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
    max_bundle_entries: int | None = None,
) -> BundleUploadResult:
    """POST resources to the target FHIR server, optionally chunked.

    Uses batch (not transaction) so HAPI does not validate cross-references
    between entries — avoids HAPI-2001 when clinical subjects are absent from
    the measure engine.

    Patient resources are placed first across the entire push. When
    `max_bundle_entries` is set, the entry list is partitioned into chunks
    of at most that size and each chunk is POSTed sequentially. Patients-
    first ordering is preserved across chunk boundaries (issue #177).

    Per-chunk outcomes are aggregated into a single BundleUploadResult.
    Partial failure (some chunks 200, some 400) does NOT raise — failed-chunk
    entries are recorded in `result.failed`. Total wipeout (every chunk
    rejected) raises FhirOperationError with the first failure's outcome,
    preserving the existing single-shot semantics for callers.
    """
    base = target_url or settings.MEASURE_ENGINE_URL
    valid = [r for r in resources if "resourceType" in r and "id" in r]
    ordered = [r for r in valid if r.get("resourceType") == "Patient"] + [
        r for r in valid if r.get("resourceType") != "Patient"
    ]
    request_entries = [
        {
            "resource": _normalize_measure_def(r),
            "request": {
                "method": "PUT",
                "url": f"{r['resourceType']}/{r['id']}",
            },
        }
        for r in ordered
    ]
    if not request_entries:
        logger.warning("No valid resources to push")
        return BundleUploadResult()

    chunks = _chunk_request_entries(request_entries, max_bundle_entries)
    headers = {"Content-Type": "application/fhir+json", **(auth_headers or {})}

    aggregated = BundleUploadResult()
    first_error: FhirOperationError | None = None
    total_succeeded = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for chunk_idx, chunk in enumerate(chunks):
            bundle = {"resourceType": "Bundle", "type": "batch", "entry": chunk}
            start_ms = int(time.monotonic() * 1000)
            resp = await client.post(base, json=bundle, headers=headers)
            latency_ms = int(time.monotonic() * 1000) - start_ms

            # HTTP error → record all entries in this chunk as failed; keep
            # going so prior successes are not lost.
            if resp.status_code >= 400:
                outcome = FhirOperationOutcome.from_response(resp)
                exc = FhirOperationError(
                    operation="push-resources",
                    url=base,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
                if first_error is None:
                    first_error = exc
                for entry in chunk:
                    aggregated.failed.append(
                        BundleEntryResult(
                            resource_type=entry["resource"].get("resourceType", "Unknown"),
                            resource_id=entry["resource"].get("id"),
                            status=f"{resp.status_code}",
                            outcome=outcome,
                        )
                    )
                logger.warning(
                    "Chunked push: chunk failed",
                    extra={
                        "chunk_index": chunk_idx,
                        "total_chunks": len(chunks),
                        "chunk_size": len(chunk),
                        "status_code": resp.status_code,
                        "target": base,
                    },
                )
                continue

            # 200 OK but body is an OperationOutcome — entire-chunk rejection
            # on the success path.
            try:
                body = resp.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
                outcome = FhirOperationOutcome.from_dict(body)
                exc = FhirOperationError(
                    operation="push-resources",
                    url=base,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
                if first_error is None:
                    first_error = exc
                for entry in chunk:
                    aggregated.failed.append(
                        BundleEntryResult(
                            resource_type=entry["resource"].get("resourceType", "Unknown"),
                            resource_id=entry["resource"].get("id"),
                            status="OperationOutcome",
                            outcome=outcome,
                        )
                    )
                logger.warning(
                    "Chunked push: chunk returned OperationOutcome",
                    extra={
                        "chunk_index": chunk_idx,
                        "total_chunks": len(chunks),
                        "chunk_size": len(chunk),
                        "status_code": resp.status_code,
                        "target": base,
                    },
                )
                continue

            chunk_result = _parse_bundle_upload_result(body, chunk)
            aggregated.succeeded.extend(chunk_result.succeeded)
            aggregated.failed.extend(chunk_result.failed)
            total_succeeded += len(chunk_result.succeeded)

    # Total wipeout — no chunk produced a single success. Raise so the caller
    # (validation worker) marks the upload as `failed`, preserving the
    # pre-chunking contract.
    if total_succeeded == 0 and first_error is not None:
        raise first_error

    logger.info(
        "Pushed resources",
        extra={
            "count": len(request_entries),
            "chunks": len(chunks),
            "succeeded": len(aggregated.succeeded),
            "failed": len(aggregated.failed),
            "target": base,
        },
    )
    return aggregated


async def evaluate_measure(
    measure_id: str,
    patient_id: str,
    period_start: str,
    period_end: str,
    measure_engine_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call $evaluate-measure on the measure engine for a single patient.

    Args:
        measure_engine_url: Base URL of the MCS to call. Defaults to
            `settings.MEASURE_ENGINE_URL` for back-compat with callers that
            haven't yet been wired to per-job active-MCS context. The
            orchestrator passes `job.mcs_url` so jobs run against the MCS
            that was active at job creation time, not whatever's active now.
        auth_headers: Credentials for the MCS, resolved from the job's linked
            MCSConfig. Omitted for unauthenticated engines (local HAPI). A
            remote MCS rejects every evaluation with 401 without these.

    Raises FhirOperationError (with the MCS OperationOutcome preserved) on
    4xx/5xx responses and on 200 OK where the body is an OperationOutcome
    instead of a MeasureReport.
    """
    base_url = measure_engine_url or settings.MEASURE_ENGINE_URL
    url = (
        f"{base_url}/Measure/{measure_id}"
        f"/$evaluate-measure"
        f"?periodStart={period_start}"
        f"&periodEnd={period_end}"
        f"&subject=Patient/{patient_id}"
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(3):
            logger.info(
                "Evaluating measure",
                extra={"measure_id": measure_id, "patient_id": patient_id, "attempt": attempt + 1},
            )
            start_ms = int(time.monotonic() * 1000)
            resp = await client.get(url, headers=auth_headers or {})
            latency_ms = int(time.monotonic() * 1000) - start_ms
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code >= 500 and attempt < 2:
                    logger.warning(
                        "Transient measure evaluation failure — retrying",
                        extra={
                            "measure_id": measure_id,
                            "patient_id": patient_id,
                            "status_code": status_code,
                            "attempt": attempt + 1,
                        },
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                outcome = FhirOperationOutcome.from_response(resp)
                raise FhirOperationError(
                    operation="evaluate-measure",
                    url=url,
                    status_code=status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    cause=exc,
                ) from exc

            body = resp.json()
            # 200 OK but MCS returned OperationOutcome instead of MeasureReport
            if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
                outcome = FhirOperationOutcome.from_dict(body)
                raise FhirOperationError(
                    operation="evaluate-measure",
                    url=url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
            # 200 OK with MeasureReport.status == "error" — HAPI evaluated but hit a
            # fatal error (e.g. Unknown ValueSet). Extract the contained OperationOutcome
            # so the error surfaces as error_phase="evaluate" instead of silent all-false
            # populations.
            if isinstance(body, dict) and body.get("resourceType") == "MeasureReport" and body.get("status") == "error":
                contained_oo = next(
                    (c for c in body.get("contained", []) if c.get("resourceType") == "OperationOutcome"),
                    None,
                )
                outcome = FhirOperationOutcome.from_dict(contained_oo) if contained_oo else None
                raise FhirOperationError(
                    operation="evaluate-measure",
                    url=url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
            return body

    raise RuntimeError("Measure evaluation failed without a response")


# --- DEQM $submit-data support (spec: 2026-08-21-deqm-submit-data-workflow) ---

SUBMIT_DATA_MODE_STU5 = "stu5"
SUBMIT_DATA_MODE_BASE = "base-fallback"

_DEQM_SUBMIT_DATA_CANONICAL = "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data"
_DEQM_SUBMIT_DATA_OP_NAME = "deqm-submit-data"


async def get_measure_canonical(
    measure_id: str,
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
) -> str:
    """Read Measure/{id} off the MCS and return its canonical `url|version`.

    The DEQM Data Exchange MeasureReport's `measure` element must carry the
    measure's canonical URL, which only the MCS knows. Raises
    FhirOperationError when the Measure can't be read — the job should fail
    fast rather than submit reports pointing at nothing. A Measure without a
    `url` (unusual but legal) degrades to the relative reference.
    """
    url = f"{mcs_url}/Measure/{measure_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        start_ms = int(time.monotonic() * 1000)
        resp = await client.get(url, headers=auth_headers or {})
        latency_ms = int(time.monotonic() * 1000) - start_ms
        if resp.status_code != 200:
            raise FhirOperationError(
                operation="read-measure",
                url=url,
                status_code=resp.status_code,
                outcome=FhirOperationOutcome.from_response(resp),
                latency_ms=latency_ms,
            )
        measure = resp.json()
    canonical = measure.get("url")
    if not canonical:
        return f"Measure/{measure_id}"
    version = measure.get("version")
    return f"{canonical}|{version}" if version else canonical


async def detect_submit_data_mode(
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> str:
    """Probe the MCS CapabilityStatement for DEQM STU5 $deqm-submit-data.

    Returns SUBMIT_DATA_MODE_STU5 when the Measure resource advertises an
    operation named `deqm-submit-data` or defined by the DEQM canonical.
    Everything else — including an unreachable/unparseable /metadata — is
    SUBMIT_DATA_MODE_BASE. Never raises: the probe decides the envelope,
    it must not block job creation (the measure pre-flight already proved
    the MCS reachable).
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{mcs_url}/metadata", headers=auth_headers or {})
            resp.raise_for_status()
            capability = resp.json()
        for rest in capability.get("rest", []):
            operations = list(rest.get("operation", []))
            for res in rest.get("resource", []):
                if res.get("type") == "Measure":
                    operations.extend(res.get("operation", []))
            for op in operations:
                if op.get("name") == _DEQM_SUBMIT_DATA_OP_NAME:
                    return SUBMIT_DATA_MODE_STU5
                definition = str(op.get("definition", ""))
                # Exact match, or the canonical with a `|version` suffix — NOT any
                # prefix. A loose startswith() let a server that merely cites the
                # DEQM canonical (but implements only the base wire shape) get
                # classified stu5, which then 400s on every $deqm-submit-data POST
                # with no downgrade (see DeqmSubmitDataWorkflow.transfer_patient).
                if definition == _DEQM_SUBMIT_DATA_CANONICAL or definition.startswith(
                    f"{_DEQM_SUBMIT_DATA_CANONICAL}|"
                ):
                    return SUBMIT_DATA_MODE_STU5
    except Exception as exc:
        # Deferred import: app.services.validation imports from this module at
        # module load time, so a top-level import here would be circular.
        from app.services.validation import sanitize_error

        logger.warning(
            "CapabilityStatement probe for $deqm-submit-data failed — assuming base $submit-data",
            extra={"mcs_url": sanitize_url(mcs_url), "error": sanitize_error(exc)},
        )
    return SUBMIT_DATA_MODE_BASE


async def submit_data(
    *,
    mcs_url: str,
    parameters: dict[str, Any],
    mode: str,
    measure_id: str,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """POST a $submit-data Parameters payload to the MCS.

    The two modes deliberately use DIFFERENT URL shapes — do not "simplify"
    them into one:

    - STU5 mode POSTs to the type-level `Measure/$deqm-submit-data`. STU5's
      OperationDefinition formally declares this operation type-level
      (`instance: false`), so a spec-compliant STU5 server only implements it
      there.
    - Base-fallback mode POSTs to the instance-level
      `Measure/{measure_id}/$submit-data`. This is empirical, not spec-driven:
      probed against a local prebaked HAPI measure server, the type-level
      `POST Measure/$submit-data` returned 400 not-supported ("does not know
      how to handle POST operation[Measure/$submit-data]"), while the
      instance-level `POST Measure/{id}/$submit-data` returned 200 and
      performed a real upsert (confirmed via `_history` after two identical
      POSTs). HAPI's clinical-reasoning module simply doesn't register the
      type-level operation, so base mode has to target the instance.

    Any 2xx is success; the response body (HAPI returns a transaction Bundle)
    carries no information the job needs.

    The reporter Organization is no longer inlined per-patient (see
    build_submission_workflow / DeqmSubmitDataWorkflow.transfer_patient) —
    that was the primary source of ResourceVersionConflictException under
    concurrent batches, since every patient upserted the same shared id. This
    retry stays as insurance for incidental conflicts only (e.g. a patient's
    own resources overlapping a concurrent evaluate-measure read/write on the
    same server), one retry with a short backoff before giving up — not the
    primary defense against the shared-Organization storm anymore.
    """
    if mode == SUBMIT_DATA_MODE_STU5:
        url = f"{mcs_url}/Measure/$deqm-submit-data"
    else:
        url = f"{mcs_url}/Measure/{measure_id}/$submit-data"
    headers = {"Content-Type": "application/fhir+json", **(auth_headers or {})}
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(2):
            start_ms = int(time.monotonic() * 1000)
            resp = await client.post(url, json=parameters, headers=headers)
            latency_ms = int(time.monotonic() * 1000) - start_ms
            if resp.status_code >= 300:
                if resp.status_code in (409, 412) and attempt < 1:
                    logger.warning(
                        "Conflict submitting $submit-data (HTTP %s) — retrying",
                        resp.status_code,
                        extra={
                            "mcs_url": sanitize_url(mcs_url),
                            "status_code": resp.status_code,
                            "attempt": attempt + 1,
                        },
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise FhirOperationError(
                    operation="submit-data",
                    url=url,
                    status_code=resp.status_code,
                    outcome=FhirOperationOutcome.from_response(resp),
                    latency_ms=latency_ms,
                )
            logger.info(
                "Submitted data via %s",
                mode,
                extra={"mcs_url": sanitize_url(mcs_url), "latency_ms": latency_ms},
            )
            return


async def _remap_valueset_ids_for_hapi(
    entries: list[dict[str, Any]],
    client: httpx.AsyncClient,
    base_url: str,
    auth_headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Rewrite ValueSet resource IDs to match existing HAPI resources.

    HAPI enforces unique (url, version) pairs.  If the bundle contains a ValueSet
    with a bare OID id (e.g. "…1014") but HAPI already holds the same url+version
    under a versioned id (e.g. "…1014-20240112"), the POST fails with HAPI-0902.
    Querying by URL and remapping the id turns the create into an in-place update.

    `base_url` must be the same MCS the bundle is about to be POSTed to —
    remapping against a different server would rewrite ids to values that don't
    exist on the upload target.
    """
    for entry in entries:
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "ValueSet" or not resource.get("url"):
            continue
        try:
            resp = await client.get(
                f"{base_url}/ValueSet",
                params={"url": resource["url"], "_count": "1"},
                headers=auth_headers or {},
                timeout=10,
            )
            if resp.status_code == 200:
                existing = resp.json().get("entry", [])
                if existing:
                    hapi_id = existing[0]["resource"]["id"]
                    if hapi_id != resource.get("id"):
                        resource["id"] = hapi_id
                        # Also rewrite the transaction request URL so HAPI PUTs
                        # to the existing resource instead of creating a new one.
                        req = entry.get("request", {})
                        if req.get("url", "").startswith("ValueSet/"):
                            req["url"] = f"ValueSet/{hapi_id}"
        except httpx.RequestError:
            pass
    return entries


async def upload_measure_bundle(
    bundle_json: dict[str, Any],
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """POST a Measure bundle to the measure engine.

    `base_url` is REQUIRED — it identifies the active MCS connection. It is
    deliberately not defaulted to `settings.MEASURE_ENGINE_URL`: a default is
    exactly how issue #396 (measure management ignoring the connected MCS)
    stayed invisible.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Remap ValueSet IDs to avoid HAPI-0902 url+version conflicts with
        # resources already loaded by seed or prior uploads.
        entries = list(bundle_json.get("entry", []))
        entries = await _remap_valueset_ids_for_hapi(entries, client, base_url, auth_headers)
        bundle = {**bundle_json, "entry": entries}
        resp = await client.post(
            base_url,
            json=bundle,
            headers={**(auth_headers or {}), "Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()
        return resp.json()


async def delete_measure(
    measure_id: str,
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> None:
    """Delete a Measure resource from the measure engine.

    `base_url` is REQUIRED — see `upload_measure_bundle` for why.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(f"{base_url}/Measure/{measure_id}", headers=auth_headers or {})
        if resp.status_code not in (200, 202, 204):
            resp.raise_for_status()


async def measure_exists(
    measure_id: str,
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> bool:
    """Return True when `measure_id` exists on the MCS at `base_url`.

    Uses `Measure?_id=…&_summary=count` so the MCS returns a bare count bundle
    rather than the full resource.

    Transport failures (connect errors, timeouts, non-2xx) propagate to the
    caller. Swallowing them into `False` would make "the MCS is unreachable"
    indistinguishable from "the measure isn't there", and `POST /jobs` needs to
    answer 502 for one and 400 for the other.
    """
    url = f"{base_url}/Measure"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params={"_id": measure_id, "_summary": "count"},
            headers=auth_headers or {},
        )
        resp.raise_for_status()
        bundle = resp.json()
    return bundle.get("total", 0) > 0


async def wipe_patient_data(*, base_url: str, strict: bool = True, auth_headers: dict[str, str] | None = None) -> None:
    """Delete patient-related data from a FHIR server.

    Called at the START of a new job (with base_url=MEASURE_ENGINE_URL) to clean
    up data from the prior run, and by the factory-reset endpoint (with the active
    CDR URL) to wipe all patient data from the CDR.

    Raises RuntimeError after 3 consecutive HTTP failures regardless of strict mode.
    A timed-out DELETE leaves HAPI's server-side operation still running; pushing new
    data over it causes the in-flight DELETE to wipe the freshly-pushed resources.
    The strict parameter is kept for API compatibility but no longer silences failures.

    `auth_headers` credentials the target server. A remote MCS rejects every DELETE
    with 401 without them, which would leave the prior job's patients in place and
    contaminate the next evaluation.
    """
    # Delete clinical resources before Patient: HAPI returns 409 when Patient is
    # referenced by Condition/Encounter/etc., so Patient must be last.
    resource_types = [
        "MeasureReport",
        "Condition",
        "Observation",
        "Encounter",
        "Procedure",
        "MedicationRequest",
        "MedicationAdministration",
        "Immunization",
        "DiagnosticReport",
        "AllergyIntolerance",
        "AdverseEvent",
        "CarePlan",
        "CareTeam",
        "Goal",
        "ServiceRequest",
        "DeviceRequest",
        "Medication",
        "Task",
        "Coverage",
        "Claim",
        "Location",
        "Practitioner",
        "Organization",
        "Patient",
    ]
    consecutive_failures = 0
    async with httpx.AsyncClient(timeout=300.0) as client:
        for rt in resource_types:
            try:
                # Use conditional delete: DELETE ResourceType?_lastUpdated=gt1900-01-01
                delete_url = f"{base_url}/{rt}?_lastUpdated=gt1900-01-01"
                resp = await client.delete(delete_url, headers=auth_headers or {})
                if resp.status_code < 300:
                    logger.info("Wiped resource type", extra={"resourceType": rt})
                elif resp.status_code in (401, 403):
                    # Credentials are wrong or missing for this server. Falling
                    # back would sweep unauthenticated, fail silently, and let
                    # wipe_patient_data report success while deleting nothing —
                    # the prior job's resources then inflate the next job's
                    # populations with no error signal. Fail loudly instead.
                    raise RuntimeError(
                        f"Not authorized to wipe {rt} at the target server (HTTP {resp.status_code}). "
                        "Check the connection's credentials. Job aborted rather than "
                        "evaluating against stale data."
                    )
                else:
                    # Conditional delete unsupported (e.g. HAPI without
                    # allow_multiple_delete) — fall back to search-and-delete,
                    # carrying the same credentials.
                    await _delete_all_of_type(client, rt, base_url, auth_headers)
                consecutive_failures = 0
            except httpx.HTTPError:
                consecutive_failures += 1
                logger.warning(
                    "Failed to wipe resource type (may not exist)",
                    extra={"resourceType": rt},
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"FHIR server unreachable: {consecutive_failures} consecutive "
                        "timeouts during wipe. Job aborted."
                    )


# Patient-scoped wipe (issue #392) ------------------------------------------
#
# Resource type -> the search parameter that scopes it to a patient. Values were
# verified empirically against `hapiproject/hapi:v8.8.0-1` by issuing
# `GET /{Type}?{param}=Patient/x&_summary=count` and checking for 200 vs 400 —
# not read off the R4 spec, because the two disagree in one place: AdverseEvent
# answers `subject` and 400s on `patient`.
#
# Ordered clinical-resources-before-Patient for the same reason as the full
# wipe: HAPI 409s on deleting a Patient that is still referenced.
_PATIENT_SCOPED_TYPES: list[tuple[str, str]] = [
    ("MeasureReport", "patient"),
    ("Condition", "patient"),
    ("Observation", "patient"),
    ("Encounter", "patient"),
    ("Procedure", "patient"),
    ("MedicationRequest", "patient"),
    ("MedicationAdministration", "patient"),
    ("Immunization", "patient"),
    ("DiagnosticReport", "patient"),
    ("AllergyIntolerance", "patient"),
    ("AdverseEvent", "subject"),
    ("CarePlan", "patient"),
    ("CareTeam", "patient"),
    ("Goal", "patient"),
    ("ServiceRequest", "patient"),
    ("DeviceRequest", "patient"),
    ("Task", "patient"),
    ("Coverage", "patient"),
    ("Claim", "patient"),
    # Patient is scoped by its own id, not by a reference param — HAPI 400s on
    # both `patient=` and `subject=` here. It MUST stay last: HAPI returns 409
    # when a Patient is still referenced by a Condition/Encounter/etc., so every
    # clinical type above has to be cleared first.
    ("Patient", "_id"),
]

# Deliberately absent from `_PATIENT_SCOPED_TYPES`: Medication, Location,
# Practitioner, Organization. HAPI 400s on both `patient=` and `subject=` for
# all four, so there is no way to scope a delete to them — and no need. They are
# shared infrastructure on a multi-tenant server (deleting another participant's
# Practitioner is exactly the harm #392 is about), and `push_resources` PUTs them
# back by ID on every job, so a stale copy is overwritten rather than orphaned.
_PATIENT_UNSCOPABLE_TYPES = ("Medication", "Location", "Practitioner", "Organization")

# Max patient IDs per conditional-delete URL. 50 x ~40-char FHIR ids keeps the
# request line under ~2KB, well inside the 8KB default header limit on HAPI's
# embedded Tomcat. A 460-patient job becomes ~10 requests per type instead of 460.
_WIPE_ID_CHUNK_SIZE = 50


async def wipe_patients_by_id(
    *,
    base_url: str,
    patient_ids: list[str],
    auth_headers: dict[str, str] | None = None,
) -> None:
    """Delete only the given patients' data from a FHIR server (issue #392).

    The safe alternative to `wipe_patient_data` when the target is a shared MCS.
    `wipe_patient_data` issues unfiltered conditional deletes, which remove every
    patient on the server — including other participants' data at a connectathon.

    This is not a correctness compromise. `evaluate_measure` calls
    `$evaluate-measure?...&subject=Patient/<id>` per patient and the job
    aggregates only over the patients it gathered, so resources belonging to
    patients this job never evaluates cannot affect its populations. The stale
    data that *can* affect them — a resource attached to a patient this job is
    about to push, left over from a prior run and absent from this push — is
    exactly what this deletes.

    No-ops on an empty `patient_ids`: falling through to an unscoped sweep is the
    bug this function exists to prevent.

    Raises RuntimeError on 401/403 (same fail-loud rule as `wipe_patient_data`:
    a wipe that reports success while deleting nothing corrupts the next
    evaluation silently) and after 3 consecutive transport failures.
    """
    if not patient_ids:
        logger.info("Scoped wipe: no patients to wipe", extra={"target": sanitize_url(base_url)})
        return

    chunks = [patient_ids[i : i + _WIPE_ID_CHUNK_SIZE] for i in range(0, len(patient_ids), _WIPE_ID_CHUNK_SIZE)]
    logger.info(
        "Scoped wipe starting",
        extra={
            "target": sanitize_url(base_url),
            "patient_count": len(patient_ids),
            "resource_types": len(_PATIENT_SCOPED_TYPES),
            "requests": len(_PATIENT_SCOPED_TYPES) * len(chunks),
            "skipped_types": list(_PATIENT_UNSCOPABLE_TYPES),
        },
    )

    consecutive_failures = 0
    async with httpx.AsyncClient(timeout=300.0) as client:
        for rt, param in _PATIENT_SCOPED_TYPES:
            for chunk in chunks:
                # Comma-joined values are a FHIR OR match, so one request covers
                # the whole chunk. Unqualified ids (not "Patient/x") match the
                # reference search param and keep the URL short.
                search_filter = f"{param}={','.join(chunk)}"
                delete_url = f"{base_url}/{rt}?{search_filter}"
                try:
                    resp = await client.delete(delete_url, headers=auth_headers or {})
                    if resp.status_code in (401, 403):
                        raise RuntimeError(
                            f"Not authorized to wipe {rt} at the target server (HTTP {resp.status_code}). "
                            "Check the connection's credentials. Job aborted rather than "
                            "evaluating against stale data."
                        )
                    if resp.status_code >= 400 and resp.status_code != 404:
                        # 404 = the server doesn't stock this type; nothing to do.
                        # Anything else means the conditional delete itself was
                        # refused — most often HAPI's 412 when the search matches
                        # multiple resources and `allow_multiple_delete` is false.
                        # Our own containers enable it, but a shared remote MCS
                        # (the case this function exists for) may not, so fall back
                        # to a scoped search-and-delete rather than logging and
                        # moving on with nothing deleted.
                        logger.info(
                            "Scoped wipe: conditional delete refused, falling back to per-resource delete",
                            extra={"resourceType": rt, "status_code": resp.status_code},
                        )
                        await _delete_all_of_type(client, rt, base_url, auth_headers, search_filter=search_filter)
                    consecutive_failures = 0
                except httpx.HTTPError:
                    consecutive_failures += 1
                    logger.warning(
                        "Scoped wipe: request failed",
                        extra={"resourceType": rt, "consecutive_failures": consecutive_failures},
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        raise RuntimeError(
                            f"FHIR server unreachable: {consecutive_failures} consecutive "
                            "timeouts during scoped wipe. Job aborted."
                        )

    logger.info(
        "Scoped wipe complete",
        extra={"target": sanitize_url(base_url), "patient_count": len(patient_ids)},
    )


async def wipe_measure_definitions(*, base_url: str, auth_headers: dict[str, str] | None = None) -> None:
    """Delete all measure-definition resources from the measure engine at `base_url`.

    Removes Library, Measure, ValueSet, CodeSystem, and ConceptMap.
    Does NOT touch clinical resources (Patient, Encounter, etc.).
    Called from the admin UI to recover from JVM/H2 state corruption (issue #238).
    After this call the bundle_loader must re-seed the engine before the next job.

    `base_url` is REQUIRED and keyword-only — it identifies the MCS the caller
    actually means. It previously read `settings.MEASURE_ENGINE_URL`, so an admin
    connected to a remote MCS wiped Lenny's local container and got a success
    response while the server they were looking at kept its data (issue #397).

    Callers own the read-only guard: this function will wipe whatever server it is
    pointed at, and pointing it at a shared MCS is exactly the hazard that made
    #397 worth fixing carefully rather than quickly.
    """
    definition_types = ["Library", "Measure", "ValueSet", "CodeSystem", "ConceptMap"]
    logger.warning(
        "Wiping measure definitions",
        extra={"target": sanitize_url(base_url), "resource_types": definition_types},
    )
    async with httpx.AsyncClient(timeout=300.0) as client:
        for rt in definition_types:
            try:
                delete_url = f"{base_url}/{rt}?_lastUpdated=gt1900-01-01"
                resp = await client.delete(delete_url, headers=auth_headers or {})
                if resp.status_code < 300:
                    logger.info("Wiped measure definition type", extra={"resourceType": rt})
                elif resp.status_code in (401, 403):
                    # Explicit, matching wipe_patient_data and wipe_patients_by_id.
                    # A 401 would otherwise fall through to the fallback sweep and
                    # surface as "not authorized to enumerate" — which does fail
                    # loudly, but names the wrong operation. All three wipes should
                    # report an auth failure the same way.
                    raise RuntimeError(
                        f"Not authorized to wipe {rt} definitions at the target server "
                        f"(HTTP {resp.status_code}). Check the connection's credentials. "
                        "Refusing to report a successful wipe."
                    )
                else:
                    await _delete_all_of_type(client, rt, base_url, auth_headers)
            except httpx.HTTPError:
                logger.warning("Failed to wipe measure definition type", extra={"resourceType": rt})


async def _delete_all_of_type(
    client: httpx.AsyncClient,
    resource_type: str,
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    search_filter: str | None = None,
) -> None:
    """Delete resources of a given type one by one from the given FHIR server.

    `auth_headers` MUST be threaded through from the caller. This is the fallback
    path taken when conditional delete is unsupported, and the target may be an
    authenticated remote MCS — an unauthenticated sweep here 401s on the first
    GET and returns silently, making the whole wipe a no-op that reports success.

    `search_filter` narrows the sweep to matching resources (e.g.
    `"patient=p1,p2"`). Without it every resource of the type is deleted, which is
    what `wipe_patient_data` wants; `wipe_patients_by_id` passes a filter so the
    fallback stays as scoped as the conditional delete it is standing in for.
    Passing an unscoped fallback there would turn a shared-server safety feature
    into the exact full wipe it exists to prevent (issue #392).
    """
    headers = auth_headers or {}
    query = f"_count=100&{search_filter}" if search_filter else "_count=100"
    url: Optional[str] = f"{base_url}/{resource_type}?{query}"
    while url:
        resp = await client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Not authorized to enumerate {resource_type} at the target server "
                f"(HTTP {resp.status_code}). Refusing to report a successful wipe."
            )
        if resp.status_code != 200:
            break
        bundle = resp.json()
        entries = bundle.get("entry", [])
        if not entries:
            break
        for entry in entries:
            res = entry.get("resource", {})
            res_id = res.get("id")
            # Reject ids that would escape the resource-type path. The bundle is
            # server-supplied, so "../../Measure/CMS130" would otherwise be
            # normalised into a DELETE against an arbitrary path.
            if res_id and "/" not in res_id and ".." not in res_id:
                del_url = f"{base_url}/{resource_type}/{res_id}"
                try:
                    await client.delete(del_url, headers=headers)
                except httpx.HTTPError:
                    pass
        # Re-check if more remain. Only follow a same-origin next link — a
        # hostile or misconfigured server could otherwise steer this loop at
        # an arbitrary host.
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                next_url = link.get("url")
                if next_url and _same_origin(base_url, next_url):
                    url = next_url
                elif next_url:
                    logger.warning(
                        "SSRF: wipe pagination next link rejected (origin mismatch)",
                        extra={"url": sanitize_url(next_url)},
                    )
                break


async def resolve_evaluated_resource(
    reference: str,
    base_url: str,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve an evaluatedResource reference from the measure engine at `base_url`.

    `base_url` is REQUIRED. It used to default to `settings.MEASURE_ENGINE_URL` with
    no credentials, which only ever worked for a local unauthenticated HAPI: against
    an authenticated remote MCS the resolve 401'd, and against any remote MCS it
    asked the wrong server entirely (issue #397).
    """
    url = f"{base_url}/{reference}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=auth_headers or {})
        resp.raise_for_status()
        return resp.json()


async def snapshot_evaluated_resources(
    measure_report: dict[str, Any],
    base_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    """Resolve every evaluatedResource reference in a MeasureReport to a stored snapshot.

    Returns a list of full FHIR resources. Per-reference failures are skipped and logged
    rather than raised — partial snapshots are still useful and the caller has already
    persisted the MeasureReport itself. Returns None when there is nothing to snapshot.

    `base_url`/`auth_headers` identify the MCS the report came from, so snapshots
    are read back from the same server that evaluated them.
    """
    refs = [
        r.get("reference")
        for r in (measure_report or {}).get("evaluatedResource", [])
        if isinstance(r, dict) and r.get("reference")
    ]
    if not refs:
        return None

    resources: list[dict[str, Any]] = []
    for ref in refs:
        try:
            resources.append(await resolve_evaluated_resource(ref, base_url, auth_headers))
        except Exception as exc:
            logger.warning(
                "Failed to snapshot evaluated resource",
                extra={"reference": ref, "error": str(exc)[:200]},
            )
    return resources


async def list_groups(
    cdr_url: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """List all Group resources on the CDR."""
    groups: list[dict[str, Any]] = []
    url: Optional[str] = f"{cdr_url}/Group?_count=100"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, headers=auth_headers)
            resp.raise_for_status()
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") == "Group":
                    groups.append(
                        {
                            "id": resource.get("id"),
                            "name": resource.get("name"),
                            "type": resource.get("type"),
                            "member_count": len(resource.get("member", [])),
                        }
                    )
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    next_url = link.get("url")
                    if next_url and _same_origin(cdr_url, next_url):
                        url = next_url
                    elif next_url:
                        logger.warning(
                            "SSRF: pagination next link rejected (origin mismatch)",
                            extra={"url": sanitize_url(next_url)},
                        )
                    break
    return groups


_CQL_CHARACTERISTIC_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/characteristicExpression"
_EXPRESSION_PREVIEW_MAX = 120


def _extract_cql_expression(resource: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return {'language', 'expression'} if resource carries a CQL valueExpression,
    else None. Filters by extension URL and `text/cql*` language prefix."""
    for ext in resource.get("extension", []) or []:
        if ext.get("url") != _CQL_CHARACTERISTIC_EXTENSION_URL:
            continue
        ve = ext.get("valueExpression") or {}
        language = ve.get("language") or ""
        expression = ve.get("expression") or ""
        if language.startswith("text/cql") and expression:
            return {"language": language, "expression": expression}
    return None


async def list_groups_with_expression(
    cdr_url: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """List CDR Group resources filtered to those carrying a CQL valueExpression.

    Used by the experimental /api/groups endpoint (issue #322). Distinct from
    `list_groups` which serves the measure-job flow and is intentionally not
    modified."""
    groups: list[dict[str, Any]] = []
    url: Optional[str] = f"{cdr_url}/Group?_count=100"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, headers=auth_headers)
            resp.raise_for_status()
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") != "Group":
                    continue
                cql = _extract_cql_expression(resource)
                if cql is None:
                    continue
                expression = cql["expression"]
                preview = (
                    expression
                    if len(expression) <= _EXPRESSION_PREVIEW_MAX
                    else expression[:_EXPRESSION_PREVIEW_MAX] + "..."
                )
                groups.append(
                    {
                        "id": resource.get("id"),
                        "name": resource.get("name"),
                        "type": resource.get("type"),
                        "expression_language": cql["language"],
                        "expression_preview": preview,
                    }
                )
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    next_url = link.get("url")
                    if next_url and _same_origin(cdr_url, next_url):
                        url = next_url
                    elif next_url:
                        logger.warning(
                            "SSRF: pagination next link rejected (origin mismatch)",
                            extra={"url": sanitize_url(next_url)},
                        )
                    break
    return groups


class GroupEvaluateError(Exception):
    """Raised when Group/<id>/$evaluate returns a non-success response.

    Carries the upstream HTTP status code and (if parseable) the FHIR
    OperationOutcome body so callers can pass it through to clients."""

    def __init__(
        self,
        message: str,
        status_code: int,
        operation_outcome: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.operation_outcome = operation_outcome


def _format_patient_name(patient: dict[str, Any]) -> Optional[str]:
    """Render the patient's first HumanName as 'family, given1 given2', or None."""
    names = patient.get("name") or []
    if not names:
        return None
    n = names[0]
    family = n.get("family")
    given = " ".join(n.get("given") or [])
    if family and given:
        return f"{family}, {given}"
    return family or given or None


async def evaluate_group_and_resolve_members(
    cdr_url: str,
    group_id: str,
    auth_headers: dict[str, str],
) -> dict[str, Any]:
    """Invoke Group/<id>/$evaluate on the CDR and resolve members to patient summaries.

    Returns: {"group_id", "evaluated_at" (ISO Z), "member_count", "members":
    [{"id", "name", "gender", "birth_date", "lookup_error"?}, ...]}.

    Raises GroupEvaluateError if $evaluate returns non-success."""
    evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{cdr_url}/Group/{group_id}/$evaluate",
                headers=auth_headers,
            )
        except httpx.RequestError as exc:
            # Let TimeoutException propagate so the router maps it to 504.
            if isinstance(exc, httpx.TimeoutException):
                raise
            raise GroupEvaluateError(
                f"Group/$evaluate unreachable: {type(exc).__name__}",
                status_code=502,
                operation_outcome=None,
            ) from exc
        if not resp.is_success:
            try:
                outcome = resp.json()
            except Exception:
                outcome = None
            if not isinstance(outcome, dict) or outcome.get("resourceType") != "OperationOutcome":
                outcome = None
            raise GroupEvaluateError(
                f"Group/$evaluate failed with status {resp.status_code}",
                status_code=resp.status_code,
                operation_outcome=outcome,
            )

        evaluated_group = resp.json()
        refs: list[tuple[str, str]] = []
        for m in evaluated_group.get("member", []):
            ref = m.get("entity", {}).get("reference", "")
            if ref.startswith("Patient/"):
                refs.append((ref.split("/", 1)[1], ref))

        semaphore = asyncio.Semaphore(10)

        async def fetch(patient_id: str, ref: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    pr = await client.get(
                        f"{cdr_url}/Patient/{patient_id}",
                        headers=auth_headers,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not fetch group member",
                        extra={
                            "group_id": group_id,
                            "patient_ref": ref,
                            "error": str(exc) or type(exc).__name__,
                        },
                    )
                    return {
                        "id": patient_id,
                        "name": None,
                        "gender": None,
                        "birth_date": None,
                        "lookup_error": f"{type(exc).__name__}: {exc}",
                    }
                if pr.status_code != 200:
                    logger.warning(
                        "Could not fetch group member",
                        extra={
                            "group_id": group_id,
                            "patient_ref": ref,
                            "error": f"HTTP {pr.status_code}",
                        },
                    )
                    return {
                        "id": patient_id,
                        "name": None,
                        "gender": None,
                        "birth_date": None,
                        "lookup_error": f"HTTP {pr.status_code}",
                    }
                p = pr.json()
                return {
                    "id": patient_id,
                    "name": _format_patient_name(p),
                    "gender": p.get("gender"),
                    "birth_date": p.get("birthDate"),
                }

        members = await asyncio.gather(*[fetch(pid, ref) for pid, ref in refs])

    return {
        "group_id": group_id,
        "evaluated_at": evaluated_at,
        "member_count": len(members),
        "members": members,
    }


async def get_group_members(
    cdr_url: str,
    group_id: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Fetch Patient resources for all members of a Group (concurrent)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Fetch the Group resource
        resp = await client.get(f"{cdr_url}/Group/{group_id}", headers=auth_headers)
        resp.raise_for_status()
        group = resp.json()

        # Extract Patient IDs from members
        patient_ids: list[tuple[str, str]] = []  # (patient_id, original_ref)
        for member in group.get("member", []):
            ref = member.get("entity", {}).get("reference", "")
            if ref.startswith("Patient/"):
                patient_id = ref.split("/", 1)[1]
                patient_ids.append((patient_id, ref))

        # Fetch all patients concurrently with a semaphore to avoid overwhelming the CDR
        semaphore = asyncio.Semaphore(10)

        async def fetch_patient(patient_id: str, ref: str) -> Optional[dict[str, Any]]:
            async with semaphore:
                patient_resp = await client.get(f"{cdr_url}/Patient/{patient_id}", headers=auth_headers)
                if patient_resp.status_code == 200:
                    return patient_resp.json()
                logger.warning(
                    "Could not fetch group member",
                    extra={"group_id": group_id, "patient_ref": ref},
                )
                return None

        results = await asyncio.gather(
            *[fetch_patient(pid, ref) for pid, ref in patient_ids],
            return_exceptions=True,
        )
        patients = [r for r in results if isinstance(r, dict)]

    logger.info(
        "Gathered group members",
        extra={"group_id": group_id, "count": len(patients)},
    )
    return patients


async def list_measures(
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """List all Measure resources on the measure engine at `base_url`.

    `base_url` is REQUIRED — see `upload_measure_bundle` for why.
    """
    url = f"{base_url}/Measure?_count=100"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=auth_headers or {})
        resp.raise_for_status()
        return resp.json()


async def probe_mcs_data_requirements(
    mcs_url: str,
    auth_type: str = "none",
    auth_credentials: Optional[dict] = None,
) -> dict[str, Any]:
    """Deep-probe an MCS by exercising `$data-requirements` on one of its measures.

    Confirms the MCS can resolve a Measure's Library + ValueSets — a stricter
    test than `verify_fhir_connection` (which only fetches /metadata). Picks
    the first available measure on the MCS via `Measure?_count=1`, then calls
    `$data-requirements` against it.

    Returns a summary dict on success. Raises `FhirOperationError` on any
    failure path so callers can convert to the standard error envelope.
    """
    _validate_ssrf_url(mcs_url, label="mcs_url")
    headers = await _build_auth_headers(auth_type, auth_credentials)

    # Step 1: find one measure on the MCS.
    list_url = f"{mcs_url}/Measure?_count=1"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(list_url, headers=headers)
            list_latency_ms = round((time.monotonic() - t0) * 1000)
            if not resp.is_success:
                outcome = FhirOperationOutcome.from_response(resp)
                raise FhirOperationError(
                    operation="probe-data-requirements",
                    url=list_url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=list_latency_ms,
                )
            bundle = resp.json()
            entries = bundle.get("entry") or []
            if not entries:
                # Empty MCS — synthesize an OperationOutcome so the UI can render it.
                empty_diagnostics = (
                    "MCS is reachable but has no Measure resources. "
                    "Load measures via the seed workflow or upload one via "
                    "the Measures page."
                )
                empty_outcome = FhirOperationOutcome(
                    issues=[FhirIssue(severity="warning", code="not-found", diagnostics=empty_diagnostics)],
                    raw={
                        "resourceType": "OperationOutcome",
                        "issue": [{"severity": "warning", "code": "not-found", "diagnostics": empty_diagnostics}],
                    },
                )
                raise FhirOperationError(
                    operation="probe-data-requirements",
                    url=list_url,
                    status_code=200,
                    outcome=empty_outcome,
                    latency_ms=list_latency_ms,
                )
            measure = entries[0].get("resource") or {}
            measure_id = measure.get("id")
            if not measure_id:
                raise FhirOperationError(
                    operation="probe-data-requirements",
                    url=list_url,
                    status_code=200,
                    outcome=None,
                    latency_ms=list_latency_ms,
                )
    except FhirOperationError:
        raise
    except Exception as exc:
        raise FhirOperationError(
            operation="probe-data-requirements",
            url=list_url,
            status_code=None,
            outcome=None,
            latency_ms=round((time.monotonic() - t0) * 1000),
            cause=exc,
        )

    # Step 2: call $data-requirements on that measure.
    dr_url = f"{mcs_url}/Measure/{measure_id}/$data-requirements?periodStart=2024-01-01&periodEnd=2024-12-31"
    t1 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(dr_url, headers=headers)
            dr_latency_ms = round((time.monotonic() - t1) * 1000)
            if not resp.is_success:
                outcome = FhirOperationOutcome.from_response(resp)
                raise FhirOperationError(
                    operation="probe-data-requirements",
                    url=dr_url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=dr_latency_ms,
                )
            library = resp.json()
            requirements = library.get("dataRequirement") or []
            return {
                "status": "ok",
                "measure_id": measure_id,
                "measure_name": measure.get("name") or measure.get("title") or measure_id,
                "data_requirement_count": len(requirements),
                "list_latency_ms": list_latency_ms,
                "data_requirements_latency_ms": dr_latency_ms,
                "url": sanitize_url(dr_url),
            }
    except FhirOperationError:
        raise
    except Exception as exc:
        raise FhirOperationError(
            operation="probe-data-requirements",
            url=dr_url,
            status_code=None,
            outcome=None,
            latency_ms=round((time.monotonic() - t1) * 1000),
            cause=exc,
        )


async def verify_fhir_connection(
    fhir_url: str,
    auth_type: str = "none",
    auth_credentials: Optional[dict] = None,
) -> dict[str, Any]:
    """Test connectivity to a FHIR server by fetching its metadata."""
    _validate_ssrf_url(fhir_url, label="cdr_url")
    headers = await _build_auth_headers(auth_type, auth_credentials)
    url = f"{fhir_url}/metadata"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            latency_ms = round((time.monotonic() - t0) * 1000)
            if not resp.is_success:
                outcome = FhirOperationOutcome.from_response(resp)
                raise FhirOperationError(
                    operation="test-connection",
                    url=url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
            data = resp.json()
            return {
                "status": "connected",
                "fhir_version": data.get("fhirVersion", "unknown"),
                "software": data.get("software", {}).get("name", "unknown"),
                "response_time_ms": latency_ms,
                "url": sanitize_url(url),
            }
    except FhirOperationError:
        raise
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        raise FhirOperationError(
            operation="test-connection",
            url=url,
            status_code=None,
            outcome=None,
            latency_ms=latency_ms,
            cause=exc,
        )
