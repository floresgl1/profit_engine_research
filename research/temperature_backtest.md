# Temperature model backtest: KXHIGHNY

Run 2026-10-04. Train 2024-08-01 to 2025-07-31 (1095 cases), test 2025-08-01 to 2026-10-01 (1281 cases). A case is one target day at one decision time.

## Fitted on the training period

Residual = actual high - NBM forecast, before using observations.

| Lead | n | Bias °F | Fixed sigma °F | Scaled k (sigma = k * xnd) |
|---|---|---|---|---|
| D-1 16:00 | 365 | +0.37 | 2.63 | 1.50 |
| D 10:00 | 365 | +0.36 | 2.51 | 1.48 |
| D 14:00 | 365 | +0.36 | 2.51 | 1.48 |

## Test period

Brier on bucket markets where the market had a two-sided price at the decision time; model and market scored on exactly the same rows. Log score: mean log probability of the realized high (higher is better).

| Variant | Lead | Cases | Log score | Bucket rows | Model Brier | Market Brier | Skill vs market |
|---|---|---|---|---|---|---|---|
| fixed | D-1 16:00 | 427 | -2.493 | 2317 | 0.1421 | 0.1195 | -0.190 |
| fixed | D 10:00 | 427 | -2.327 | 1670 | 0.1795 | 0.1344 | -0.336 |
| fixed | D 14:00 | 427 | -2.204 | 1079 | 0.2383 | 0.1194 | -0.996 |
| fixed | all | 1281 | -2.341 | 5066 | 0.1749 | 0.1244 | -0.407 |
| scaled | D-1 16:00 | 427 | -2.419 | 2317 | 0.1405 | 0.1195 | -0.176 |
| scaled | D 10:00 | 427 | -2.274 | 1670 | 0.1759 | 0.1344 | -0.309 |
| scaled | D 14:00 | 427 | -2.157 | 1079 | 0.2348 | 0.1194 | -0.967 |
| scaled | all | 1281 | -2.283 | 5066 | 0.1722 | 0.1244 | -0.385 |

Better variant on held-out log score: **scaled**.

Model calibration (scaled, all leads, same rows as market)
  bucket        n   mean_pred  hit_rate
  [0.0, 0.1)    972       0.047     0.078
  [0.1, 0.2)   1539       0.147     0.191
  [0.2, 0.3)   1698       0.241     0.285
  [0.3, 0.4)    364       0.366     0.371
  [0.4, 0.5)    386       0.455     0.547
  [0.5, 0.6)     65       0.524     0.538
  [0.6, 0.7)     24       0.641     0.500
  [0.7, 0.8)      8       0.737     0.500
  [0.8, 0.9)      5       0.849     0.600
  [0.9, 1.0]      5       0.973     0.400

Market calibration (same rows)
  bucket        n   mean_pred  hit_rate
  [0.0, 0.1)   1888       0.043     0.029
  [0.1, 0.2)    743       0.144     0.129
  [0.2, 0.3)    592       0.248     0.225
  [0.3, 0.4)    587       0.346     0.324
  [0.4, 0.5)    494       0.446     0.468
  [0.5, 0.6)    281       0.544     0.616
  [0.6, 0.7)    168       0.640     0.619
  [0.7, 0.8)    100       0.745     0.790
  [0.8, 0.9)    100       0.849     0.860
  [0.9, 1.0]    113       0.947     0.973

Parameters for the live model written to `research/temperature_params.json`.
