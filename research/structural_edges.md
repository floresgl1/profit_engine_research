# Structural edges in Kalshi daily-high events

Test period 2025-08-01 to 2026-10-01, hourly candle closes, fee 0.07 x P x (1-P) per contract.

## 1. Event arbitrage

Buy-all edge = 1 - sum(ask + fee); sell-all edge = (n-1) - sum((1-bid) + fee). Positive = free money.

| City | Event-hours | Median ask sum | Buy-all > 0 | Best buy-all | Median bid sum | Sell-all > 0 | Best sell-all |
|---|---|---|---|---|---|---|---|
| KXHIGHNY | 12778 | 1.080 | 5 of 10483 | +0.0634 | 0.990 | 35 of 2845 | +0.0308 |
| KXHIGHCHI | 13228 | 1.070 | 19 of 10878 | +0.0612 | 0.970 | 29 of 4675 | +0.0256 |
| KXHIGHAUS | 12546 | 1.090 | 18 of 10548 | +0.0497 | 0.970 | 51 of 3865 | +0.0336 |
| KXHIGHMIA | 12377 | 1.070 | 7 of 9701 | +0.0117 | 0.980 | 29 of 2286 | +0.0290 |
| KXHIGHLAX | 13527 | 1.070 | 2 of 10396 | +0.0159 | 0.990 | 67 of 3475 | +0.0505 |
| KXHIGHDEN | 12679 | 1.080 | 17 of 10591 | +0.1121 | 0.970 | 82 of 5370 | +0.0609 |
| KXHIGHPHIL | 12208 | 1.090 | 29 of 10237 | +0.0901 | 0.950 | 29 of 4172 | +0.0190 |

Positive event-hours (largest first, up to 20):

| Event | Local hour | Buy-all | Sell-all |
|---|---|---|---|
| KXHIGHDEN-25NOV25 | 01:00 | +0.1121 | - |
| KXHIGHDEN-25SEP03 | 02:00 | +0.1085 | - |
| KXHIGHPHIL-25NOV27 | 00:00 | +0.0901 | - |
| KXHIGHDEN-25SEP03 | 04:00 | +0.0779 | - |
| KXHIGHPHIL-26FEB01 | 05:00 | +0.0727 | -0.2720 |
| KXHIGHNY-25SEP23 | 03:00 | +0.0634 | -0.2517 |
| KXHIGHCHI-26MAY21 | 02:00 | +0.0612 | -0.3028 |
| KXHIGHDEN-26MAY06 | 14:00 | -0.2841 | +0.0609 |
| KXHIGHLAX-26FEB09 | 16:00 | -0.2947 | +0.0505 |
| KXHIGHAUS-26JUN02 | 12:00 | +0.0497 | - |
| KXHIGHPHIL-26JUL02 | 06:00 | +0.0470 | - |
| KXHIGHCHI-26MAY21 | 10:00 | +0.0458 | - |
| KXHIGHLAX-26MAR17 | 01:00 | -0.2342 | +0.0388 |
| KXHIGHDEN-26JUN22 | 19:00 | -0.2173 | +0.0356 |
| KXHIGHPHIL-25AUG01 | 20:00 | +0.0338 | - |
| KXHIGHAUS-26MAY23 | 14:00 | -0.3326 | +0.0336 |
| KXHIGHDEN-26MAR23 | 14:00 | -0.2098 | +0.0328 |
| KXHIGHPHIL-26JAN29 | 02:00 | +0.0327 | -0.2329 |
| KXHIGHDEN-26JAN28 | 22:00 | -0.2739 | +0.0310 |
| KXHIGHNY-26JAN14 | 17:00 | -0.2127 | +0.0308 |

## 2. Dead and certain buckets

Bound = rounded max reading so far (01:00 to midnight local) + the station's smallest training undercount.
Dead: bucket entirely below the bound, still bid. Certain: 'greater' bucket whose floor is below the bound, still offered below $1.

| City | Kind | Quotes | Bound wrong | Profit >= 1c | Profit >= 3c | Median price | Realized, summed |
|---|---|---|---|---|---|---|---|
| KXHIGHNY | dead | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHNY | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHCHI | dead | 4 | 0 | 4 | 4 | 0.550 | +1.51 |
| KXHIGHCHI | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHAUS | dead | 6 | 0 | 0 | 0 | 0.010 | +0.06 |
| KXHIGHAUS | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHMIA | dead | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHMIA | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHLAX | dead | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHLAX | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHDEN | dead | 6 | 0 | 3 | 3 | 0.040 | +0.32 |
| KXHIGHDEN | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |
| KXHIGHPHIL | dead | 1 | 0 | 1 | 0 | 0.020 | +0.02 |
| KXHIGHPHIL | certain | 0 | 0 | 0 | 0 | 0.000 | +0.00 |

By local hour, all cities (quotes with profit >= 1c; realized from the settled value):

| Local hour | Bound held | Bound failed | Realized per contract, summed |
|---|---|---|---|
| 02:00 | 1 | 0 | +0.05 |
| 13:00 | 1 | 0 | +0.11 |
| 14:00 | 2 | 0 | +0.34 |
| 15:00 | 2 | 0 | +0.55 |
| 16:00 | 1 | 0 | +0.56 |
| 17:00 | 1 | 0 | +0.21 |

## Skipped events

- KXHIGHNY: {'no settlement value': 68, 'not a partition': 7}
- KXHIGHCHI: {'no settlement value': 64, 'not a partition': 7}
- KXHIGHAUS: {'no settlement value': 68, 'not a partition': 7}
- KXHIGHMIA: {'no settlement value': 74, 'not a partition': 7}
- KXHIGHLAX: {'no settlement value': 62, 'not a partition': 7}
- KXHIGHDEN: {'no settlement value': 62, 'not a partition': 7}
- KXHIGHPHIL: {'no settlement value': 63, 'crossed quote (event-hours)': 15, 'not a partition': 7}
