"""Transient API failures are retried with backoff; client errors are not."""

from __future__ import annotations

import io
import urllib.error

import pytest

from forge.providers import _http
from forge.providers._http import post_json
from forge.providers.base import ProviderError


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: str = "boom", headers: dict = None):
    return urllib.error.HTTPError(
        "http://x", code, "err", headers or {}, io.BytesIO(body.encode())
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Make backoff instant in tests, and record that it was called.
    calls = []
    monkeypatch.setattr(_http, "_sleep", lambda s: calls.append(s))
    return calls


def _patch_urlopen(monkeypatch, behaviors):
    """behaviors: list of callables or values; each call pops the next."""

    seq = list(behaviors)
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        b = seq.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def test_retries_on_500_then_succeeds(monkeypatch, _no_sleep):
    calls = _patch_urlopen(monkeypatch, [
        _http_error(500), _http_error(503), _Resp('{"ok": 1}'),
    ])
    out = post_json("http://x", {"a": 1}, base_delay=0.01)
    assert out == {"ok": 1}
    assert calls["n"] == 3          # two failures + one success
    assert len(_no_sleep) == 2      # slept before each retry


def test_retries_on_connection_error(monkeypatch, _no_sleep):
    calls = _patch_urlopen(monkeypatch, [
        urllib.error.URLError("connection refused"), _Resp('{"ok": 2}'),
    ])
    assert post_json("http://x", {}, base_delay=0.01) == {"ok": 2}
    assert calls["n"] == 2


def test_does_not_retry_on_401(monkeypatch, _no_sleep):
    calls = _patch_urlopen(monkeypatch, [_http_error(401, "bad key")])
    with pytest.raises(ProviderError) as exc:
        post_json("http://x", {})
    assert "401" in str(exc.value)
    assert calls["n"] == 1          # no retry on auth error
    assert _no_sleep == []


def test_gives_up_after_max_retries(monkeypatch, _no_sleep):
    calls = _patch_urlopen(monkeypatch, [_http_error(503)] * 10)
    with pytest.raises(ProviderError) as exc:
        post_json("http://x", {}, max_retries=2, base_delay=0.01)
    assert "503" in str(exc.value)
    assert calls["n"] == 3          # initial + 2 retries
    assert len(_no_sleep) == 2


def test_respects_retry_after_header(monkeypatch, _no_sleep):
    calls = _patch_urlopen(monkeypatch, [
        _http_error(429, headers={"Retry-After": "7"}), _Resp('{"ok": 3}'),
    ])
    assert post_json("http://x", {}, base_delay=0.01) == {"ok": 3}
    assert _no_sleep == [7.0]       # honored the server's backoff, not the jitter
