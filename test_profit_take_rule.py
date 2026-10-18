# -*- coding: utf-8 -*-
"""+50%利益100%利確ルール(design 2026-10-18、バックテスト検証(戦略E)を踏まえて
本番実装)のテスト。

ルール: 現在価格が取得単価の+50%以上になった場合、現在の評価額に対して取得原本
(shares*entry_price)相当額を残すように売却株数を計算し、含み益を100%利確する
(保有株数の50%を売るのではない)。株数(shares)が未入力の銘柄は従来通り
profit_takenフラグのみの手動利確(既存機能、変更しない)。

Twelve Data・Redisへは一切アクセスしない(store側の該当関数のみmock)。
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import app


def _business_days(n, start="2026-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(60)


def _row(ticker, price, history, status="ready"):
    return {"ticker": ticker, "price": price,
            "quintile": {"status": status, "current_q": None, "history": history}}


def _position(entry_date, entry_price, shares=None, profit_taken=False):
    return {"entry_date": entry_date, "entry_price": entry_price, "shares": shares,
            "profit_taken": profit_taken, "profit_taken_date": None}


class ComputeProfitTakeAmountsTests(unittest.TestCase):
    """純粋な$計算そのもの(_compute_profit_take_amounts)の検証。"""

    def test_1_gain_49pct_is_below_threshold_not_tested_here(self):
        # 閾値判定自体は_compute_daily_actions側(このテストは金額計算のみ対象)。
        # ここでは単に、閾値未満でも関数自体はprofit_valueを素直に計算することだけ確認する
        # (呼び出し元の_compute_daily_actionsがgain_pct>=50のときだけ呼ぶ設計)。
        r = app._compute_profit_take_amounts(10.0, 100.0, 149.0)
        self.assertAlmostEqual(r["profit_value"], 490.0)

    def test_2_exactly_150_sells_the_full_profit_portion(self):
        # 取得単価$100、現在価格$150(+50%)、10株保有
        r = app._compute_profit_take_amounts(10.0, 100.0, 150.0)
        self.assertAlmostEqual(r["cost_basis_value"], 1000.0)
        self.assertAlmostEqual(r["current_value"], 1500.0)
        self.assertAlmostEqual(r["profit_value"], 500.0)
        self.assertAlmostEqual(r["sell_shares"], 500.0 / 150.0)  # 3.333...
        self.assertAlmostEqual(r["sell_value"], 500.0)
        self.assertAlmostEqual(r["remaining_shares"], 10.0 - 500.0 / 150.0)  # 6.666...
        self.assertAlmostEqual(r["remaining_value"], 1000.0)  # 原本相当がきっちり残る

    def test_3_at_160_remaining_value_still_equals_cost_basis(self):
        # 取得単価$100、現在価格$160、10株保有 → 原本$1,000相当を残す
        r = app._compute_profit_take_amounts(10.0, 100.0, 160.0)
        self.assertAlmostEqual(r["cost_basis_value"], 1000.0)
        self.assertAlmostEqual(r["sell_value"], 600.0)
        self.assertAlmostEqual(r["remaining_value"], 1000.0)
        self.assertAlmostEqual(r["sell_shares"], 600.0 / 160.0)  # 3.75

    def test_shares_none_returns_all_none(self):
        r = app._compute_profit_take_amounts(None, 100.0, 160.0)
        self.assertIsNone(r["cost_basis_value"])
        self.assertIsNone(r["sell_shares"])

    def test_sell_shares_is_not_half_of_holding_shares(self):
        # 回帰防止: 「保有株数の50%を売る」という誤った解釈になっていないことの確認。
        r = app._compute_profit_take_amounts(10.0, 100.0, 150.0)
        self.assertNotAlmostEqual(r["sell_shares"], 5.0, places=3)  # 誤り(株数の50%)ではない
        self.assertAlmostEqual(r["sell_shares"], 3.3333333, places=5)  # 正しい(評価額ベース)


class ComputeDailyActionsProfitTakeTests(unittest.TestCase):
    """_compute_daily_actionsのPROFIT判定への統合(項目1,2,3,4,5,6,7)。"""

    def setUp(self):
        self.fake_records = {}
        patcher = mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self.fake_records.get(t))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, positions, rows=None):
        today = CAL[-1]
        state = {"positions": positions, "last_updated": today}
        return app._compute_daily_actions(rows or [], state, None, today, CAL)

    def test_1_gain_49pct_is_not_profit_candidate(self):
        self.fake_records["AAA"] = {"price": 149.0}
        positions = {"AAA": _position(CAL[-5], 100.0, shares=10.0)}
        result = self._run(positions)
        self.assertEqual(result["profit"], [])

    def test_2_gain_exactly_50pct_is_profit_candidate_with_amounts(self):
        self.fake_records["AAA"] = {"price": 150.0}
        positions = {"AAA": _position(CAL[-5], 100.0, shares=10.0)}
        result = self._run(positions)
        self.assertEqual([p["ticker"] for p in result["profit"]], ["AAA"])
        p = result["profit"][0]
        self.assertAlmostEqual(p["cost_basis_value"], 1000.0)
        self.assertAlmostEqual(p["sell_value"], 500.0)
        self.assertAlmostEqual(p["remaining_value"], 1000.0)

    def test_3_gain_60pct_is_profit_candidate_remaining_is_cost_basis(self):
        self.fake_records["AAA"] = {"price": 160.0}
        positions = {"AAA": _position(CAL[-5], 100.0, shares=10.0)}
        result = self._run(positions)
        p = result["profit"][0]
        self.assertAlmostEqual(p["remaining_value"], 1000.0)  # 原本$1,000相当を残す

    def test_7_sell_and_profit_same_day_sell_wins_no_profit_candidate(self):
        # 20営業日経過かつ+50%到達が同日 → SELL優先、PROFIT候補には出さない(既存仕様の再確認)
        entry_date = CAL[0]
        self.fake_records["AAA"] = {"price": 200.0}  # +100%、かつ20日経過
        positions = {"AAA": _position(entry_date, 100.0, shares=10.0)}
        today = CAL[20]
        state = {"positions": positions, "last_updated": today}
        result = app._compute_daily_actions([], state, None, today, CAL)
        self.assertEqual([s["ticker"] for s in result["sell"]], ["AAA"])
        self.assertEqual(result["profit"], [])
        self.assertTrue(result["sell"][0]["profit_eligible_too"])


class ConfirmProfitTakeApiTests(unittest.TestCase):
    """/api/portfolio/confirm_profit経由の$計算・状態更新(項目2,3,4,5,6,8,9,10,11)。"""

    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._watchlist = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
        self._records = {}
        self._calendar = CAL

        def fake_get_portfolio_state():
            return self._portfolio_state

        def fake_set_portfolio_state(data):
            self._portfolio_state = data
            return True

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=fake_get_portfolio_state),
            mock.patch.object(app.store, "set_portfolio_state", side_effect=fake_set_portfolio_state),
            mock.patch.object(app.store, "get_watchlist", side_effect=lambda: list(self._watchlist)),
            mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self._records.get(t)),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, path, body):
        resp = self.client.post(path, data=json.dumps(body), content_type="application/json")
        return resp, resp.get_json()

    def _positions(self):
        return (self._portfolio_state or {}).get("positions") or {}

    def test_buy_with_shares_is_stored(self):
        resp, data = self._post("/api/portfolio/confirm_buy",
                                 {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(self._positions()["AAA"]["shares"], 10.0)

    def test_buy_without_shares_still_works_backward_compat(self):
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        self.assertTrue(data["ok"])
        self.assertIsNone(self._positions()["AAA"].get("shares"))

    def test_buy_rejects_non_positive_shares(self):
        resp, data = self._post("/api/portfolio/confirm_buy",
                                 {"ticker": "AAA", "entry_price": 100.0, "shares": -1.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_2_3_confirm_profit_computes_and_stores_remaining_shares(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self._records["AAA"] = {"price": 150.0}
        resp, data = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        self.assertTrue(data["ok"])
        pos = self._positions()["AAA"]
        self.assertTrue(pos["profit_taken"])
        self.assertAlmostEqual(pos["shares"], 10.0 - 500.0 / 150.0)  # 残り約6.667株
        self.assertAlmostEqual(pos["profit_take_sold_shares"], 500.0 / 150.0)
        self.assertAlmostEqual(pos["profit_take_sold_value"], 500.0)
        self.assertAlmostEqual(pos["profit_take_price"], 150.0)

    def test_4_5_6_does_not_refire_after_price_moves(self):
        """項目4,5,6: 利確確定後、価格が$180に上がっても$120に下がっても再度利確しない。"""
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self._records["AAA"] = {"price": 150.0}
        self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        shares_after_first_take = self._positions()["AAA"]["shares"]

        for later_price in (180.0, 120.0):
            self._records["AAA"] = {"price": later_price}
            today = self._calendar[-1]
            state = {"positions": self._positions(), "last_updated": today}
            result = app._compute_daily_actions([], state, None, today, self._calendar)
            self.assertEqual(result["profit"], [])  # 再度PROFIT候補に出ない
            # confirm_profitを再度呼んでも状態が変化しない(冪等)ことも確認
            resp, data = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
            self.assertTrue(data.get("already_done"))
            self.assertAlmostEqual(self._positions()["AAA"]["shares"], shares_after_first_take)

    def test_8_reload_recompute_does_not_double_fire(self):
        """項目8: 再計算・ページリロード相当(/api/rankingを複数回呼ぶ)でも二重に
        利確候補が出ない。"""
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self._records["AAA"] = {"price": 150.0}

        with mock.patch.object(app, "_build_riskoff_view", return_value={"active": False}), \
             mock.patch.object(app, "_build_ai_market_env_view", return_value=None), \
             mock.patch.object(app.store, "get_last_refresh", return_value=None), \
             mock.patch.object(app.refresh, "remaining_td_budget", return_value=(10, 0)):
            r1 = self.client.get("/api/ranking").get_json()
            self.assertEqual([p["ticker"] for p in r1["daily_actions"]["profit"]], ["AAA"])

        self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})

        with mock.patch.object(app, "_build_riskoff_view", return_value={"active": False}), \
             mock.patch.object(app, "_build_ai_market_env_view", return_value=None), \
             mock.patch.object(app.store, "get_last_refresh", return_value=None), \
             mock.patch.object(app.refresh, "remaining_td_budget", return_value=(10, 0)):
            # 何度リロード(再計算)しても、同じエピソードでは二度と出ない
            for _ in range(3):
                r2 = self.client.get("/api/ranking").get_json()
                self.assertEqual(r2["daily_actions"]["profit"], [])

    def test_9_buy_fills_free_slot_as_before(self):
        """項目9: 空きスロットへのQ5 Day0 BUYが既存仕様どおり動くことの回帰確認。"""
        resp, data = self._post("/api/portfolio/confirm_buy",
                                 {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self.assertTrue(data["ok"])
        self.assertEqual(data["held_count"], 1)

    def test_10_max_6_positions_unaffected(self):
        """項目10: 最大6銘柄の制約が既存仕様どおりであることの回帰確認。"""
        for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]:
            self._post("/api/portfolio/confirm_buy", {"ticker": t, "entry_price": 100.0, "shares": 10.0})
        self.assertEqual(len(self._positions()), 6)
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "GGG", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_11_riskoff_state_unaffected_by_profit_take_fields(self):
        """項目11: Risk-off ON でも既存のdaily_actions計算(risk_off_active等)が
        壊れていないことの確認(+50%利確の新フィールドとは独立)。"""
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0, "shares": 10.0})
        self._records["AAA"] = {"price": 150.0}
        today = self._calendar[-1]
        riskoff_state = {"active": True, "ratio": 0.70}
        state = {"positions": self._positions(), "last_updated": today}
        result = app._compute_daily_actions([], state, riskoff_state, today, self._calendar)
        self.assertTrue(result["risk_off_active"])
        self.assertAlmostEqual(result["recommended_ratio"], 0.70)
        self.assertEqual([p["ticker"] for p in result["profit"]], ["AAA"])  # PROFIT判定も共存して動く


if __name__ == "__main__":
    unittest.main()
