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

## Austin (Bergstrom) (KXHIGHAUS, settles on CLIAUS)

Train 1457 cases, test 1707 cases. Undercount days: 218 DST, 115 standard time.

| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | +1.00 | 2.75 | 0.00 | 426 | 2372 | 0.1420 | 0.1214 | -0.169 | -2.433 |
| D 10:00 | +1.00 | 2.25 | 0.25 | 427 | 1909 | 0.1585 | 0.1252 | -0.266 | -2.226 |
| D 12:00 | +1.25 | 2.00 | 0.50 | 427 | 1774 | 0.1636 | 0.1270 | -0.289 | -2.117 |
| D 14:00 | +1.00 | 1.75 | 0.75 | 427 | 1488 | 0.1633 | 0.1258 | -0.298 | -1.795 |
| all |  |  |  | 1707 | 7543 | 0.1554 | 0.1246 | -0.248 | -2.142 |

## Miami (KXHIGHMIA, settles on CLIMIA)

Train 1456 cases, test 1707 cases. Undercount days: 208 DST, 124 standard time.

| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | +1.25 | 1.75 | 0.00 | 426 | 2237 | 0.1351 | 0.1175 | -0.150 | -2.071 |
| D 10:00 | +1.25 | 1.50 | 0.25 | 427 | 1763 | 0.1608 | 0.1372 | -0.172 | -1.942 |
| D 12:00 | +1.00 | 1.50 | 0.50 | 427 | 1442 | 0.1845 | 0.1422 | -0.298 | -1.747 |
| D 14:00 | +0.25 | 1.50 | 0.50 | 427 | 920 | 0.1751 | 0.1045 | -0.675 | -1.312 |
| all |  |  |  | 1707 | 6362 | 0.1592 | 0.1267 | -0.257 | -1.768 |

## Los Angeles (LAX) (KXHIGHLAX, settles on CLILAX)

Train 1458 cases, test 1707 cases. Undercount days: 215 DST, 118 standard time.

| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | +1.75 | 2.50 | 0.00 | 426 | 2291 | 0.1340 | 0.1182 | -0.134 | -2.331 |
| D 10:00 | +1.75 | 1.75 | 0.50 | 427 | 1775 | 0.1451 | 0.1272 | -0.141 | -1.934 |
| D 12:00 | +1.00 | 1.50 | 0.50 | 427 | 1235 | 0.1380 | 0.1164 | -0.186 | -1.508 |
| D 14:00 | -1.00 | 2.50 | 0.00 | 427 | 761 | 0.1538 | 0.0710 | -1.165 | -1.218 |
| all |  |  |  | 1707 | 6062 | 0.1406 | 0.1145 | -0.227 | -1.747 |

## Denver (DIA) (KXHIGHDEN, settles on CLIDEN)

Train 1457 cases, test 1707 cases. Undercount days: 211 DST, 116 standard time.

| Lead | Bias | Sigma | Alpha | Test cases | Rows | Model Brier | Market Brier | Skill | Log score |
|---|---|---|---|---|---|---|---|---|---|
| D-1 16:00 | +2.25 | 3.25 | 0.00 | 426 | 2447 | 0.1462 | 0.1189 | -0.229 | -2.674 |
| D 10:00 | +1.75 | 2.50 | 0.25 | 427 | 1979 | 0.1582 | 0.1273 | -0.242 | -2.446 |
| D 12:00 | +1.75 | 2.00 | 0.50 | 427 | 1730 | 0.1612 | 0.1334 | -0.209 | -2.205 |
| D 14:00 | +1.00 | 1.75 | 0.75 | 427 | 1314 | 0.1635 | 0.1256 | -0.301 | -1.757 |
| all |  |  |  | 1707 | 7470 | 0.1559 | 0.1257 | -0.240 | -2.270 |

