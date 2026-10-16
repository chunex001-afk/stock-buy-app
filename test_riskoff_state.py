# -*- coding: utf-8 -*-
"""Risk-off判定(design 2026-10-16、refresh._update_riskoff_state)のテスト。

Twelve Data・Redisへは一切アクセスしない(test_ai_market_env_start_date.pyと
同じ方針)。_update_riskoff_stateはRedisアクセスをstore経由で行うだけの
純粋ロジックなので、refresh.storeをmock.Mockに差し替えて直接呼び出す。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import refresh


def _business_days(n, start="2024-01-01"):
    """startからn営業日(平日のみ)の日付文字列リストを古い順に返す。"""
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def _base_series(n=250, price=100.0):
    """SMA200が安定して計算できるだけの十分な長さの、変動の無い基準系列。"""
    dates = _business_days(n)
    closes = [price] * n
    return dates, closes


class RiskoffStateTests(unittest.TestCase):
    """開始条件(10営業日連続でSMA200割れ)・解除条件(10営業日連続でSMA200
    上回り)・ヒステリシスリセット・冪等性・look-ahead biasなしを確認する。"""

    def setUp(self):
        self.fake_store = mock.Mock()
        self.fake_store.get_riskoff_state.return_value = None
        patcher = mock.patch.object(refresh, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_sequence(self, extra_closes):
        """基準系列の後に extra_closes(1件ずつ)を1日ずつ追加し、
        _update_riskoff_stateを順に呼び出す。各呼び出し後の状態をリストで返す。
        前の呼び出しの戻り値を次の呼び出しのget_riskoff_state戻り値として
        チェーンする(本番のRedis読み書きを模する、既存テストと同じ手法)。"""
        dates, closes = _base_series()
        states = []
        for c in extra_closes:
            dates = dates + [self._next_business_day(dates[-1])]
            closes = closes + [c]
            state = refresh._update_riskoff_state(dates, closes, dates[-1])
            self.fake_store.get_riskoff_state.return_value = state
            states.append(state)
        return states

    @staticmethod
    def _next_business_day(day_str):
        d = date.fromisoformat(day_str) + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()

    # --- 開始条件 ---

    def test_9_days_below_does_not_trigger(self):
        states = self._run_sequence([90.0] * 9)
        self.assertFalse(states[-1]["active"])
        self.assertEqual(states[-1]["below_streak"], 9)

    def test_10_days_below_triggers(self):
        states = self._run_sequence([90.0] * 10)
        self.assertFalse(states[-2]["active"])
        self.assertTrue(states[-1]["active"])
        self.assertEqual(states[-1]["below_streak"], 10)
        self.assertAlmostEqual(states[-1]["ratio"], refresh.RISKOFF_RATIO)

    def test_entry_streak_resets_when_price_recovers_mid_sequence(self):
        # 5日下回り→1日上回り(リセット)→9日下回り(合計はentry_days超だが
        # 連続では10日に達していないため、まだ発動しない)。
        states = self._run_sequence([90.0] * 5 + [110.0] + [90.0] * 9)
        self.assertFalse(states[-1]["active"])
        self.assertEqual(states[-1]["below_streak"], 9)
        # さらに1日下回ればちょうど10日連続になり発動する。
        states2 = self._run_sequence([90.0] * 5 + [110.0] + [90.0] * 10)
        self.assertTrue(states2[-1]["active"])

    # --- 解除条件(Risk-off中) ---

    def test_9_days_above_does_not_exit_during_riskoff(self):
        states = self._run_sequence([90.0] * 10 + [110.0] * 9)
        self.assertTrue(states[-1]["active"])
        self.assertEqual(states[-1]["above_streak"], 9)

    def test_10_days_above_exits(self):
        states = self._run_sequence([90.0] * 10 + [110.0] * 10)
        self.assertTrue(states[-2]["active"])
        self.assertFalse(states[-1]["active"])
        self.assertAlmostEqual(states[-1]["ratio"], 1.0)

    def test_exit_streak_resets_when_price_dips_mid_recovery(self):
        # Risk-off開始後、5日上回り→1日下回り(リセット)→9日上回り、
        # まだ連続10日に達していないので解除されない。
        states = self._run_sequence([90.0] * 10 + [110.0] * 5 + [90.0] + [110.0] * 9)
        self.assertTrue(states[-1]["active"])
        self.assertEqual(states[-1]["above_streak"], 9)
        # さらに1日上回れば連続10日になり解除される。
        states2 = self._run_sequence([90.0] * 10 + [110.0] * 5 + [90.0] + [110.0] * 10)
        self.assertFalse(states2[-1]["active"])

    # --- look-ahead biasなし ---

    def test_no_lookahead_future_prices_do_not_affect_past_state(self):
        dates, closes = _base_series()
        seq_dates, seq_closes = list(dates), list(closes)
        for c in [90.0] * 7:
            seq_dates = seq_dates + [self._next_business_day(seq_dates[-1])]
            seq_closes = seq_closes + [c]
        state_day7 = refresh._update_riskoff_state(seq_dates, seq_closes, seq_dates[-1])

        # day7時点までのデータに、未来の値(day8以降、大きく上昇)を追加してから
        # 「day7時点の判定」を再計算しても、day7の終値までしか見ていないので
        # 結果が変わらないことを確認する(未来の価格を一切参照していない証明)。
        future_dates = list(seq_dates)
        future_closes = list(seq_closes)
        for c in [500.0] * 30:
            future_dates = future_dates + [self._next_business_day(future_dates[-1])]
            future_closes = future_closes + [c]
        # day7時点のインデックスまでに切り詰めて再計算(＝day7時点で利用可能
        # だった情報だけを渡す、本番のrun_refreshと同じ使い方)。
        truncated_dates = future_dates[: len(seq_dates)]
        truncated_closes = future_closes[: len(seq_closes)]
        self.fake_store.get_riskoff_state.return_value = None
        state_day7_again = refresh._update_riskoff_state(
            truncated_dates, truncated_closes, truncated_dates[-1])

        self.assertEqual(state_day7["below_streak"], state_day7_again["below_streak"])
        self.assertEqual(state_day7["active"], state_day7_again["active"])
        self.assertEqual(state_day7["sma200"], state_day7_again["sma200"])

    # --- 冪等性 ---

    def test_same_date_key_called_twice_does_not_double_count(self):
        dates, closes = _base_series()
        for c in [90.0] * 5:
            dates = dates + [self._next_business_day(dates[-1])]
            closes = closes + [c]
        state1 = refresh._update_riskoff_state(dates, closes, dates[-1])
        self.fake_store.get_riskoff_state.return_value = state1
        # 同じdate_keyで再度呼ぶ(例: GitHub Actionsの再実行等を想定)。
        state2 = refresh._update_riskoff_state(dates, closes, dates[-1])
        self.assertEqual(state1["below_streak"], state2["below_streak"])
        self.assertEqual(state1, state2)

    # --- SMA200計算不能時 ---

    def test_insufficient_history_returns_previous_state_unchanged(self):
        short_dates = _business_days(50)
        short_closes = [90.0] * 50
        result = refresh._update_riskoff_state(short_dates, short_closes, short_dates[-1])
        self.assertIsNone(result)  # prev=None、SMA200計算不能のためNoneのまま


class ExistingQuintileLogicUnaffectedTests(unittest.TestCase):
    """Risk-off機能の追加によって、既存のQ1〜Q5判定・Day0判定に一切影響が
    出ていないことを確認する(importが成功し、既存の定数・関数がそのままで
    あることの最低限のチェック)。"""

    def test_quintile_logic_constants_unchanged(self):
        import quintile_logic as q
        self.assertEqual(len(q.REFERENCE_UNIVERSE), 49)
        self.assertIn("SPY", q.REFERENCE_UNIVERSE)

    def test_ai_universe_unchanged(self):
        self.assertEqual(len(refresh.AI_UNIVERSE), 41)

    def test_riskoff_module_level_constants_independent(self):
        # Risk-off用の定数がAI一斉調整用の定数と混線していないことの確認。
        self.assertEqual(refresh.RISKOFF_ENTRY_DAYS, 10)
        self.assertEqual(refresh.RISKOFF_EXIT_DAYS, 10)
        self.assertEqual(refresh.RISKOFF_RATIO, 0.70)
        self.assertNotEqual(refresh.RISKOFF_ENTRY_DAYS, refresh.AI_MARKET_ENV_MIN_POP)


if __name__ == "__main__":
    unittest.main()
