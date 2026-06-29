"""Minimal JSON-over-HTTP helper built on stdlib `urllib`, with retries.

Forge deliberately avoids the official provider SDKs so it runs with nothing but
PyYAML. All hosted adapters POST JSON here, so this is the one place to handle
transient API failures: rate limits and 5xx errors and dropped connections are
retried with exponential backoff (honoring `Retry-After`); client errors like
401/403/404 are NOT retried (those are bugs/bad keys, retrying wastes time/money).
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Optional

from forge.providers.base import ProviderError

# Transient HTTP statuses worth retrying. 429 = rate limit; 5xx = server-side.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DEFAULT_MAX_RETRIES = 4
DEFAULT_BASE_DELAY = 1.0
MAX_DELAY = 30.0

# Indirection so tests can stub out the sleep without real delays.
_sleep = time.sleep


def _backoff_delay(attempt: int, base_delay: float, exc=None) -> float:
    """Seconds to wait before the next attempt (0-based `attempt`).

    Honors a numeric `Retry-After` header when present (429/503); otherwise
    exponential backoff with full jitter, capped at MAX_DELAY.
    """

    if exc is not None and getattr(exc, "headers", None) is not None:
        retry_after = exc.headers.get("Retry-After")
        if retry_after and str(retry_after).strip().isdigit():
            return min(float(retry_after), MAX_DELAY)
    return min(base_delay * (2 ** attempt) + random.uniform(0, base_delay), MAX_DELAY)


def post_json(
    url: str,
    payload: dict,
    headers: Optional[dict] = None,
    timeout: float = 600.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
) -> dict:
    """POST `payload` as JSON and decode the JSON response, retrying transients.

    Raises `ProviderError` once retries are exhausted (or immediately for a
    non-retryable status), with the server body attached.
    """

    data = json.dumps(payload).encode("utf-8")
    body: Optional[str] = None
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            break  # success
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if exc.code in RETRYABLE_STATUS and attempt < max_retries:
                _sleep(_backoff_delay(attempt, base_delay, exc))
                last_exc = exc
                continue
            raise ProviderError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:  # timeout / connection refused / DNS
            if attempt < max_retries:
                _sleep(_backoff_delay(attempt, base_delay))
                last_exc = exc
                continue
            raise ProviderError(f"could not reach {url}: {exc.reason}") from exc

    if body is None:  # defensive; the loop always breaks or raises
        raise ProviderError(f"request to {url} failed after retries: {last_exc}")

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"non-JSON response from {url}: {body[:500]}") from exc
