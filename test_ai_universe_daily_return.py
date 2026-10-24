# -*- coding: utf-8 -*-
"""refresh._update_ai_market_decline_stateに追加した"daily_ret"(ティッカー別)
・"ai_universe_daily_return"(AI_UNIVERSE41銘柄の当日騰落率の等加重平均、
design 2026-10-24、Q5乗り換え推奨機能専用)のテスト。

既存のATH・current_price・n_down・pct_down等の計算には一切影響しないこと
(test_ai_market_decline.pyの既存テストが全てPASSし続けることで別途保証
済み)、追加フィールドが「既に取得済みのcloses配列」だけから計算され、
追加のAPI呼び出しが発生しないことを重点的に確認する。
"""
import unittest
from unittest import mock

import refresh


def _series_with_prev_close(prev_close, current, n=300):
    """closes[-2]=prev_close, closes[-1]=currentになる系列を返す
    (daily_ret=(current/prev_close-1)*100のテスト用。ATH用のath引数とは
    独立に、「前日終値→当日終値」の変化だけを制御する)。"""
    dates = [f"2024-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)]
    closes = [prev_close * 0.5] * (n - 2) + [prev_close, current]
    volumes = [1000] * n
    return (dates, closes, volumes)


class AiUniverseDailyReturnTests(unittest.TestCase):
    def setUp(self):
        self.fake_store = mock.Mock()
        self.saved_state = None
        self.fake_store.get_ai_market_decline_state.side_effect = lambda: self.saved_state
        self.fake_store.set_ai_market_decline_state.side_effect = self._save
        p1 = mock.patch.object(refresh, "store", self.fake_store)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        p2.start()
        self.addCleanup(p2.stop)

    def _save(self, data):
        self.saved_state = data

    def _patch_fetch(self, prev_current_by_ticker):
        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            spec = prev_current_by_ticker.get(ticker, (100.0, 100.0))
            if spec is None:
                return None, False, "NO_DATA", "test", None
            prev_close, current = spec
            return _series_with_prev_close(prev_close, current), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

    def test_per_ticker_daily_ret_computed_from_last_two_closes(self):
        spec = {t: (100.0, 98.0) for t in refresh.AI_UNIVERSE}  # 全銘柄-2.0%
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        for t in refresh.AI_UNIVERSE:
            self.assertAlmostEqual(state["tickers"][t]["daily_ret"], -2.0, places=6)

    def test_ai_universe_daily_return_is_equal_weighted_mean(self):
        spec = {}
        for i, t in enumerate(refresh.AI_UNIVERSE):
            # 半分は-4%、半分は0%になるよう設定(等加重平均=-2%になるはず)
            spec[t] = (100.0, 96.0) if i < len(refresh.AI_UNIVERSE) // 2 else (100.0, 100.0)
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertIsNotNone(state["ai_universe_daily_return"])
        self.assertAlmostEqual(state["ai_universe_daily_return"], -2.0, places=1)

    def test_matches_backtest_threshold_example_minus_2_point_0(self):
        spec = {t: (100.0, 98.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertLessEqual(state["ai_universe_daily_return"], -2.0)

    def test_positive_day_gives_positive_ai_universe_daily_return(self):
        spec = {t: (100.0, 103.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertAlmostEqual(state["ai_universe_daily_return"], 3.0, places=1)

    def test_no_additional_api_calls_beyond_existing_41(self):
        # 既存のoutputsize=5000の取得をそのまま使い、daily_ret専用の追加
        # fetchは一切発生しないこと(呼び出し回数=41のまま)を確認する。
        calls = []

        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            calls.append(ticker)
            return _series_with_prev_close(100.0, 98.0), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

        refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(len(calls), 41)

    def test_existing_ath_and_current_price_fields_unaffected(self):
        spec = {t: (100.0, 98.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        ticker = refresh.AI_UNIVERSE[0]
        # 既存フィールドは従来通り存在し、値も変わらない
        self.assertIn("ath", state["tickers"][ticker])
        self.assertIn("current_price", state["tickers"][ticker])
        self.assertEqual(state["tickers"][ticker]["current_price"], 98.0)
        self.assertEqual(state["n_pop"], 41)

    def test_partial_failure_still_does_not_save_even_with_new_field(self):
        spec = {t: (100.0, 98.0) for t in refresh.AI_UNIVERSE}
        spec[refresh.AI_UNIVERSE[0]] = None
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertFalse(state.get("date"))
        self.fake_store.set_ai_market_decline_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
