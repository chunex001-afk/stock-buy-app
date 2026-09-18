# -*- coding: utf-8 -*-
"""登録銘柄数表示(「登録銘柄：○/15」)のテスト(2026-09-18追加)。

要件:
- /api/ranking が現在のwatchlist件数(rows)と既存のstock_logic.MAX_TICKERSを
  そのまま返すだけ(表示専用、Q1〜Q5判定・Q5経過状態には一切変更なし)。
Redis・Twelve Dataへは一切アクセスしない(store/refreshの呼び出しをmockする)。
"""
import unittest
from unittest import mock

import app
import stock_logic as logic


class TickerCountApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def _patch_common(self, watchlist):
        patchers = [
            mock.patch.object(app.store, "get_watchlist", return_value=watchlist),
            mock.patch.object(app.store, "get_ticker_record", return_value=None),
            mock.patch.object(app.store, "get_quintile_state", return_value=None),
            mock.patch.object(app.store, "get_last_refresh", return_value=None),
            mock.patch.object(app.refresh, "remaining_td_budget", return_value=(700, 0)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_max_tickers_matches_existing_constant(self):
        self._patch_common(["AAPL", "MSFT", "NVDA"])
        resp = self.client.get("/api/ranking")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["max_tickers"], logic.MAX_TICKERS)

    def test_rows_length_matches_watchlist_size(self):
        watchlist = ["AAPL", "MSFT", "NVDA", "AMD", "GOOG", "META", "TSLA", "AMZN"]
        self._patch_common(watchlist)
        resp = self.client.get("/api/ranking")
        data = resp.get_json()
        self.assertEqual(len(data["rows"]), len(watchlist))

    def test_empty_watchlist_falls_back_to_defaults(self):
        """空のwatchlistは既存仕様でlogic.DEFAULT_TICKERSにフォールバックする
        (_get_watchlist()の既存動作、今回の変更では触れていない)。
        件数表示もこのフォールバック後の件数と一致するべき。"""
        self._patch_common([])
        resp = self.client.get("/api/ranking")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["rows"]), len(logic.DEFAULT_TICKERS))
        self.assertEqual(data["max_tickers"], logic.MAX_TICKERS)

    def test_full_watchlist_at_max(self):
        watchlist = [f"T{i}" for i in range(logic.MAX_TICKERS)]
        self._patch_common(watchlist)
        resp = self.client.get("/api/ranking")
        data = resp.get_json()
        self.assertEqual(len(data["rows"]), logic.MAX_TICKERS)
        self.assertEqual(data["max_tickers"], logic.MAX_TICKERS)

    def test_response_still_has_existing_fields_unchanged(self):
        """既存フィールド(rows/last_refresh/budget)がmax_tickers追加後も
        壊れていないことを確認する(既存機能への回帰確認)。"""
        self._patch_common(["AAPL"])
        resp = self.client.get("/api/ranking")
        data = resp.get_json()
        self.assertIn("rows", data)
        self.assertIn("last_refresh", data)
        self.assertIn("budget", data)
        self.assertIn("max_tickers", data)


if __name__ == "__main__":
    unittest.main()
