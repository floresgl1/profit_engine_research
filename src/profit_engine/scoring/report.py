"""Plain-text rendering of scores for the CLI."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from profit_engine.scoring.brier import (
    CalibrationBucket,
    Comparison,
    Selection,
    calibration_table,
    compare,
    market_pairs,
    model_pairs,
    select,
)
from profit_engine.storage import ScoredRow


def _f(value: Decimal | None, places: int = 4) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def format_comparison(c: Comparison) -> str:
    skill = c.skill
    verdict = "-" if skill is None else ("model better" if skill > 0 else "market better" if skill < 0 else "tie")
    return (
        f"{c.model} [{c.selection.value}]: {c.n_predictions} predictions on {c.n_markets} markets "
        f"({c.n_excluded} excluded: no two-sided price)\n"
        f"  Brier  model {_f(c.model_brier)}  market {_f(c.market_brier)}  "
        f"skill {_f(skill)} ({verdict})"
    )


def format_calibration(title: str, table: Sequence[CalibrationBucket]) -> str:
    lines = [title, "  bucket        n   mean_pred  hit_rate"]
    for b in table:
        lines.append(
            f"  [{b.lower:.1f}, {b.upper:.1f}{']' if b.upper == 1 else ')'}  {b.count:5d}   "
            f"{_f(b.mean_predicted, 3):>9}  {_f(b.hit_rate, 3):>8}"
        )
    return "\n".join(lines)


def full_report(rows: Sequence[ScoredRow], models: Sequence[str], n_buckets: int = 10) -> str:
    if not rows:
        return "No resolved predictions yet. Predictions are scored once their market resolves."
    sections = []
    for model in models:
        own = [r for r in rows if r.prediction.model == model]
        if not own:
            continue
        for selection in Selection:
            try:
                sections.append(format_comparison(compare(rows, model, selection)))
            except ValueError as exc:
                sections.append(f"{model} [{selection.value}]: {exc}")
        # Same rows for both tables: last prediction per market, two-sided price only.
        last = [r for r in select(own, Selection.LAST) if r.prediction.market_probability is not None]
        title = f"{model} calibration (last per market)"
        sections.append(format_calibration(title, calibration_table(model_pairs(last), n_buckets)))
        sections.append(format_calibration("market calibration (same rows)", calibration_table(market_pairs(last), n_buckets)))
    return "\n\n".join(sections)
