# -*- coding: utf-8 -*-
"""Unit tests for BacktestEngine.compute_summary()."""

import unittest
import json
from dataclasses import dataclass

from src.core.backtest_engine import BacktestEngine


@dataclass
class FakeRow:
    eval_status: str = "completed"
    position_recommendation: str = "long"
    outcome: str = "win"
    direction_correct: bool | None = True
    stock_return_pct: float | None = 1.0
    simulated_return_pct: float | None = 1.0
    hit_stop_loss: bool | None = False
    hit_take_profit: bool | None = False
    first_hit: str | None = "neither"
    first_hit_trading_days: int | None = None
    operation_advice: str | None = "买入"


class BacktestSummaryTestCase(unittest.TestCase):
    def test_nonfinite_historical_returns_do_not_poison_summary(self):
        for valid_returns, expected in (([4.0, -2.0], 1.0), ([], None)):
            with self.subTest(valid_returns=valid_returns):
                values = valid_returns + [None, float("nan"), float("inf"), -float("inf")]
                rows = [
                    FakeRow(stock_return_pct=value, simulated_return_pct=value)
                    for value in values
                ]
                summary = BacktestEngine.compute_summary(
                    results=rows,
                    scope="stock",
                    code="600519",
                    eval_window_days=3,
                    engine_version="v1",
                )
                self.assertEqual(summary["avg_stock_return_pct"], expected)
                self.assertEqual(summary["avg_simulated_return_pct"], expected)
                self.assertEqual(summary["completed_count"], len(rows))
                json.dumps(summary, allow_nan=False)

    def test_trigger_rates_use_applicable_denominators(self) -> None:
        # One row has stop-loss configured, one row doesn't.
        rows = [
            FakeRow(hit_stop_loss=True, hit_take_profit=None, first_hit="stop_loss"),
            FakeRow(hit_stop_loss=None, hit_take_profit=True, first_hit="take_profit"),
        ]

        summary = BacktestEngine.compute_summary(
            results=rows,
            scope="stock",
            code="600519",
            eval_window_days=3,
            engine_version="v1",
        )

        # stop_loss_trigger_rate denominator should be 1 (only applicable row)
        self.assertEqual(summary["stop_loss_trigger_rate"], 100.0)

        # take_profit_trigger_rate denominator should be 1 (only applicable row)
        self.assertEqual(summary["take_profit_trigger_rate"], 100.0)

        # ambiguous_rate denominator should be 2 (any target applicable)
        self.assertEqual(summary["ambiguous_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()

