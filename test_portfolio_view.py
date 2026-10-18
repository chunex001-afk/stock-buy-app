# -*- coding: utf-8 -*-
"""日次アクション型UI(design 2026-10-16)のapp._build_portfolio_viewのテスト。
store.get_portfolio_state/get_ticker_record/_trading_days_calendarをmockで
差し替え、Upstash・Twelve Dataへは一切アクセスしない。"""
import unittest
from unittest import mock

import app


class BuildPortfolioViewTests(unittest.TestCase):
    def setUp(self):
        self.cal = [f"2026-01-{d:02d}" for d in range(1, 29) if d % 7 not in (0,)]  # 平日簡易カレンダー
        self.records = {}
        self.portfolio_state = None

        p1 = mock.patch.object(app.store, "get_portfolio_state", side_effect=lambda: self.portfolio_state)
        p2 = mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self.records.get(t))
        p3 = mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self.cal)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def test_no_positions_returns_empty_holdings(self):
        self.portfolio_state = None
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"], [])
        self.assertEqual(view["held_count"], 0)
        self.assertEqual(view["max_positions"], 6)

    def test_holding_shows_gain_pct_and_days_held(self):
        entry_date = self.cal[5]
        self.portfolio_state = {
            "positions": {"AAA": {"entry_date": entry_date, "entry_price": 100.0,
                                   "profit_taken": False, "profit_taken_date": None}},
            "last_updated": self.cal[-1],
        }
        self.records["AAA"] = {"price": 120.0}
        view = app._build_portfolio_view()
        self.assertEqual(view["held_count"], 1)
        h = view["holdings"][0]
        self.assertEqual(h["ticker"], "AAA")
        self.assertAlmostEqual(h["gain_pct"], 20.0)
        self.assertEqual(h["days_held"], len(self.cal) - 1 - 5)
        self.assertFalse(h["profit_taken"])


if __name__ == "__main__":
    unittest.main()
