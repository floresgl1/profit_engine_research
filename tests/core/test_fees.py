from decimal import ROUND_CEILING, ROUND_HALF_UP
from decimal import Decimal as D

import pytest

from profit_engine.core import ZERO_FEES, FeeSchedule, Level

# Kalshi: per-fill fee ceil'd to $0.000001, then the order's cash change is
# rounded against the trader to the balance precision.
KALSHI_DIRECT = FeeSchedule(D("0.07"), D("0.000001"), ROUND_CEILING, cash_quantum=D("0.0001"))
KALSHI_FCM = FeeSchedule(D("0.07"), D("0.000001"), ROUND_CEILING, cash_quantum=D("0.01"))
# Polymarket: per-fill fee rounded to 5 dp, no balance rounding.
POLY_CRYPTO = FeeSchedule(D("0.07"), D("0.00001"), ROUND_HALF_UP)
POLY_SPORTS = FeeSchedule(D("0.05"), D("0.00001"), ROUND_HALF_UP)
POLY_POLITICS = FeeSchedule(D("0.04"), D("0.00001"), ROUND_HALF_UP)


def L(price, size):
    return Level(D(price), D(size))


class TestKalshi:
    # Worked example from docs.kalshi.com/getting_started/fee_rounding:
    # 1 contract at $0.055, model fee 0.07 * 1 * 0.055 * 0.945 = 0.00363825.
    def test_fill_fee_ceils_to_6dp(self):
        assert KALSHI_FCM.fill_fee(D("0.055"), D("1")) == D("0.003639")

    def test_fcm_buy_matches_docs(self):
        # Docs: balance changes by -$0.06, trade fee + rounding fee = $0.005.
        assert KALSHI_FCM.order_fee([L("0.055", "1")], buying=True) == D("0.005")

    def test_direct_buy(self):
        # paid = ceil_4dp(0.055 + 0.003639) = 0.0587 -> fee = 0.0037
        assert KALSHI_DIRECT.order_fee([L("0.055", "1")], buying=True) == D("0.0037")

    def test_fcm_sell(self):
        # received = floor_cent(0.055 - 0.003639) = 0.05 -> fee = 0.005
        assert KALSHI_FCM.order_fee([L("0.055", "1")], buying=False) == D("0.005")

    def test_peak_at_half(self):
        # 100 contracts at 0.50: 0.07 * 100 * 0.25 = 1.75, already on every grid.
        assert KALSHI_DIRECT.order_fee([L("0.50", "100")], buying=True) == D("1.75")

    def test_multi_fill_order(self):
        # 17 @ 0.44: 0.07*17*0.44*0.56 = 0.293216
        # 20 @ 0.55: 0.07*20*0.55*0.45 = 0.3465
        #  3 @ 0.56: 0.07*3*0.56*0.44  = 0.051744
        # trade fees = 0.69146; gross = 7.48 + 11.00 + 1.68 = 20.16
        # paid = ceil_4dp(20.85146) = 20.8515 -> fee = 0.6915
        fills = [L("0.44", "17"), L("0.55", "20"), L("0.56", "3")]
        assert KALSHI_DIRECT.order_fee(fills, buying=True) == D("0.6915")


class TestPolymarket:
    # Values from the 100-share fee tables on docs.polymarket.com/trading/fees.
    @pytest.mark.parametrize(
        "schedule, price, expected",
        [
            (POLY_CRYPTO, "0.50", "1.75"),
            (POLY_CRYPTO, "0.30", "1.47"),
            (POLY_SPORTS, "0.30", "1.05"),
            (POLY_POLITICS, "0.90", "0.36"),
        ],
    )
    def test_matches_docs_table(self, schedule, price, expected):
        assert schedule.order_fee([L(price, "100")], buying=True) == D(expected)

    def test_symmetric_around_half(self):
        assert POLY_CRYPTO.fill_fee(D("0.3"), D("100")) == POLY_CRYPTO.fill_fee(D("0.7"), D("100"))

    def test_rounds_to_5dp(self):
        # 0.04 * 1 * 0.001 * 0.999 = 0.00003996 -> 0.00004
        assert POLY_POLITICS.fill_fee(D("0.001"), D("1")) == D("0.00004")

    def test_tiny_fee_rounds_to_zero(self):
        # 0.04 * 1 * 0.0001 * 0.9999 = 0.0000039996 -> 0.00000
        assert POLY_POLITICS.fill_fee(D("0.0001"), D("1")) == D("0")

    def test_no_cash_rounding(self):
        # Fee only depends on the fills, not on direction.
        fills = [L("0.30", "100")]
        assert POLY_SPORTS.order_fee(fills, buying=True) == POLY_SPORTS.order_fee(fills, buying=False)


def test_zero_fees():
    assert ZERO_FEES.order_fee([L("0.5", "1000")], buying=True) == 0


def test_empty_order_has_no_fee():
    assert KALSHI_DIRECT.order_fee([], buying=True) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(rate=D("-0.01"), fill_quantum=D("0.01"), fill_rounding=ROUND_CEILING),
        dict(rate=0.07, fill_quantum=D("0.01"), fill_rounding=ROUND_CEILING),
        dict(rate=D("0.07"), fill_quantum=D("0"), fill_rounding=ROUND_CEILING),
        dict(rate=D("0.07"), fill_quantum=D("0.01"), fill_rounding="ROUND_SIDEWAYS"),
        dict(rate=D("0.07"), fill_quantum=D("0.01"), fill_rounding=ROUND_CEILING, cash_quantum=D("0")),
    ],
)
def test_invalid_schedules(kwargs):
    with pytest.raises(ValueError):
        FeeSchedule(**kwargs)
