#!/usr/bin/env python3
"""Copy measure content (Measure / Library / ValueSet / CodeSystem) from one
FHIR server to another.

Reads every resource of the selected types from --source, then writes them to
--target as FHIR `batch` Bundles of PUT entries (same shape as
`backend/app/services/fhir_client.py::push_resources`). Batch, not transaction,
so the target does not enforce cross-entry referential integrity.

Resources are written in dependency order: CodeSystem, ValueSet, Library,
Measure. Libraries missing `url` get `url = "Library/{id}"` backfilled so HAPI
8.x can resolve canonical references (mirrors `_normalize_measure_def`).

Before writing anything it runs a read-only collision check and refuses to
proceed, without --overwrite, if the target holds different content at any of
the same IDs. A probe that cannot read the target (401/403/5xx) is an error, not
an empty target.

AUTH
    Secrets are never command-line arguments: argv is readable by any process via
    `ps` and is recorded in shell history.

    Access token, first usable wins:
      1. $TARGET_FHIR_TOKEN
      2. the cache file (default ~/.lenny-target-token)
      3. minted via the OAuth2 client_credentials grant
    A cached token whose JWT `exp` has passed is skipped, and a 401 mid-run
    triggers one refresh-and-retry, so a long push cannot die on expiry.

    Client credentials, first found wins:
      1. $TARGET_CLIENT_ID / $TARGET_CLIENT_SECRET
      2. macOS keychain, service `lenny-target-oauth`

    Recommended one-time keychain setup (`-w` with no value prompts, so the
    secret never reaches argv or history):
        security add-generic-password -s lenny-target-oauth -a client_id -w
        security add-generic-password -s lenny-target-oauth -a client_secret -w

Usage:
    python3 scripts/copy_measure_content.py --target https://example.org/fhir --check-only
    python3 scripts/copy_measure_content.py --target https://example.org/fhir
    python3 scripts/copy_measure_content.py --target https://example.org/fhir --refresh-token
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Dependency order: referenced content first, Measures last.
DEFAULT_TYPES = ["CodeSystem", "ValueSet", "Library", "Measure"]
DEFAULT_SOURCE = "http://localhost:8080/fhir"
PAGE_SIZE = 200
FHIR_JSON = "application/fhir+json"
DEFAULT_TOKEN_FILE = "~/.lenny-target-token"
DEFAULT_KEYCHAIN_SERVICE = "lenny-target-oauth"


def _default_token_url(target: str) -> str:
    """Guess the token endpoint as the target's origin + /token."""
    parsed = urllib.parse.urlparse(target)
    return f"{parsed.scheme}://{parsed.netloc}/token"


class TransferError(Exception):
    pass


class AuthError(TransferError):
    pass


def _keychain_get(service: str, account: str) -> str | None:
    """Read one secret from the macOS login keychain. None if absent or not macOS."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _jwt_expiry(token: str) -> int | None:
    """Read the `exp` claim from a JWT without verifying it.

    Signature verification is the server's job. We only need to know whether it is
    worth sending. Returns None for opaque (non-JWT) tokens.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) else None


class TokenSource:
    """Supplies a bearer token for the target, minting a fresh one when needed.

    Resolution order for an existing token: TARGET_FHIR_TOKEN env var, then the
    cache file. Either is used only if it is not already expired. When there is no
    usable token — or the server rejects one mid-run — the client-credentials grant
    mints a new one and rewrites the cache with 0600 permissions.

    Client credentials are looked up from the environment first, then the macOS
    keychain. They are never accepted as command-line arguments: argv is world-
    readable via `ps` and lands in shell history.
    """

    SKEW_SECONDS = 120  # refresh early rather than race the clock mid-batch

    def __init__(
        self,
        token_url: str | None,
        cache_path: pathlib.Path | None,
        keychain_service: str,
        token_env: str,
        id_env: str,
        secret_env: str,
    ) -> None:
        self.token_url = token_url
        self.cache_path = cache_path
        self.keychain_service = keychain_service
        self.token_env = token_env
        self.id_env = id_env
        self.secret_env = secret_env
        self._token: str | None = None
        self.last_source = "none"
        self.refresh_count = 0

    def client_credentials(self) -> tuple[str, str] | None:
        client_id = os.environ.get(self.id_env, "").strip() or _keychain_get(self.keychain_service, "client_id")
        client_secret = os.environ.get(self.secret_env, "").strip() or _keychain_get(
            self.keychain_service, "client_secret"
        )
        if client_id and client_secret:
            return client_id, client_secret
        return None

    @property
    def can_refresh(self) -> bool:
        return bool(self.token_url) and self.client_credentials() is not None

    def _cached_token(self) -> tuple[str, str] | None:
        """Return (token, where_it_came_from) for the first non-expired candidate."""
        candidates: list[tuple[str, str]] = []
        env_token = os.environ.get(self.token_env, "").strip()
        if env_token:
            candidates.append((env_token, f"${self.token_env}"))
        if self.cache_path and self.cache_path.exists():
            try:
                file_token = self.cache_path.read_text().strip()
            except OSError:
                file_token = ""
            if file_token:
                candidates.append((file_token, str(self.cache_path)))

        for token, origin in candidates:
            exp = _jwt_expiry(token)
            if exp is None:
                return token, f"{origin} (opaque token, expiry unknown)"
            if exp - time.time() > self.SKEW_SECONDS:
                mins = (exp - time.time()) / 60
                return token, f"{origin} (valid {mins:.0f} more min)"
        return None

    def mint(self) -> str:
        """Exchange client credentials for a fresh access token."""
        if not self.token_url:
            raise AuthError("no --token-url configured, cannot mint a token")
        creds = self.client_credentials()
        if not creds:
            raise AuthError(
                f"no client credentials found. Set ${self.id_env} and ${self.secret_env}, "
                f"or add them to the macOS keychain under service '{self.keychain_service}'."
            )
        client_id, client_secret = creds
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        status, payload, raw = _request(
            self.token_url,
            method="POST",
            body=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        if status >= 400 or not isinstance(payload, dict):
            raise AuthError(f"token endpoint returned HTTP {status}: {raw[:300]}")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError(f"token endpoint response had no access_token: {raw[:300]}")

        self._token = token
        self.refresh_count += 1
        self._write_cache(token)
        exp = _jwt_expiry(token)
        lifetime = payload.get("expires_in")
        if exp is not None:
            detail = f"expires in {(exp - time.time()) / 60:.0f} min"
        elif isinstance(lifetime, int):
            detail = f"expires in {lifetime / 60:.0f} min"
        else:
            detail = "expiry unknown"
        print(f"  minted a new access token ({detail})")
        return token

    def _write_cache(self, token: str) -> None:
        if not self.cache_path:
            return
        try:
            # Create with 0600 from the start — never briefly world-readable.
            fd = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(token + "\n")
            os.chmod(self.cache_path, 0o600)
        except OSError as exc:
            print(f"  warning: could not write token cache {self.cache_path}: {exc}")

    def token(self) -> str:
        if self._token:
            return self._token
        cached = self._cached_token()
        if cached:
            self._token, self.last_source = cached[0], cached[1]
            print(f"  using token from {self.last_source}")
            return self._token
        if not self.can_refresh:
            raise AuthError(
                "no valid token available and cannot mint one.\n"
                f"  Provide a token via ${self.token_env} or {self.cache_path}, or supply\n"
                f"  client credentials (${self.id_env}/${self.secret_env} or macOS keychain\n"
                f"  service '{self.keychain_service}') together with --token-url."
            )
        self.last_source = "client_credentials grant"
        return self.mint()

    def headers(self) -> dict[str, str]:
        """Auth header, or none at all.

        An unauthenticated server (local HAPI) needs no header, so a missing token
        is not fatal here — we send the request bare and let the target answer. A
        real 401 is then reported by preflight with an actionable message, rather
        than this layer guessing that auth was required.
        """
        try:
            return {"Authorization": f"Bearer {self.token()}"}
        except AuthError:
            return {}

    def invalidate(self) -> bool:
        """Drop the current token and mint a replacement. False if that is impossible."""
        self._token = None
        if not self.can_refresh:
            return False
        try:
            self.mint()
        except AuthError as exc:
            print(f"  token refresh failed: {exc}")
            return False
        return True


def _request_target(
    url: str,
    auth: TokenSource | dict[str, str],
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    """Request against the target, refreshing the token once on a 401.

    A long push can straddle the token's lifetime, so one silent retry after a
    refresh keeps a multi-minute run from dying halfway through.
    """
    headers = {"Accept": FHIR_JSON, **(extra_headers or {})}
    if isinstance(auth, TokenSource):
        headers.update(auth.headers())
    else:
        headers.update(auth)

    status, payload, raw = _request(url, method=method, body=body, headers=headers)
    if status == 401 and isinstance(auth, TokenSource) and auth.can_refresh:
        print("  target returned 401 — refreshing token and retrying once")
        if auth.invalidate():
            headers.update(auth.headers())
            status, payload, raw = _request(url, method=method, body=body, headers=headers)
    return status, payload, raw


def _request(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> tuple[int, dict[str, Any] | None, str]:
    """Issue one HTTP request. Returns (status, parsed_json_or_None, raw_text)."""
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise TransferError(f"Cannot reach {url}: {exc.reason}") from exc
    try:
        return status, json.loads(raw), raw
    except json.JSONDecodeError:
        return status, None, raw


def fetch_all(source: str, resource_type: str, auth: dict[str, str]) -> list[dict[str, Any]]:
    """Page through every resource of one type on the source server."""
    resources: list[dict[str, Any]] = []
    url = f"{source.rstrip('/')}/{resource_type}?_count={PAGE_SIZE}"
    seen_pages = 0
    while url:
        status, payload, raw = _request(url, headers={"Accept": FHIR_JSON, **auth})
        if status >= 400 or payload is None:
            raise TransferError(f"GET {url} -> HTTP {status}: {raw[:400]}")
        for entry in payload.get("entry", []) or []:
            resource = entry.get("resource")
            if isinstance(resource, dict) and resource.get("id"):
                resources.append(resource)
        seen_pages += 1
        url = next(
            (link.get("url") for link in payload.get("link", []) or [] if link.get("relation") == "next"),
            None,
        )
        if seen_pages > 500:  # runaway-paging guard
            raise TransferError(f"{resource_type}: exceeded 500 pages, aborting")
    return resources


def normalize(resource: dict[str, Any]) -> dict[str, Any]:
    """Backfill Library.url so HAPI can resolve canonical Measure.library refs."""
    if resource.get("resourceType") == "Library" and not resource.get("url") and resource.get("id"):
        return {**resource, "url": f"Library/{resource['id']}"}
    return resource


def _outcome_text(entry: dict[str, Any]) -> str:
    outcome = (entry.get("response") or {}).get("outcome")
    if not isinstance(outcome, dict):
        return ""
    issues = outcome.get("issue", []) or []
    parts = [(issue.get("diagnostics") or (issue.get("details") or {}).get("text") or "").strip() for issue in issues]
    return "; ".join(p for p in parts if p)[:300]


def push_chunk(
    target: str,
    chunk: list[dict[str, Any]],
    auth: TokenSource | dict[str, str],
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """POST one batch Bundle. Returns (succeeded_refs, [(ref, status, detail)])."""
    bundle = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": normalize(r),
                "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
            }
            for r in chunk
        ],
    }
    refs = [f"{r['resourceType']}/{r['id']}" for r in chunk]
    status, payload, raw = _request_target(
        target.rstrip("/"),
        auth,
        method="POST",
        body=json.dumps(bundle).encode("utf-8"),
        extra_headers={"Content-Type": FHIR_JSON},
    )
    # Whole-bundle rejection: every entry in this chunk failed.
    if status >= 400 or payload is None:
        return [], [(ref, str(status), raw[:300]) for ref in refs]

    succeeded: list[str] = []
    failed: list[tuple[str, str, str]] = []
    entries = payload.get("entry", []) or []
    for i, ref in enumerate(refs):
        entry = entries[i] if i < len(entries) else {}
        entry_status = (entry.get("response") or {}).get("status", "")
        if entry_status.startswith("2"):
            succeeded.append(ref)
        else:
            failed.append((ref, entry_status or "no-response", _outcome_text(entry)))
    return succeeded, failed


def _identity(resource: dict[str, Any]) -> tuple[str, str]:
    """The (url, version) pair that decides whether two resources are the same content."""
    return (resource.get("url") or "", resource.get("version") or "")


def probe_existing(
    target: str,
    resource_type: str,
    ids: list[str],
    auth: TokenSource | dict[str, str],
    chunk_size: int = 20,
) -> dict[str, dict[str, Any]]:
    """Read-only: return {id: existing_resource} for the target IDs that already exist.

    Tries a chunked `_id=a,b,c` search first (few round trips), and falls back to
    one GET per resource when the server rejects multi-value `_id`. Writes nothing
    either way — GET only.

    Only 404/410 counts as "this resource is absent". Any other error (401, 403,
    5xx) raises, because a probe that cannot see the target must never be reported
    as "nothing there" — that would turn an auth failure into a silent green light
    to overwrite.
    """
    found: dict[str, dict[str, Any]] = {}
    base = target.rstrip("/")
    elements = "id,url,version,name,title,status"

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        joined = ",".join(urllib.parse.quote(rid, safe="") for rid in chunk)
        url = f"{base}/{resource_type}?_id={joined}&_elements={elements}&_count={len(chunk)}"
        status, payload, raw = _request_target(url, auth)

        if status in (401, 403) or status >= 500:
            raise TransferError(f"probe of {resource_type} failed: HTTP {status}: {raw[:300]}")

        if status >= 400 or payload is None or payload.get("resourceType") != "Bundle":
            # Server does not honor multi-value _id — fall back to single reads.
            for rid in chunk:
                one_status, one_payload, one_raw = _request_target(
                    f"{base}/{resource_type}/{urllib.parse.quote(rid, safe='')}", auth
                )
                if one_status == 200 and isinstance(one_payload, dict):
                    found[rid] = one_payload
                elif one_status in (404, 410):
                    continue  # genuinely absent
                else:
                    raise TransferError(f"probe of {resource_type}/{rid} failed: HTTP {one_status}: {one_raw[:300]}")
            continue

        for entry in payload.get("entry", []) or []:
            resource = entry.get("resource")
            if isinstance(resource, dict) and resource.get("id"):
                found[resource["id"]] = resource
    return found


def preflight_target(target: str, resource_type: str, auth: TokenSource | dict[str, str]) -> None:
    """Confirm the target answers an authenticated search before we do real work.

    `/metadata` is often unsecured, so it proves nothing about the token. Search on
    a real resource type is what the collision check and verify steps depend on.
    """
    url = f"{target.rstrip('/')}/{resource_type}?_summary=count"
    status, _, raw = _request_target(url, auth)
    if status in (401, 403):
        diagnostics = ""
        try:
            issues = json.loads(raw).get("issue", []) or []
            diagnostics = "; ".join(i.get("diagnostics", "") for i in issues if i.get("diagnostics"))
        except (json.JSONDecodeError, AttributeError):
            diagnostics = raw[:200]
        hint = ""
        if isinstance(auth, TokenSource) and not auth.can_refresh:
            hint = (
                "\n  No client credentials are configured, so a fresh token cannot be minted."
                "\n  Add them to the macOS keychain:"
                f"\n    security add-generic-password -s {auth.keychain_service} -a client_id -w"
                f"\n    security add-generic-password -s {auth.keychain_service} -a client_secret -w"
                "\n  (the -w with no value prompts, keeping the secret out of argv and history)"
            )
        raise TransferError(
            f"target rejected the credentials: HTTP {status}" + (f" — {diagnostics}" if diagnostics else "") + hint
        )
    if status >= 400:
        raise TransferError(f"target search failed: GET {url} -> HTTP {status}: {raw[:300]}")


def collision_check(
    target: str,
    inventory: dict[str, list[dict[str, Any]]],
    types: list[str],
    auth: TokenSource | dict[str, str],
) -> tuple[int, int]:
    """Report which source resources already exist on the target. Read-only.

    Returns (identical_count, differing_count). "Identical" means the target's
    resource carries the same canonical url and version as the source's — an
    overwrite there is a no-op in content terms. "Differing" is where an
    overwrite would actually replace something.
    """
    print("collision check (read-only, nothing is written):")
    identical_total = 0
    differing_total = 0
    differing_detail: list[str] = []

    for resource_type in types:
        resources = inventory[resource_type]
        if not resources:
            continue
        by_id = {r["id"]: r for r in resources}
        # A probe failure propagates: an unreadable target is not an empty target.
        existing = probe_existing(target, resource_type, list(by_id), auth)

        identical = 0
        differing = 0
        for rid, target_resource in existing.items():
            source_resource = by_id.get(rid)
            if source_resource is None:
                continue
            if _identity(source_resource) == _identity(target_resource):
                identical += 1
            else:
                differing += 1
                src_url, src_ver = _identity(source_resource)
                tgt_url, tgt_ver = _identity(target_resource)
                differing_detail.append(
                    f"  {resource_type}/{rid}\n"
                    f"      target: url={tgt_url or '(none)'} version={tgt_ver or '(none)'}\n"
                    f"      source: url={src_url or '(none)'} version={src_ver or '(none)'}"
                )

        identical_total += identical
        differing_total += differing
        new_count = len(resources) - len(existing)
        print(
            f"  {resource_type:<12} {new_count:>4} new, "
            f"{identical:>4} already identical, {differing:>4} would be replaced"
        )

    if differing_detail:
        print(f"\n{differing_total} resource(s) on the target would be REPLACED with different content:")
        for line in differing_detail[:25]:
            print(line)
        if len(differing_detail) > 25:
            print(f"  … and {len(differing_detail) - 25} more")

    print()
    return identical_total, differing_total


def search_count(
    target: str,
    resource_type: str,
    auth: TokenSource | dict[str, str],
    want: int = 0,
    settle_seconds: int = 0,
) -> int | None:
    """Search-based count, optionally polled until it reaches `want`.

    HAPI refreshes its Lucene index asynchronously unless
    `synchronization.strategy=sync` is set, so a resource that was just written
    successfully can be invisible to search for tens of seconds. Measured against
    this target: 40s for a single CodeSystem. Polling avoids reporting a healthy
    write as a failure. See the HAPI async-indexing section in CLAUDE.md.
    """
    url = f"{target.rstrip('/')}/{resource_type}?_summary=count"
    deadline = time.monotonic() + max(0, settle_seconds)
    total: int | None = None
    while True:
        try:
            status, payload, _ = _request_target(url, auth)
        except TransferError:
            return None
        if status >= 400 or payload is None:
            return None
        candidate = payload.get("total")
        total = candidate if isinstance(candidate, int) else None
        if total is None or total >= want or time.monotonic() >= deadline:
            return total
        time.sleep(5)


def verify_written(
    target: str,
    resource_type: str,
    ids: list[str],
    auth: TokenSource | dict[str, str],
) -> tuple[int, list[str]]:
    """Direct-read each id. Returns (ok_count, missing_ids).

    `GET {Type}/{id}` bypasses the search index entirely, so this is the
    authoritative check that content actually landed — the triage rule from
    CLAUDE.md: if the direct read shows the data and search does not, the index
    is stale and the write was fine.
    """
    base = target.rstrip("/")
    ok = 0
    missing: list[str] = []
    for rid in ids:
        status, _, _ = _request_target(f"{base}/{resource_type}/{urllib.parse.quote(rid, safe='')}", auth)
        if status == 200:
            ok += 1
        else:
            missing.append(f"{rid} (HTTP {status})")
    return ok, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="Target FHIR base URL (e.g. https://example.org/fhir)")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"Source FHIR base URL (default: {DEFAULT_SOURCE})")
    parser.add_argument(
        "--types",
        default=",".join(DEFAULT_TYPES),
        help="Comma-separated resource types, in write order",
    )
    parser.add_argument("--chunk-size", type=int, default=25, help="Max entries per batch Bundle (default: 25)")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Copy at most N resources of each type. For smoke-testing the write path "
        "against a real target before committing to the full set. 0 = no limit.",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=5,
        help="Direct-read this many resources per type after writing (default: 5)",
    )
    parser.add_argument(
        "--verify-all",
        action="store_true",
        help="Direct-read every written resource instead of a sample. Slower, exhaustive.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=60,
        help="How long to let the target's search index catch up before reporting its "
        "count (default: 60). Does not affect pass/fail — direct reads decide that.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Read from source and report, write nothing")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run the read-only collision check against the target and exit. Writes nothing.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Required to proceed when the collision check finds target resources whose "
        "content differs from the source. Without it, the run aborts before writing.",
    )
    parser.add_argument(
        "--source-token-env",
        default="SOURCE_FHIR_TOKEN",
        help="Env var holding the source bearer token, if it needs one",
    )
    parser.add_argument("--token-env", default="TARGET_FHIR_TOKEN", help="Env var holding the target bearer token")
    parser.add_argument(
        "--token-url",
        default=None,
        help="OAuth2 token endpoint for the client_credentials grant. Defaults to the "
        "target's origin + /token. Use --no-token-url to disable auto-refresh.",
    )
    parser.add_argument(
        "--no-token-url",
        action="store_true",
        help="Never mint tokens; use only the supplied token and fail when it expires.",
    )
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=f"Cache file for the access token, written 0600 (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--keychain-service",
        default=DEFAULT_KEYCHAIN_SERVICE,
        help=f"macOS keychain service holding client_id/client_secret (default: {DEFAULT_KEYCHAIN_SERVICE})",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="Mint a fresh access token, cache it, and exit. Touches no FHIR content.",
    )
    args = parser.parse_args()

    types = [t.strip() for t in args.types.split(",") if t.strip()]

    token_url = None
    if not args.no_token_url:
        token_url = args.token_url or _default_token_url(args.target)
    cache_path = pathlib.Path(args.token_file).expanduser() if args.token_file else None

    target_auth = TokenSource(
        token_url=token_url,
        cache_path=cache_path,
        keychain_service=args.keychain_service,
        token_env=args.token_env,
        id_env="TARGET_CLIENT_ID",
        secret_env="TARGET_CLIENT_SECRET",
    )

    source_token = os.environ.get(args.source_token_env, "").strip()
    source_auth = {"Authorization": f"Bearer {source_token}"} if source_token else {}

    print(f"source: {args.source}")
    print(f"target: {args.target}{'  (DRY RUN — nothing will be written)' if args.dry_run else ''}")
    print(f"token:  endpoint {token_url or '(auto-refresh disabled)'}")
    print(
        f"        credentials {'available' if target_auth.client_credentials() else 'NOT found'}"
        f" (env TARGET_CLIENT_ID/SECRET or keychain '{args.keychain_service}')"
    )

    if args.refresh_token:
        try:
            target_auth.mint()
        except TransferError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Token cached at {cache_path}. Nothing else was done.")
        return 0

    # Resolve a token now so its state is visible up front. Not having one is not
    # fatal — unauthenticated targets exist — so preflight makes the real call.
    try:
        target_auth.token()
    except AuthError as exc:
        print(f"        no token yet: {exc}")
    print()

    # Prove the credentials work before reading 185 resources or touching anything.
    try:
        preflight_target(args.target, types[0] if types else "Measure", target_auth)
    except TransferError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("  target preflight: search OK\n")

    # Read everything up front so a source-side failure aborts before any write.
    inventory: dict[str, list[dict[str, Any]]] = {}
    for resource_type in types:
        try:
            found = fetch_all(args.source, resource_type, source_auth)
        except TransferError as exc:
            print(f"ERROR reading {resource_type}: {exc}", file=sys.stderr)
            return 1
        if args.limit > 0 and len(found) > args.limit:
            print(f"  read {len(found):>4} {resource_type}  -> limited to {args.limit}")
            found = found[: args.limit]
        else:
            print(f"  read {len(found):>4} {resource_type}")
        inventory[resource_type] = found
    total_read = sum(len(v) for v in inventory.values())
    print(f"  {'-' * 24}\n  read {total_read:>4} resources total\n")

    if args.dry_run and not args.check_only:
        for resource_type in types:
            ids = [r["id"] for r in inventory[resource_type]]
            preview = ", ".join(ids[:5]) + (f", … (+{len(ids) - 5})" if len(ids) > 5 else "")
            print(f"{resource_type}: {preview}")
        print()

    # Always look before writing. This is GET-only against the target.
    try:
        _, differing = collision_check(args.target, inventory, types, target_auth)
    except TransferError as exc:
        print(f"ERROR during collision check: {exc}", file=sys.stderr)
        print("Nothing was written. An unreadable target is not an empty target.", file=sys.stderr)
        return 2

    if args.check_only:
        print("Check-only run complete. Nothing written.")
        return 0
    if args.dry_run:
        print("Dry run complete. Nothing written.")
        return 0
    if differing and not args.overwrite:
        print(
            f"ABORTED: {differing} target resource(s) hold different content at these IDs.\n"
            f"Nothing was written. Review the list above, then re-run with --overwrite to replace them.",
            file=sys.stderr,
        )
        return 3

    all_failed: list[tuple[str, str, str]] = []
    total_ok = 0
    started = time.monotonic()

    for resource_type in types:
        resources = inventory[resource_type]
        if not resources:
            continue
        chunks = [resources[i : i + args.chunk_size] for i in range(0, len(resources), args.chunk_size)]
        ok_count = 0
        for idx, chunk in enumerate(chunks, 1):
            try:
                ok, failed = push_chunk(args.target, chunk, target_auth)
            except TransferError as exc:
                print(
                    f"ERROR writing {resource_type} chunk {idx}/{len(chunks)}: {exc}",
                    file=sys.stderr,
                )
                return 1
            ok_count += len(ok)
            all_failed.extend(failed)
            print(
                f"  {resource_type} chunk {idx}/{len(chunks)}: {len(ok)} ok, {len(failed)} failed",
                flush=True,
            )
        total_ok += ok_count
        print(f"  → {resource_type}: {ok_count}/{len(resources)} written\n")

    elapsed = time.monotonic() - started
    print(f"{'=' * 46}")
    print(f"wrote {total_ok}/{total_read} resources in {elapsed:.1f}s")

    if all_failed:
        print(f"\n{len(all_failed)} FAILED:")
        for ref, status, detail in all_failed[:40]:
            print(f"  {ref:<50} HTTP {status}  {detail}")
        if len(all_failed) > 40:
            print(f"  … and {len(all_failed) - 40} more")

    # Direct reads decide pass/fail. The search count is informational only: it
    # lags the write by design and is not evidence of anything when it is low.
    print("\nverifying on target (direct reads are authoritative):")
    missing_total: list[str] = []
    for resource_type in types:
        resources = inventory[resource_type]
        if not resources:
            continue
        ids = [r["id"] for r in resources]
        sample = ids if args.verify_all else ids[: min(len(ids), args.verify_sample)]
        ok, missing = verify_written(args.target, resource_type, sample, target_auth)
        missing_total.extend(f"{resource_type}/{m}" for m in missing)

        counted = search_count(
            args.target,
            resource_type,
            target_auth,
            want=len(resources),
            settle_seconds=args.settle_seconds,
        )
        if counted is None:
            count_note = "search count unavailable"
        elif counted >= len(resources):
            count_note = f"search count {counted}"
        else:
            count_note = f"search count {counted} (index still catching up, not a failure)"
        print(f"  {resource_type:<12} direct reads {ok}/{len(sample)} OK, {count_note}")

    if missing_total:
        print(f"\n{len(missing_total)} resource(s) could NOT be read back:")
        for ref in missing_total[:20]:
            print(f"  {ref}")

    return 1 if (all_failed or missing_total) else 0


if __name__ == "__main__":
    sys.exit(main())
