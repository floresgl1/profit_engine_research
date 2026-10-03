"""Polling loop: what to fetch from each venue, how often, and what to do with it.

Requirements carried from core design (implemented in `pipeline` and `monitor`):
- Catch only `core.InvalidOrderBook` per snapshot: log it and skip it. Any
  other exception is a bug and must stop the loop. (Venue network errors
  skip that venue for one cycle.)
- Track the skip rate per venue and alert when it is too high. A high rate
  usually means an adapter bug (e.g. a wrong NO->YES flip), not bad venue data.
"""

from profit_engine.ingest.monitor import SkipAlert, SkipMonitor
from profit_engine.ingest.pipeline import CycleStats, Pipeline, PipelineConfig

__all__ = ["CycleStats", "Pipeline", "PipelineConfig", "SkipAlert", "SkipMonitor"]
