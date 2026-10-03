"""Decides what to fetch from each venue and how often.

Requirements carried from core design:
- Catch only `core.InvalidOrderBook` per snapshot: log it and skip it. Any
  other exception is a bug and must stop the loop.
- Track the skip rate per venue and alert when it is too high. A high rate
  usually means an adapter bug (e.g. a wrong NO->YES flip), not bad venue data.
"""
