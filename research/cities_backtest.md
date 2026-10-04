# v3 temperature model across Kalshi cities

Run 2026-10-04. Train 2024-08-01 to 2025-07-31, test 2025-08-01 to 2026-10-01. Skill = 1 - model Brier / market Brier on identical rows (positive beats the market). NYC results: research/temperature_backtest_v3.md.

## Chicago (Midway) (KXHIGHCHI, settles on CLIMDW)

Train 1457 cases, test 1707 cases. Undercount days: 193 DST, 125 standard time.

| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | +1.25 | 2.75 | 0.00 | 426 | 2424 | 0.1403 | 0.1228 | -0.143 | -2.467 |
| D 10:00 | +1.00 | 2.00 | 0.50 | 427 | 1838 | 0.1505 | 0.1317 | -0.143 | -2.058 |
| D 12:00 | +1.00 | 2.00 | 0.50 | 427 | 1576 | 0.1636 | 0.1396 | -0.172 | -1.920 |
| D 14:00 | +0.75 | 1.50 | 0.75 | 427 | 1146 | 0.1645 | 0.1284 | -0.281 | -1.546 |
| all |  |  |  | 1707 | 6984 | 0.1522 | 0.1299 | -0.172 | -1.997 |

