"""Skip-rate monitor: alert when too many snapshots per venue fail validation.

A high skip rate usually means an adapter bug (e.g. a wrong NO->YES flip
makes every book look crossed), not bad venue data. Without this, the loop
would log-and-skip forever while storing nothing.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkipAlert:
    venue: str
    skipped: int
    total: int

    @property
    def rate(self) -> float:
        return self.skipped / self.total


def log_alert(alert: SkipAlert) -> None:
    log.error(
        "ALERT %s: %d of the last %d snapshots skipped (%.0f%%). Check the adapter before trusting this data.",
        alert.venue,
        alert.skipped,
        alert.total,
        alert.rate * 100,
    )


class SkipMonitor:
    """Rolling window of the last `window` snapshot attempts per venue.

    Fires `on_alert` once when the skip rate rises above `threshold` (with at
    least `min_samples` attempts), and logs once when it falls back below.
    """

    def __init__(
        self,
        threshold: float = 0.2,
        window: int = 200,
        min_samples: int = 20,
        on_alert: Callable[[SkipAlert], None] = log_alert,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("threshold must be in (0, 1)")
        self.threshold = threshold
        self.window = window
        self.min_samples = min_samples
        self.on_alert = on_alert
        self._attempts: dict[str, deque[bool]] = {}
        self._alerting: set[str] = set()

    def record(self, venue: str, skipped: bool) -> None:
        attempts = self._attempts.setdefault(venue, deque(maxlen=self.window))
        attempts.append(skipped)
        if len(attempts) < self.min_samples:
            return
        n_skipped = sum(attempts)
        rate = n_skipped / len(attempts)
        if rate > self.threshold and venue not in self._alerting:
            self._alerting.add(venue)
            self.on_alert(SkipAlert(venue, n_skipped, len(attempts)))
        elif rate <= self.threshold and venue in self._alerting:
            self._alerting.discard(venue)
            log.warning("%s skip rate back to %.0f%%", venue, rate * 100)

    def rate(self, venue: str) -> float | None:
        attempts = self._attempts.get(venue)
        return sum(attempts) / len(attempts) if attempts else None

    def alerting(self, venue: str) -> bool:
        return venue in self._alerting
