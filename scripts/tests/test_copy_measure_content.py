"""Unit tests for scripts/copy_measure_content.py — pure logic, no network.

Run:
    python3 -m pytest scripts/tests/test_copy_measure_content.py -v

NOTE: CI's unit job runs `cd backend && pytest tests/`, so it does not collect
this file. Run it manually when touching the script.

The expiry cases here are regression guards. During development the collision
check reported "185 new, 0 collisions" against a server that was 401ing every
probe — an expired token made an unreadable target look like an empty one, which
is a green light to overwrite. `probe_existing` now treats only 404/410 as
absent, and `_cached_token` refuses to hand back an already-expired token.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import time

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "copy_measure_content.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("copy_measure_content", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cmc = _load_module()


def _jwt(exp: int) -> str:
    """Build an unsigned JWT carrying only an `exp` claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


class TestJwtExpiry:
    def test_reads_exp_claim(self):
        exp = int(time.time()) + 3600
        assert cmc._jwt_expiry(_jwt(exp)) == exp

    def test_reads_past_exp(self):
        exp = int(time.time()) - 3600
        assert cmc._jwt_expiry(_jwt(exp)) == exp

    @pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", "", "one.two"])
    def test_non_jwt_returns_none(self, token):
        """Opaque tokens have no readable expiry — None, not a crash."""
        assert cmc._jwt_expiry(token) is None


class TestCachedToken:
    """Token selection must never hand back something the server will reject."""

    def _source(self, monkeypatch, token: str) -> cmc.TokenSource:
        monkeypatch.setenv("TEST_TOKEN_ENV", token)
        return cmc.TokenSource(
            token_url=None,
            cache_path=None,
            keychain_service="unused",
            token_env="TEST_TOKEN_ENV",
            id_env="TEST_ID_ENV",
            secret_env="TEST_SECRET_ENV",
        )

    def test_expired_token_is_rejected(self, monkeypatch):
        src = self._source(monkeypatch, _jwt(int(time.time()) - 60))
        assert src._cached_token() is None

    def test_live_token_is_accepted(self, monkeypatch):
        src = self._source(monkeypatch, _jwt(int(time.time()) + 7200))
        assert src._cached_token() is not None

    def test_token_inside_skew_window_is_rejected(self, monkeypatch):
        """Refuse a token about to expire — a long push would die mid-run."""
        src = self._source(monkeypatch, _jwt(int(time.time()) + 30))
        assert src._cached_token() is None

    def test_opaque_token_is_accepted(self, monkeypatch):
        """Expiry unknowable — send it and let the server decide."""
        src = self._source(monkeypatch, "opaque-token-value")
        assert src._cached_token() is not None

    def test_cannot_refresh_without_credentials(self, monkeypatch):
        monkeypatch.delenv("TEST_ID_ENV", raising=False)
        monkeypatch.delenv("TEST_SECRET_ENV", raising=False)
        monkeypatch.setattr(cmc, "_keychain_get", lambda service, account: None)
        src = self._source(monkeypatch, "tok")
        src.token_url = "https://example.org/token"
        assert src.can_refresh is False

    def test_can_refresh_with_env_credentials(self, monkeypatch):
        monkeypatch.setenv("TEST_ID_ENV", "client-abc")
        monkeypatch.setenv("TEST_SECRET_ENV", "secret-xyz")
        src = self._source(monkeypatch, "tok")
        src.token_url = "https://example.org/token"
        assert src.can_refresh is True
        assert src.client_credentials() == ("client-abc", "secret-xyz")


class TestIdentity:
    """(url, version) decides whether an overwrite would replace real content."""

    def test_same_url_and_version_match(self):
        a = {"url": "http://x/vs", "version": "1.0"}
        b = {"url": "http://x/vs", "version": "1.0"}
        assert cmc._identity(a) == cmc._identity(b)

    def test_differing_version_does_not_match(self):
        a = {"url": "http://x/vs", "version": "1.0"}
        b = {"url": "http://x/vs", "version": "2.0"}
        assert cmc._identity(a) != cmc._identity(b)

    def test_missing_fields_normalize_to_empty(self):
        assert cmc._identity({}) == ("", "")


class TestNormalize:
    def test_backfills_library_url(self):
        out = cmc.normalize({"resourceType": "Library", "id": "L1"})
        assert out["url"] == "Library/L1"

    def test_leaves_existing_library_url(self):
        out = cmc.normalize({"resourceType": "Library", "id": "L1", "url": "http://real/url"})
        assert out["url"] == "http://real/url"

    def test_ignores_other_resource_types(self):
        out = cmc.normalize({"resourceType": "ValueSet", "id": "V1"})
        assert "url" not in out


class TestDefaultTokenUrl:
    def test_derives_origin_plus_token(self):
        assert cmc._default_token_url("https://example.org/fhir/") == "https://example.org/token"

    def test_ignores_deep_paths(self):
        assert cmc._default_token_url("https://example.org/a/b/fhir") == "https://example.org/token"
