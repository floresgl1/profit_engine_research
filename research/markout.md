# Market-maker markouts, Kalshi daily-high markets

280 events (40 per city, evenly spaced 2025-08-01 to 2026-10-01), 1,426,986 non-block trades. Maker PnL per contract in cents, volume-weighted, ± SE clustered by event. Entry edge = vs the hourly mid, only for trades within 10 min after it; 1 h markout = vs the first hourly mid an hour or more later; settlement = held to the outcome; last column subtracts a maker fee of 0.0175 x P x (1-P).

### All trades

| Group | Trades | Contracts | Entry edge | 1 h markout | Settlement | Settlement, maker fee |
|---|---|---|---|---|---|---|
| All | 1426986 | 51,352,171 | +2.62 ± 0.22 | +0.19 ± 0.11 | +0.78 ± 0.28 | +0.64 ± 0.28 |

### By session (local time)

| Group | Trades | Contracts | Entry edge | 1 h markout | Settlement | Settlement, maker fee |
|---|---|---|---|---|---|---|
| day before | 269152 | 4,962,401 | +2.12 ± 0.16 | +0.41 ± 0.07 | +0.53 ± 0.36 | +0.30 ± 0.36 |
| D 00-10 | 432350 | 11,217,166 | +2.07 ± 0.19 | +0.35 ± 0.15 | +0.31 ± 0.81 | +0.13 ± 0.81 |
| D 10-14 | 417525 | 14,164,785 | +3.03 ± 0.29 | +0.32 ± 0.21 | +1.10 ± 0.28 | +0.95 ± 0.27 |
| D 14-18 | 265479 | 14,574,058 | +3.19 ± 0.73 | -0.73 ± 0.47 | +1.02 ± 0.31 | +0.93 ± 0.31 |
| D 18-close | 42480 | 6,433,761 | +1.51 ± 0.57 | +0.36 ± 0.89 | +0.53 ± 0.54 | +0.50 ± 0.54 |

### By trade price

| Group | Trades | Contracts | Entry edge | 1 h markout | Settlement | Settlement, maker fee |
|---|---|---|---|---|---|---|
| 00-5c | 239803 | 24,729,788 | +0.34 ± 0.75 | -1.05 ± 0.71 | +0.50 ± 0.34 | +0.47 ± 0.34 |
| 05-20c | 361268 | 7,971,093 | +2.20 ± 0.14 | +0.51 ± 0.15 | +0.99 ± 0.44 | +0.83 ± 0.44 |
| 20-50c | 466896 | 7,773,580 | +3.16 ± 0.19 | +0.22 ± 0.26 | +0.73 ± 0.53 | +0.36 ± 0.53 |
| 50-80c | 243883 | 4,034,676 | +5.29 ± 0.53 | +1.08 ± 0.74 | +0.89 ± 1.04 | +0.49 ± 1.03 |
| 80-95c | 70406 | 1,804,821 | +4.00 ± 0.66 | +1.87 ± 0.77 | +2.95 ± 1.49 | +2.77 ± 1.47 |
| 95-100c | 44730 | 5,038,212 | +2.89 ± 0.64 | -0.54 ± 0.81 | +1.03 ± 0.30 | +1.00 ± 0.30 |

### By city

| Group | Trades | Contracts | Entry edge | 1 h markout | Settlement | Settlement, maker fee |
|---|---|---|---|---|---|---|
| KXHIGHAUS | 150211 | 3,575,916 | +2.18 ± 0.90 | +0.45 ± 0.20 | +0.33 ± 0.37 | +0.15 ± 0.37 |
| KXHIGHCHI | 209892 | 6,578,432 | +3.25 ± 0.63 | -0.47 ± 0.42 | +0.58 ± 0.27 | +0.45 ± 0.27 |
| KXHIGHDEN | 132963 | 3,247,049 | +4.43 ± 1.01 | -0.03 ± 0.23 | +0.32 ± 0.43 | +0.16 ± 0.42 |
| KXHIGHLAX | 346309 | 15,808,688 | +1.58 ± 0.39 | +0.60 ± 0.17 | +1.16 ± 0.17 | +1.03 ± 0.17 |
| KXHIGHMIA | 198168 | 7,730,032 | +2.59 ± 0.36 | -0.10 ± 0.25 | +0.83 ± 0.28 | +0.72 ± 0.28 |
| KXHIGHNY | 284691 | 10,034,562 | +3.18 ± 0.46 | +0.34 ± 0.25 | +1.53 ± 0.47 | +1.38 ± 0.46 |
| KXHIGHPHIL | 104752 | 4,377,492 | +3.14 ± 0.61 | -0.29 ± 0.41 | -1.41 ± 2.97 | -1.52 ± 2.97 |

