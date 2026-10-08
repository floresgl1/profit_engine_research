"""Named paper market-making strategies, shared by the live maker and replay.

All quote 10 contracts at the touch with a 50-contract position limit and the
conservative maker fee; they differ in one rule each:

- join_touch_v1: nothing else (the baseline);
- model_veto_v2: no quote the temperature model puts 20c+ against us;
- skew_size_v3a: on the side that adds to the position, size shrinks with
  the position (full when flat, 0 at the limit);
- skew_back_v3b: v3a, and from half the limit on that side quotes one tick
  behind the best price.

v3 variants are replay-only until replay shows they fix the inventory losses.
"""

from __future__ import annotations

from decimal import Decimal

from profit_engine.maker.quoter import QuoterConfig
from profit_engine.maker.runner import Strategy

BASE = QuoterConfig()

STRATEGIES: dict[str, Strategy] = {
    "join_touch_v1": Strategy("join_touch_v1", BASE),
    "model_veto_v2": Strategy("model_veto_v2", QuoterConfig(model_margin=Decimal("0.20"))),
    "skew_size_v3a": Strategy("skew_size_v3a", QuoterConfig(skew_size=True)),
    "skew_back_v3b": Strategy("skew_back_v3b", QuoterConfig(skew_size=True, skew_back=True)),
}
LIVE = ["join_touch_v1", "model_veto_v2"]
