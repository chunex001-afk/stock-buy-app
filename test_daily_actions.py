# -*- coding: utf-8 -*-
"""日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
フェーズ2)のapp._compute_daily_actionsのテスト。

Twelve Data・Redisへは一切アクセスしない。_compute_daily_actionsは純粋関数
(rows/portfolio_state/riskoff_state/anchor_date/trading_calendarを全て引数で
受け取る)なので、store.get_ticker_record(SELL時のprofit_eligible_too判定・
PROFIT判定で使われる)だけをmockで差し替える。

IMPLEMENTATION_DESIGN_daily_action_ui.md 7節で確認済みの6状態遷移ケースと、
追加の境界値ケースを全て検証する。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import app


def _business_days(n, start="2026-01-01"):
    """startからn営業日(平日のみ)の日付文字列リストを古い順に返す。"""
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(60)  # 十分な長さの取引日カレンダー


def _row(ticker, price, history, status="ready"):
    return {
        "ticker": ticker,
        "price": price,
        "quintile": {"status": status, "current_q": None, "history": history},
    }


def _day0_history(day0_date):
    """day0_dateに初めてQ5になった(前日はQ5でなかった)履歴。"""
    return [{"date": day0_date, "q": "Q5"}]


def _continuing_q5_history(since_date):
    """since_dateから継続してQ5(今日もDay0ではない)履歴。"""
    return [{"date": since_date, "q": "Q5"}]


def _position(entry_date, entry_price, profit_taken=False):
    return {"entry_date": entry_date, "entry_price": entry_price, "profit_taken": profit_taken,
            "profit_taken_date": None}


class DailyActionsTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_records = {}
        patcher = mock.patch.object(
            app.store, "get_ticker_record",
            side_effect=lambda t: self.fake_records.get(t),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set_price(self, ticker, price):
        self.fake_records[ticker] = {"price": price}

    def _run(self, rows, positions, anchor_idx, riskoff_state=None):
        portfolio_state = {"positions": positions, "last_updated": CAL[anchor_idx]}
        return app._compute_daily_actions(rows, portfolio_state, riskoff_state, CAL[anchor_idx], CAL)


class SixTransitionCasesTests(DailyActionsTestCase):
    """IMPLEMENTATION_DESIGN_daily_action_ui.md 7節の6ケース。"""

    def test_case1_more_than_6_day0_same_day_fills_by_watchlist_order_no_carryover(self):
        anchor = 30
        today = CAL[anchor]
        # 8銘柄が同日Day0、保有は0/6 → 空き枠6、先頭6件だけ採用
        tickers = [f"T{i}" for i in range(8)]
        rows = [_row(t, 100.0, _day0_history(today)) for t in tickers]
        result = self._run(rows, {}, anchor)
        self.assertEqual(len(result["buy"]), 6)
        self.assertEqual([b["ticker"] for b in result["buy"]], tickers[:6])
        self.assertEqual(result["buy_candidates_total"], 8)
        # 「翌日に繰り越す記憶」を持たないことの確認: 翌日、同じ銘柄群がまだQ5
        # (今日Day0だった銘柄は継続、=翌日はDay0でなくなる)状態にすると、
        # 採用されなかった2銘柄も含め誰もBUY候補にならない(保留リストは存在しない)。
        next_idx = anchor + 1
        next_today = CAL[next_idx]
        rows_next = [_row(t, 100.0, _continuing_q5_history(today)) for t in tickers]
        result_next = self._run(rows_next, {}, next_idx)
        self.assertEqual(result_next["buy"], [])
        self.assertEqual(result_next["buy_candidates_total"], 0)

    def test_case2_already_held_ticker_becoming_day0_again_is_not_a_buy_candidate(self):
        anchor = 30
        today = CAL[anchor]
        positions = {"AAA": _position(CAL[anchor - 5], 100.0)}
        rows = [_row("AAA", 150.0, _day0_history(today))]  # 保有中だが"再Day0"
        result = self._run(rows, positions, anchor)
        self.assertEqual(result["buy"], [])
        self.assertEqual(result["buy_candidates_total"], 0)

    def test_case3_profit_take_keeps_entry_unchanged_and_does_not_fire_twice(self):
        anchor = 30
        entry_date = CAL[anchor - 5]
        positions = {"AAA": _position(entry_date, 100.0, profit_taken=False)}
        self._set_price("AAA", 150.0)  # +50%
        rows = [_row("AAA", 150.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, anchor)
        self.assertEqual([p["ticker"] for p in result["profit"]], ["AAA"])

        # 利確済みフラグを立てた後、entry_date/entry_priceは変更しない前提で
        # 再度実行すると、PROFITは二度出ない。
        positions2 = {"AAA": _position(entry_date, 100.0, profit_taken=True)}
        rows2 = [_row("AAA", 160.0, _continuing_q5_history(entry_date))]
        result2 = self._run(rows2, positions2, anchor)
        self.assertEqual(result2["profit"], [])

    def test_case4_position_stays_held_after_profit_take_until_20_days_then_fully_sold(self):
        entry_date = CAL[10]
        positions = {"AAA": _position(entry_date, 100.0, profit_taken=True)}
        self._set_price("AAA", 160.0)
        rows = [_row("AAA", 160.0, _continuing_q5_history(entry_date))]

        # 19日目: まだ保有継続(SELLなし)、6枠を占有し続ける
        result_day19 = self._run(rows, positions, 10 + 19)
        self.assertEqual(result_day19["sell"], [])
        self.assertEqual(result_day19["held_count"], 1)

        # 20日目: 残り(利確済みの残存分)を全売却
        result_day20 = self._run(rows, positions, 10 + 20)
        self.assertEqual([s["ticker"] for s in result_day20["sell"]], ["AAA"])

    def test_case5_sell_and_profit_same_day_sell_wins_no_profit_shown(self):
        entry_date = CAL[10]
        anchor = 10 + 20  # ちょうど20営業日経過
        positions = {"AAA": _position(entry_date, 100.0, profit_taken=False)}
        self._set_price("AAA", 160.0)  # +60%、PROFIT条件も満たす
        rows = [_row("AAA", 160.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, anchor)
        self.assertEqual([s["ticker"] for s in result["sell"]], ["AAA"])
        self.assertEqual(result["profit"], [])  # SELL優先、PROFITは表示しない
        # 参考情報としてprofit_eligible_tooがTrueになっていること
        self.assertTrue(result["sell"][0]["profit_eligible_too"])

    def test_case6_riskoff_active_does_not_block_buy_only_annotates_ratio(self):
        anchor = 30
        today = CAL[anchor]
        rows = [_row("AAA", 100.0, _day0_history(today))]
        riskoff_state = {"active": True, "ratio": 0.70}
        result = self._run(rows, {}, anchor, riskoff_state=riskoff_state)
        self.assertEqual([b["ticker"] for b in result["buy"]], ["AAA"])  # 通常通りBUY候補になる
        self.assertTrue(result["risk_off_active"])
        self.assertAlmostEqual(result["recommended_ratio"], 0.70)


class BoundaryValueTests(DailyActionsTestCase):
    def test_zero_of_six_held_one_day0(self):
        anchor = 30
        today = CAL[anchor]
        rows = [_row("AAA", 100.0, _day0_history(today))]
        result = self._run(rows, {}, anchor)
        self.assertEqual(result["held_count"], 0)
        self.assertEqual(result["free_slots"], 6)
        self.assertEqual([b["ticker"] for b in result["buy"]], ["AAA"])

    def test_five_of_six_held_two_day0_both_fit(self):
        anchor = 30
        today = CAL[anchor]
        positions = {f"H{i}": _position(CAL[anchor - 5], 10.0) for i in range(5)}
        rows = [_row("B1", 100.0, _day0_history(today)), _row("B2", 100.0, _day0_history(today))]
        result = self._run(rows, positions, anchor)
        self.assertEqual(result["free_slots"], 1)
        self.assertEqual(len(result["buy"]), 1)
        self.assertEqual(result["buy"][0]["ticker"], "B1")  # watchlist順で先頭のみ採用
        self.assertEqual(result["buy_candidates_total"], 2)

    def test_six_of_six_held_multiple_day0_none_admitted(self):
        anchor = 30
        today = CAL[anchor]
        positions = {f"H{i}": _position(CAL[anchor - 5], 10.0) for i in range(6)}
        rows = [_row("B1", 100.0, _day0_history(today)), _row("B2", 100.0, _day0_history(today))]
        result = self._run(rows, positions, anchor)
        self.assertEqual(result["free_slots"], 0)
        self.assertEqual(result["buy"], [])
        self.assertEqual(result["buy_candidates_total"], 2)

    def test_gain_49_9_percent_is_not_profit(self):
        anchor = 30
        entry_date = CAL[anchor - 5]  # 20日未満に保つ(SELLとの干渉を避ける)
        positions = {"AAA": _position(entry_date, 100.0)}
        self._set_price("AAA", 149.9)
        rows = [_row("AAA", 149.9, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, anchor)
        self.assertEqual(result["profit"], [])

    def test_gain_exactly_50_0_percent_is_profit(self):
        anchor = 30
        entry_date = CAL[anchor - 5]  # 20日未満に保つ(SELLとの干渉を避ける)
        positions = {"AAA": _position(entry_date, 100.0)}
        self._set_price("AAA", 150.0)
        rows = [_row("AAA", 150.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, anchor)
        self.assertEqual([p["ticker"] for p in result["profit"]], ["AAA"])
        self.assertAlmostEqual(result["profit"][0]["gain_pct"], 50.0)

    def test_held_19_trading_days_is_not_sell(self):
        entry_date = CAL[10]
        positions = {"AAA": _position(entry_date, 100.0)}
        self._set_price("AAA", 100.0)
        rows = [_row("AAA", 100.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, 10 + 19)
        self.assertEqual(result["sell"], [])

    def test_held_exactly_20_trading_days_is_sell(self):
        entry_date = CAL[10]
        positions = {"AAA": _position(entry_date, 100.0)}
        self._set_price("AAA", 100.0)
        rows = [_row("AAA", 100.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, 10 + 20)
        self.assertEqual([s["ticker"] for s in result["sell"]], ["AAA"])
        self.assertEqual(result["sell"][0]["days_held"], 20)

    def test_profit_taken_true_never_fires_profit_again_even_at_high_gain(self):
        entry_date = CAL[10]
        positions = {"AAA": _position(entry_date, 100.0, profit_taken=True)}
        self._set_price("AAA", 500.0)  # +400%でも
        rows = [_row("AAA", 500.0, _continuing_q5_history(entry_date))]
        result = self._run(rows, positions, 10 + 5)
        self.assertEqual(result["profit"], [])

    def test_sell_frees_slot_for_same_day_buy_when_full(self):
        """SELL→PROFIT→空き枠確定→BUYの順序そのもののテスト: 6/6保有中でも、
        20営業日到達でSELLする銘柄があれば、その空いた枠を同日のBUY候補が
        利用できる。"""
        anchor = 30
        today = CAL[anchor]
        entry_date = CAL[anchor - 20]  # ちょうど20営業日前 → 今日SELL対象
        positions = {"OLD": _position(entry_date, 100.0)}
        for i in range(5):
            positions[f"H{i}"] = _position(CAL[anchor - 5], 10.0)  # 残り5枠も埋まっている(合計6/6)
        self._set_price("OLD", 100.0)
        rows = [_row("NEW", 100.0, _day0_history(today))]
        result = self._run(rows, positions, anchor)
        self.assertEqual([s["ticker"] for s in result["sell"]], ["OLD"])
        self.assertEqual(result["free_slots"], 1)  # SELLで1枠空く
        self.assertEqual([b["ticker"] for b in result["buy"]], ["NEW"])  # その枠をBUYが使える

    def test_profit_does_not_free_a_slot_for_buy(self):
        """PROFITはポジションを閉じないため、BUY枠は増えないことの確認。"""
        anchor = 30
        today = CAL[anchor]
        entry_date = CAL[anchor - 5]  # まだ20日未満、SELLにはならない
        positions = {"OLD": _position(entry_date, 100.0)}
        for i in range(5):
            positions[f"H{i}"] = _position(CAL[anchor - 5], 10.0)
        self._set_price("OLD", 160.0)  # +60%、PROFIT対象
        rows = [_row("NEW", 100.0, _day0_history(today))]
        result = self._run(rows, positions, anchor)
        self.assertEqual([p["ticker"] for p in result["profit"]], ["OLD"])
        self.assertEqual(result["free_slots"], 0)  # PROFITでは枠は空かない
        self.assertEqual(result["buy"], [])  # BUYは採用されない

    def test_multiple_sell_and_profit_same_day(self):
        anchor = 30
        entry_old = CAL[anchor - 20]
        entry_mid = CAL[anchor - 5]
        positions = {
            "SELL1": _position(entry_old, 100.0),
            "SELL2": _position(entry_old, 100.0),
            "PROFIT1": _position(entry_mid, 100.0),
            "PROFIT2": _position(entry_mid, 100.0),
        }
        self._set_price("SELL1", 100.0)
        self._set_price("SELL2", 200.0)  # SELL対象かつ+50%以上だがSELL優先
        self._set_price("PROFIT1", 160.0)
        self._set_price("PROFIT2", 170.0)
        rows = []
        result = self._run(rows, positions, anchor)
        self.assertEqual(sorted(s["ticker"] for s in result["sell"]), ["SELL1", "SELL2"])
        self.assertEqual(sorted(p["ticker"] for p in result["profit"]), ["PROFIT1", "PROFIT2"])
        self.assertTrue(any(s["ticker"] == "SELL2" and s["profit_eligible_too"] for s in result["sell"]))


if __name__ == "__main__":
    unittest.main()
