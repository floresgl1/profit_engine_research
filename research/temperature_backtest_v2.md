# Temperature model backtest v2: LAMP, max of observed and remaining

Run 2026-10-04. Train 2024-08-01 to 2025-07-31 (1092 cases), test 2025-08-01 to 2026-10-01 (1280 cases with a LAMP forecast).

## Fitted v2 parameters (training period)

| Lead | n | Bias °F (added to LAMP's remaining max) | Sigma °F |
|---|---|---|---|
| D-1 16:00 | 364 | +1.25 | 2.50 |
| D 10:00 | 364 | +0.75 | 2.00 |
| D 14:00 | 364 | -0.25 | 2.00 |

## Test period: v1 vs v2 vs market on identical bucket rows

Log score: mean log probability of the realized high (higher is better). Skill = 1 - model Brier / market Brier (positive beats the market).

| Lead | Cases | v1 log | v2 log | Rows | v1 Brier | v2 Brier | Market Brier | v1 skill | v2 skill |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | 426 | -2.419 | -2.295 | 2312 | 0.1404 | 0.1349 | 0.1194 | -0.176 | -0.130 |
| D 10:00 | 427 | -2.274 | -1.948 | 1670 | 0.1759 | 0.1559 | 0.1344 | -0.309 | -0.160 |
| D 14:00 | 427 | -2.157 | -1.478 | 1079 | 0.2348 | 0.1604 | 0.1194 | -0.967 | -0.344 |
| all | 1280 | -2.283 | -1.907 | 5061 | 0.1722 | 0.1473 | 0.1243 | -0.385 | -0.184 |

v2 calibration (all leads, same rows as market)
  bucket        n   mean_pred  hit_rate
  [0.0, 0.1)   1370       0.045     0.050
  [0.1, 0.2)    944       0.150     0.128
  [0.2, 0.3)   1316       0.256     0.264
  [0.3, 0.4)    876       0.347     0.396
  [0.4, 0.5)    114       0.442     0.395
  [0.5, 0.6)    141       0.550     0.631
  [0.6, 0.7)    157       0.659     0.739
  [0.7, 0.8)     54       0.732     0.759
  [0.8, 0.9)     64       0.823     0.875
  [0.9, 1.0]     25       0.942     0.960

Market calibration (same rows)
  bucket        n   mean_pred  hit_rate
  [0.0, 0.1)   1886       0.043     0.029
  [0.1, 0.2)    743       0.144     0.129
  [0.2, 0.3)    591       0.248     0.225
  [0.3, 0.4)    586       0.346     0.323
  [0.4, 0.5)    493       0.446     0.469
  [0.5, 0.6)    281       0.544     0.616
  [0.6, 0.7)    168       0.640     0.619
  [0.7, 0.8)    100       0.745     0.790
  [0.8, 0.9)    100       0.849     0.860
  [0.9, 1.0]    113       0.947     0.973

v2 parameters written to `research/temperature_params_lamp.json`.
