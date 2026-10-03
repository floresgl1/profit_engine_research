"""GET-only HTTP client shared by all venue adapters.

This is the only module that talks to the network. It exposes `get_json` and
nothing else: there is no way to send a POST/PUT/DELETE or a request body,
and it never sends credentials. That is what makes "no code path can place
an order" a structural property rather than a promise.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

QueryParams = Mapping[str, str | int | Sequence[str]]

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class VenueHttpError(RuntimeError):
    """A request failed after retries, or returned a non-retryable error."""


class ReadOnlyHttp:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        max_retries: int = 4,
        min_interval: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "profit-engine-research/0.1"},
            follow_redirects=False,
        )
        self._max_retries = max_retries
        self._min_interval = min_interval  # client-side politeness throttle, seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None

    def get_json(self, path: str, params: QueryParams | None = None) -> Any:
        """GET `path` and decode JSON. Retries 429/5xx/transport errors with backoff."""
        delay = 2.0
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise VenueHttpError(f"GET {path} failed: {exc}") from exc
                log.warning("GET %s transport error (%s), retrying in %.0fs", path, exc, delay)
                self._sleep(delay)
                delay *= 2
                continue

            if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                wait = _retry_after(response) or delay
                log.warning("GET %s -> %d, retrying in %.0fs", path, response.status_code, wait)
                self._sleep(wait)
                delay *= 2
                continue
            if response.status_code != 200:
                raise VenueHttpError(f"GET {path} -> {response.status_code}: {response.text[:200]}")
            return response.json()
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request is not None:
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_request = now


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
