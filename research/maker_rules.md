# Maker fills vs the temperature model

628,567 maker fills in the 2 h after each model decision time, 280 events. PnL per contract in cents (held to settlement, maker fee 0.0175 x P x (1-P)), volume-weighted, ± SE clustered by event. Model edge = maker side x (model prob - price).

## Maker PnL by model edge

| Model edge | Fills | Contracts | PnL per contract | Discovery | Holdout |
|---|---|---|---|---|---|
| -100 to -10c | 154941 | 6,676,077 | -0.81 ± 0.85 | -0.61 | -0.99 |
| -10 to -5c | 46695 | 1,676,686 | -0.27 ± 0.81 | +0.55 | -1.40 |
| -5 to -2c | 34837 | 1,249,319 | +1.10 ± 0.99 | +1.47 | +0.43 |
| -2 to +2c | 76487 | 6,562,152 | +0.83 ± 0.18 | +0.90 | +0.72 |
| +2 to +5c | 38901 | 1,025,850 | +0.66 ± 0.57 | +0.92 | +0.39 |
| +5 to +10c | 52336 | 1,122,332 | +1.48 ± 0.95 | +0.97 | +1.98 |
| +10 to +100c | 224370 | 4,983,589 | +3.82 ± 1.44 | +2.60 | +4.95 |

## Rule: skip fills the model puts below -m

| m | Half | Contracts kept | PnL per contract | Total PnL ($ per contract-size 1) |
|---|---|---|---|---|
| v1 (none) | discovery | 12,190,829 | +0.87 ± 0.32 | +105,867 |
| v1 (none) | holdout | 11,105,176 | +1.05 ± 0.25 | +116,841 |
| 20c | discovery | 10,223,912 | +1.26 ± 0.46 | +128,753 |
| 20c | holdout | 8,329,999 | +1.80 ± 0.60 | +150,319 |
| 10c | discovery | 9,125,848 | +1.36 ± 0.55 | +124,436 |
| 10c | holdout | 7,494,080 | +2.04 ± 0.72 | +152,656 |
| 5c | discovery | 8,152,068 | +1.46 ± 0.61 | +119,102 |
| 5c | holdout | 6,791,174 | +2.39 ± 0.84 | +162,510 |
| 2c | discovery | 7,344,067 | +1.46 ± 0.67 | +107,208 |
| 2c | holdout | 6,349,856 | +2.53 ± 0.92 | +160,610 |
| 0c | discovery | 6,765,702 | +1.59 ± 0.73 | +107,801 |
| 0c | holdout | 5,718,945 | +2.80 ± 1.03 | +160,243 |

## By lead (all fills)

| Lead | Contracts | v1 PnL | PnL keeping model edge >= -5c |
|---|---|---|---|
| D-1 16:00 | 670,886 | +0.86 | +2.63 |
| D 10:00 | 6,030,362 | +0.68 | +1.64 |
| D 12:00 | 8,134,423 | +1.15 | +1.65 |
| D 14:00 | 8,460,335 | +0.97 | +2.28 |
