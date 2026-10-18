# -*- coding: utf-8 -*-
"""日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
フェーズ4)のUI/API連携テスト。

本リポジトリには既存のブラウザ/JSテストインフラが無いため(既存test_*.pyは
全てPython側のunittest)、/api/rankingのJSONレスポンス(daily_actions・
portfolio)が、JS側のrenderDailyActions/renderPortfolioStatusが必要とする
データを正しく含んでいること、およびconfirm_*呼び出し後にそのデータが
正しく更新されることを、Flaskのテストクライアントで検証する。

Upstash・Twelve Dataへは一切アクセスしない。
"""
import json
import unittest
from unittest import mock

import app


def _ticker_record(price):
    return {"price": price, "last_trade_date": "2026-01-20"}


def _quintile_state(history, last_updated):
    return {"current_q": history[-1]["q"] if history else None, "previous_q": None,
            "history": history, "last_updated": last_updated}


class DailyActionsUiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._watchlist = []
        self._records = {}
        self._quintile = {}
        self._calendar = [f"2026-01-{d:02d}" for d in range(1, 32) if d % 7 not in (0,)] \
            + [f"2026-02-{d:02d}" for d in range(1, 29) if d % 7 not in (0,)]

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
            mock.patch.object(app.store, "get_quintile_state", side_effect=lambda t: self._quintile.get(t)),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
            mock.patch.object(app, "_build_riskoff_view", return_value={"active": False}),
            mock.patch.object(app, "_build_ai_market_env_view", return_value=None),
            mock.patch.object(app.store, "get_last_refresh", return_value=None),
            mock.patch.object(app.refresh, "remaining_td_budget", return_value=(10, 0)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _ranking(self):
        resp = self.client.get("/api/ranking")
        return resp.get_json()

    def _post(self, path, body):
        resp = self.client.post(path, data=json.dumps(body), content_type="application/json")
        return resp.get_json()


class ActionCardDataTests(DailyActionsUiTestCase):
    """1〜5: 各アクションカード・ACTIONなし・保有X/6に必要なデータが
    /api/rankingのレスポンスに正しく含まれること。"""

    def test_1_buy_card_data_present_for_day0_ticker(self):
        anchor_idx = 30
        anchor = self._calendar[anchor_idx]
        self._watchlist = ["BUYME"]
        self._records["BUYME"] = _ticker_record(100.0)
        self._quintile["BUYME"] = _quintile_state([{"date": anchor, "q": "Q5"}], anchor)
        # anchor_date(=_compute_daily_actionsの"今日")はtrading_calendar[-1]を
        # 使う実装のため、「今日」をDay0の日そのものに揃えるためにカレンダーを
        # そこで切り詰める(test_3/test_9と同じ手法)。
        with mock.patch.object(app, "_trading_days_calendar",
                                side_effect=lambda: self._calendar[:anchor_idx + 1]):
            data = self._ranking()
        self.assertEqual([b["ticker"] for b in data["daily_actions"]["buy"]], ["BUYME"])

    def test_2_profit_card_data_present_for_50pct_ticker(self):
        anchor_idx = 30
        anchor = self._calendar[anchor_idx]
        entry_date = self._calendar[25]
        self._watchlist = ["PROFITME"]
        self._records["PROFITME"] = _ticker_record(160.0)
        self._quintile["PROFITME"] = _quintile_state([{"date": entry_date, "q": "Q5"}], anchor)
        self._portfolio_state = {
            "positions": {"PROFITME": {"entry_date": entry_date, "entry_price": 100.0,
                                        "profit_taken": False, "profit_taken_date": None}},
            "last_updated": anchor,
        }
        with mock.patch.object(app, "_trading_days_calendar",
                                side_effect=lambda: self._calendar[:anchor_idx + 1]):
            data = self._ranking()
        self.assertEqual([p["ticker"] for p in data["daily_actions"]["profit"]], ["PROFITME"])
        holding = next(h for h in data["portfolio"]["holdings"] if h["ticker"] == "PROFITME")
        self.assertAlmostEqual(holding["gain_pct"], 60.0)

    def test_3_sell_card_data_present_for_20day_ticker(self):
        entry_idx = 5
        anchor_idx = entry_idx + 20
        entry_date = self._calendar[entry_idx]
        self._watchlist = ["SELLME"]
        self._records["SELLME"] = _ticker_record(105.0)
        self._quintile["SELLME"] = _quintile_state([{"date": entry_date, "q": "Q5"}], self._calendar[anchor_idx])
        self._portfolio_state = {
            "positions": {"SELLME": {"entry_date": entry_date, "entry_price": 100.0,
                                      "profit_taken": False, "profit_taken_date": None}},
            "last_updated": self._calendar[anchor_idx],
        }
        with mock.patch.object(app, "_trading_days_calendar",
                                side_effect=lambda: self._calendar[:anchor_idx + 1]):
            data = self._ranking()
        self.assertEqual([s["ticker"] for s in data["daily_actions"]["sell"]], ["SELLME"])

    def test_4_no_action_when_nothing_qualifies(self):
        self._watchlist = ["QUIET"]
        anchor = self._calendar[30]
        self._quintile["QUIET"] = _quintile_state([{"date": self._calendar[20], "q": "Q3"}], anchor)
        self._records["QUIET"] = _ticker_record(100.0)
        data = self._ranking()
        da = data["daily_actions"]
        self.assertEqual(da["buy"], [])
        self.assertEqual(da["sell"], [])
        self.assertEqual(da["profit"], [])

    def test_5_held_count_reflects_portfolio(self):
        self._portfolio_state = {
            "positions": {
                "A": {"entry_date": self._calendar[1], "entry_price": 10.0, "profit_taken": False, "profit_taken_date": None},
                "B": {"entry_date": self._calendar[1], "entry_price": 10.0, "profit_taken": False, "profit_taken_date": None},
            },
            "last_updated": self._calendar[1],
        }
        data = self._ranking()
        self.assertEqual(data["portfolio"]["held_count"], 2)
        self.assertEqual(data["portfolio"]["max_positions"], 6)


class ConfirmUpdatesUiDataTests(DailyActionsUiTestCase):
    """6〜8: confirm_*呼び出し後にUIが参照するデータが正しく更新されること。"""

    def test_6_buy_updates_portfolio_display(self):
        self._watchlist = ["NEWBUY"]
        before = self._ranking()
        self.assertEqual(before["portfolio"]["held_count"], 0)

        result = self._post("/api/portfolio/confirm_buy", {"ticker": "NEWBUY", "entry_price": 42.0})
        self.assertTrue(result["ok"])

        after = self._ranking()
        self.assertEqual(after["portfolio"]["held_count"], 1)
        holding = after["portfolio"]["holdings"][0]
        self.assertEqual(holding["ticker"], "NEWBUY")
        self.assertEqual(holding["entry_price"], 42.0)

    def test_7_profit_updates_profit_taken_display(self):
        self._watchlist = ["P"]
        self._post("/api/portfolio/confirm_buy", {"ticker": "P", "entry_price": 100.0})
        before = self._ranking()
        self.assertFalse(before["portfolio"]["holdings"][0]["profit_taken"])

        result = self._post("/api/portfolio/confirm_profit", {"ticker": "P"})
        self.assertTrue(result["ok"])

        after = self._ranking()
        self.assertTrue(after["portfolio"]["holdings"][0]["profit_taken"])

    def test_8_sell_reduces_held_count(self):
        self._watchlist = ["S"]
        self._post("/api/portfolio/confirm_buy", {"ticker": "S", "entry_price": 100.0})
        self.assertEqual(self._ranking()["portfolio"]["held_count"], 1)

        result = self._post("/api/portfolio/confirm_sell", {"ticker": "S"})
        self.assertTrue(result["ok"])

        self.assertEqual(self._ranking()["portfolio"]["held_count"], 0)


class Day0OnlyRuleTests(DailyActionsUiTestCase):
    """9〜10: 「Q5 Day0だけがBUY対象、20日経過はQ5継続でもSELL」が
    UIに渡るデータレベルで維持されていること(daily_actionsの計算ロジック
    自体はフェーズ2のまま変更していない、その事実をUI経路で再確認する)。"""

    def test_9_sell_fires_at_20_days_even_if_still_q5(self):
        entry_idx = 5
        anchor_idx = entry_idx + 20
        entry_date = self._calendar[entry_idx]
        self._watchlist = ["STILLQ5"]
        self._records["STILLQ5"] = _ticker_record(110.0)
        # 保有開始日からずっとQ5が継続している(今日もQ5)という状態。
        self._quintile["STILLQ5"] = _quintile_state(
            [{"date": entry_date, "q": "Q5"}], self._calendar[anchor_idx])
        self._portfolio_state = {
            "positions": {"STILLQ5": {"entry_date": entry_date, "entry_price": 100.0,
                                       "profit_taken": False, "profit_taken_date": None}},
            "last_updated": self._calendar[anchor_idx],
        }
        with mock.patch.object(app, "_trading_days_calendar",
                                side_effect=lambda: self._calendar[:anchor_idx + 1]):
            data = self._ranking()
        self.assertEqual([s["ticker"] for s in data["daily_actions"]["sell"]], ["STILLQ5"])

    def test_10_day1_or_later_is_not_a_buy_candidate(self):
        anchor = self._calendar[30]
        day0 = self._calendar[25]  # Day0は5営業日前、今日はDay5相当(継続中)
        self._watchlist = ["DAY5"]
        self._records["DAY5"] = _ticker_record(100.0)
        self._quintile["DAY5"] = _quintile_state([{"date": day0, "q": "Q5"}], anchor)
        data = self._ranking()
        self.assertNotIn("DAY5", [b["ticker"] for b in data["daily_actions"]["buy"]])


if __name__ == "__main__":
    unittest.main()
