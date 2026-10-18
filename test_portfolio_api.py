# -*- coding: utf-8 -*-
"""日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
フェーズ3)の/api/portfolio/confirm_buy・confirm_sell・confirm_profitのテスト。

Flaskのテストクライアントで実際のHTTPエンドポイントとして呼び出す。
Upstash・Twelve Dataへは一切アクセスしない(store側の該当関数のみmockで
差し替え、portfolio:stateは呼び出しを跨いで保持する簡易fakeにする)。
"""
import json
import unittest
from unittest import mock

import app


class PortfolioApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._watchlist = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
        self._calendar = [f"2026-01-{d:02d}" for d in range(1, 29) if d % 7 not in (0,)]

        def fake_get_portfolio_state():
            return self._portfolio_state

        def fake_set_portfolio_state(data):
            self._portfolio_state = data
            return True

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=fake_get_portfolio_state),
            mock.patch.object(app.store, "set_portfolio_state", side_effect=fake_set_portfolio_state),
            mock.patch.object(app.store, "get_watchlist", side_effect=lambda: list(self._watchlist)),
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


class ConfirmBuyTests(PortfolioApiTestCase):
    def test_1_buy_moves_not_held_to_held(self):
        self.assertNotIn("AAA", self._positions())
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertIn("AAA", self._positions())
        self.assertEqual(self._positions()["AAA"]["entry_price"], 100.0)
        self.assertFalse(self._positions()["AAA"]["profit_taken"])

    def test_2_duplicate_buy_is_rejected_not_double_registered(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 999.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        # 既存の記録(entry_price=100.0)が上書きされていないこと
        self.assertEqual(self._positions()["AAA"]["entry_price"], 100.0)
        self.assertEqual(len(self._positions()), 1)

    def test_3_buy_rejected_when_6_slots_full(self):
        for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]:
            self._post("/api/portfolio/confirm_buy", {"ticker": t, "entry_price": 100.0})
        self.assertEqual(len(self._positions()), 6)
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "GGG", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(len(self._positions()), 6)
        self.assertNotIn("GGG", self._positions())

    def test_buy_rejects_non_positive_price(self):
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        resp2, data2 = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": -5})
        self.assertEqual(resp2.status_code, 400)
        self.assertFalse(data2["ok"])

    def test_buy_rejects_non_numeric_price(self):
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_buy_rejects_empty_ticker(self):
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_buy_rejects_ticker_not_in_watchlist(self):
        resp, data = self._post("/api/portfolio/confirm_buy", {"ticker": "ZZZZ", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_buy_rejects_malformed_entry_date(self):
        resp, data = self._post("/api/portfolio/confirm_buy",
                                 {"ticker": "AAA", "entry_price": 100.0, "entry_date": "not-a-date"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])


class ConfirmProfitTests(PortfolioApiTestCase):
    def test_4_profit_sets_profit_taken_true(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        resp, data = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(self._positions()["AAA"]["profit_taken"])

    def test_5_profit_rerun_is_idempotent(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        first_date = self._positions()["AAA"]["profit_taken_date"]
        resp2, data2 = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(data2["ok"])
        self.assertTrue(data2.get("already_done"))
        self.assertTrue(self._positions()["AAA"]["profit_taken"])
        self.assertEqual(self._positions()["AAA"]["profit_taken_date"], first_date)
        self.assertEqual(len(self._positions()), 1)  # ポジションが増減していない

    def test_9_entry_date_and_entry_price_unchanged_by_profit(self):
        self._post("/api/portfolio/confirm_buy",
                    {"ticker": "AAA", "entry_price": 123.45, "entry_date": self._calendar[3]})
        self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        pos = self._positions()["AAA"]
        self.assertEqual(pos["entry_price"], 123.45)
        self.assertEqual(pos["entry_date"], self._calendar[3])

    def test_profit_on_unheld_ticker_is_rejected(self):
        resp, data = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])


class ConfirmSellTests(PortfolioApiTestCase):
    def test_6_sell_moves_held_to_not_held(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())

    def test_7_sell_rerun_is_idempotent(self):
        self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        resp2, data2 = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(data2["ok"])
        self.assertTrue(data2.get("already_absent"))
        self.assertNotIn("AAA", self._positions())

    def test_10_sell_frees_exactly_one_slot(self):
        for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]:
            self._post("/api/portfolio/confirm_buy", {"ticker": t, "entry_price": 100.0})
        self.assertEqual(len(self._positions()), 6)
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "CCC"})
        self.assertTrue(data["ok"])
        self.assertEqual(len(self._positions()), 5)
        self.assertEqual(data["held_count"], 5)
        # 空いた1枠を使って新規購入できることの確認
        resp2, data2 = self._post("/api/portfolio/confirm_buy", {"ticker": "GGG", "entry_price": 50.0})
        self.assertTrue(data2["ok"])
        self.assertEqual(len(self._positions()), 6)


class FullLifecycleTests(PortfolioApiTestCase):
    def test_8_buy_then_profit_then_sell_sequence(self):
        # NOT_HELD -> HELD(未利確)
        r1, d1 = self._post("/api/portfolio/confirm_buy", {"ticker": "AAA", "entry_price": 100.0})
        self.assertTrue(d1["ok"])
        self.assertIn("AAA", self._positions())
        self.assertFalse(self._positions()["AAA"]["profit_taken"])

        # HELD(未利確) -> HELD(利確済)
        r2, d2 = self._post("/api/portfolio/confirm_profit", {"ticker": "AAA"})
        self.assertTrue(d2["ok"])
        self.assertTrue(self._positions()["AAA"]["profit_taken"])
        self.assertIn("AAA", self._positions())  # 利確後も保有継続・枠を占有

        # HELD(利確済) -> NOT_HELD
        r3, d3 = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        self.assertTrue(d3["ok"])
        self.assertNotIn("AAA", self._positions())


class RankingResponseTests(PortfolioApiTestCase):
    """/api/rankingへの追加キーが既存の挙動を壊していないことの確認。
    (既存のrows・q5_momentum・ai_market_env・risk_off等の計算自体は、
    既存の重いQ5パイプライン依存のため、watchlist=[]にして軽量に確認する。)"""

    def test_ranking_includes_new_keys_without_touching_existing_ones(self):
        # _get_watchlist自体をmockして空の監視銘柄にする(store.get_watchlist()が
        # 空リストを返すと既存仕様でDEFAULT_TICKERSにフォールバックするため、
        # それを経由せず「監視銘柄0件」の状態を直接作る)。
        with mock.patch.object(app, "_get_watchlist", return_value=[]), \
             mock.patch.object(app, "_build_riskoff_view", return_value={"active": False}), \
             mock.patch.object(app, "_build_ai_market_env_view", return_value=None), \
             mock.patch.object(app.store, "get_last_refresh", return_value=None), \
             mock.patch.object(app.refresh, "remaining_td_budget", return_value=(10, 0)):
            resp = self.client.get("/api/ranking")
            data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["rows"], [])  # 既存キー・既存挙動は無変更
        self.assertIn("daily_actions", data)
        self.assertIn("portfolio", data)
        self.assertEqual(data["portfolio"]["held_count"], 0)


if __name__ == "__main__":
    unittest.main()
