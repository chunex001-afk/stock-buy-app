# -*- coding: utf-8 -*-
"""GET /api/compare/q5universe(design 2026-10-21)の結合テスト。読み取り
専用APIで、trade_history・q5universe:state・positions等、既存のどのキー
にも書き込まない。Twelve Data・Upstashへは一切アクセスしない(store側の
該当関数のみmockで差し替える)。
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


CAL = _business_days(150)


class CompareApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._q5_state = None
        self._calendar = list(CAL)

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=lambda: self._portfolio_state),
            mock.patch.object(app.store, "get_q5universe_state", side_effect=lambda: self._q5_state),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self):
        resp = self.client.get("/api/compare/q5universe")
        return resp, resp.get_json()


class BasicResponseShapeTests(CompareApiTestCase):
    def test_9_response_has_expected_shape(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[95], "exit_price": 110.0, "return_pct": 10.0},
        ]}
        self._q5_state = {"entries": [
            {"ticker": "BBB", "day0_date": CAL[0], "day0_price": 100.0,
             "day5_date": None, "day5_price": None,
             "day20_date": CAL[20], "day20_price": 120.0,
             "day60_date": None, "day60_price": None},
        ]}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["recent_window"].keys()), {"5", "20", "60", "cumulative"})
        self.assertEqual(set(data["all_time_by_horizon"].keys()), {"5", "20", "60"})
        self.assertEqual(len(data["q5_entries"]), 1)
        self.assertIn("day20_return_pct", data["q5_entries"][0])
        self.assertAlmostEqual(data["q5_entries"][0]["day20_return_pct"], 20.0)

    def test_cumulative_row_reflects_my_trade_and_q5_day20(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[10], "exit_price": 130.0, "return_pct": 30.0},
        ]}
        self._q5_state = {"entries": [
            {"ticker": "BBB", "day0_date": CAL[0], "day0_price": 100.0,
             "day5_date": None, "day5_price": None,
             "day20_date": CAL[20], "day20_price": 110.0,
             "day60_date": None, "day60_price": None},
        ]}
        resp, data = self._get()
        cumulative = data["recent_window"]["cumulative"]
        self.assertEqual(cumulative["mine"]["n"], 1)
        self.assertAlmostEqual(cumulative["mine"]["mean"], 30.0)
        self.assertEqual(cumulative["q5"]["n"], 1)
        self.assertAlmostEqual(cumulative["q5"]["mean"], 10.0)
        self.assertAlmostEqual(cumulative["diff"], 20.0)


class EmptyStateTests(CompareApiTestCase):
    """10: trade_history/q5universe:stateが未初期化(None)でもエラーに
    ならず0件として返る。"""

    def test_10_both_states_uninitialized_returns_zero_counts_not_error(self):
        self._portfolio_state = None
        self._q5_state = None
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        for horizon in ("5", "20", "60", "cumulative"):
            self.assertEqual(data["recent_window"][horizon]["mine"]["n"], 0)
            self.assertEqual(data["recent_window"][horizon]["q5"]["n"], 0)
        self.assertEqual(data["q5_entries"], [])

    def test_empty_trading_calendar_does_not_error(self):
        self._calendar = []
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": "2026-01-01", "entry_price": 100.0,
             "exit_date": "2026-01-10", "exit_price": 90.0, "return_pct": -10.0},
        ]}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        # 直近N営業日版はカレンダー無しのため0件、累計はカレンダー非依存なので反映される
        self.assertEqual(data["recent_window"]["20"]["mine"]["n"], 0)
        self.assertEqual(data["recent_window"]["cumulative"]["mine"]["n"], 1)


class ReadOnlyTests(CompareApiTestCase):
    """この新規APIが既存のどのキーにも書き込まないことの確認。"""

    def test_does_not_write_to_portfolio_or_q5universe_state(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[5], "exit_price": 105.0, "return_pct": 5.0},
        ]}
        self._q5_state = {"entries": []}
        with mock.patch.object(app.store, "set_portfolio_state") as set_portfolio, \
             mock.patch.object(app.store, "set_q5universe_state") as set_q5:
            resp, data = self._get()
            self.assertEqual(resp.status_code, 200)
            set_portfolio.assert_not_called()
            set_q5.assert_not_called()


if __name__ == "__main__":
    unittest.main()
