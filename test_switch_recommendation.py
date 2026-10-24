# -*- coding: utf-8 -*-
"""Q5「乗り換え推奨」表示機能(design 2026-10-24)のテスト。

switch_recommendation_logic.compute_switch_recommendation(純粋関数、ユニット
テスト)と、app._compute_daily_actionsへの統合(switch_recommendationキーの
追加・既存のSELL/PROFIT/BUY判定が変わっていないことの確認)の両方を検証する。

Twelve Data・Redisへは一切アクセスしない。市場環境バイアス研究+最終ホール
ドアウト検証で選定された4条件(①現在Q5保有 ②AI_UNIVERSE<=-2.0%
③新Q5 Day0 ④prior_n>=10かつprior_mean 3.0〜4.5%)をそれぞれ単独で崩す
ケースを重点的に検証する。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import app
import switch_recommendation_logic as srl


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
    return {"ticker": ticker, "price": price, "quintile": {"status": status, "current_q": None, "history": history}}


def _day0_history(day0_date):
    return [{"date": day0_date, "q": "Q5"}]


def _ok_pm(mean, n=42):
    return {"status": "ok", "n": n, "mean": mean}


def _insufficient_pm(n=3):
    return {"status": "insufficient", "n": n, "mean": None}


# ---------------------------------------------------------------------------
# switch_recommendation_logic.compute_switch_recommendation (純粋関数)
# ---------------------------------------------------------------------------

class ComputeSwitchRecommendationUnitTests(unittest.TestCase):
    def _cand(self, ticker, mean, n=42):
        return {"ticker": ticker, "suggested_price": 100.0, "prior_mean": _ok_pm(mean, n)}

    def test_all_four_conditions_met_is_active(self):
        result = srl.compute_switch_recommendation(
            [self._cand("AAA", 3.7)], {"HELD": {}}, -2.5,
        )
        self.assertTrue(result["active"])
        self.assertEqual(result["candidates"], [{"ticker": "AAA", "prior_mean": 3.7, "prior_n": 42}])

    def test_no_positions_is_inactive_condition1(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 3.7)], {}, -3.0)
        self.assertFalse(result["active"])
        self.assertEqual(result["candidates"], [])

    def test_ai_universe_above_threshold_is_inactive_condition2(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 3.7)], {"HELD": {}}, -1.9)
        self.assertFalse(result["active"])

    def test_ai_universe_exactly_at_threshold_is_active_boundary(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 3.7)], {"HELD": {}}, -2.0)
        self.assertTrue(result["active"])

    def test_ai_universe_none_is_inactive(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 3.7)], {"HELD": {}}, None)
        self.assertFalse(result["active"])
        self.assertIsNone(result["ai_universe_daily_return"])

    def test_no_new_day0_candidates_is_inactive_condition3(self):
        result = srl.compute_switch_recommendation([], {"HELD": {}}, -3.0)
        self.assertFalse(result["active"])

    def test_prior_mean_below_3_0_excluded_condition4(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 2.9)], {"HELD": {}}, -3.0)
        self.assertFalse(result["active"])
        self.assertEqual(result["candidates"], [])

    def test_prior_mean_at_3_0_is_included_lower_bound(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 3.0)], {"HELD": {}}, -3.0)
        self.assertTrue(result["active"])

    def test_prior_mean_at_4_5_excluded_upper_bound_exclusive(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 4.5)], {"HELD": {}}, -3.0)
        self.assertFalse(result["active"])

    def test_prior_mean_just_below_4_5_is_included(self):
        result = srl.compute_switch_recommendation([self._cand("AAA", 4.4999)], {"HELD": {}}, -3.0)
        self.assertTrue(result["active"])

    def test_prior_n_below_10_insufficient_status_excluded_condition4(self):
        cand = {"ticker": "AAA", "suggested_price": 100.0, "prior_mean": _insufficient_pm(n=5)}
        result = srl.compute_switch_recommendation([cand], {"HELD": {}}, -3.0)
        self.assertFalse(result["active"])

    def test_mixed_candidates_only_qualifying_ones_included(self):
        cands = [
            self._cand("TOO_LOW", 1.0),
            self._cand("GOOD1", 3.2),
            {"ticker": "INSUFFICIENT", "suggested_price": 1.0, "prior_mean": _insufficient_pm()},
            self._cand("TOO_HIGH", 7.0),
            self._cand("GOOD2", 4.1),
        ]
        result = srl.compute_switch_recommendation(cands, {"HELD": {}}, -3.0)
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["GOOD1", "GOOD2"])

    def test_max_3_candidates_preserves_input_order(self):
        cands = [self._cand(f"T{i}", 3.5) for i in range(5)]
        result = srl.compute_switch_recommendation(cands, {"HELD": {}}, -3.0)
        self.assertEqual([c["ticker"] for c in result["candidates"]], ["T0", "T1", "T2"])

    def test_default_threshold_constant_is_minus_2_0(self):
        self.assertEqual(srl.AI_UNIVERSE_THRESHOLD_PCT, -2.0)

    def test_default_prior_mean_band_is_3_0_to_4_5(self):
        self.assertEqual(srl.PRIOR_MEAN_LO, 3.0)
        self.assertEqual(srl.PRIOR_MEAN_HI, 4.5)

    def test_max_candidates_is_3(self):
        self.assertEqual(srl.MAX_CANDIDATES, 3)


# ---------------------------------------------------------------------------
# app._compute_daily_actions への統合
# ---------------------------------------------------------------------------

class ComputeDailyActionsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fake_records = {}
        patcher = mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self.fake_records.get(t))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, rows, positions, anchor_idx, ai_universe_daily_return=None):
        portfolio_state = {"positions": positions, "last_updated": CAL[anchor_idx]}
        return app._compute_daily_actions(
            rows, portfolio_state, None, CAL[anchor_idx], CAL, ai_universe_daily_return,
        )

    def test_switch_recommendation_appears_when_all_conditions_met(self):
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = self._run(rows, {"HELD": {"entry_date": CAL[20], "entry_price": 10.0}}, anchor,
                                ai_universe_daily_return=-2.4)
        self.assertTrue(result["switch_recommendation"]["active"])
        self.assertEqual(result["switch_recommendation"]["candidates"][0]["ticker"], "NEWQ5")

    def test_held_ticker_never_appears_as_switch_target(self):
        # NEWQ5自体が既にpositionsにある(=保有中)場合、buy_candidatesの
        # 時点で除外されるため、switch_recommendationにも出てこないことを確認。
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = self._run(rows, {"NEWQ5": {"entry_date": CAL[20], "entry_price": 10.0}}, anchor,
                                ai_universe_daily_return=-2.4)
        self.assertEqual(result["switch_recommendation"]["candidates"], [])
        # 条件①(現在Q5保有)は満たすが、候補(条件③④)が無いためactive=False
        self.assertFalse(result["switch_recommendation"]["active"])

    def test_no_recommendation_without_holdings_even_if_market_weak_and_day0_exists(self):
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = self._run(rows, {}, anchor, ai_universe_daily_return=-5.0)
        self.assertFalse(result["switch_recommendation"]["active"])

    def test_no_recommendation_when_market_not_weak(self):
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = self._run(rows, {"HELD": {"entry_date": CAL[20], "entry_price": 10.0}}, anchor,
                                ai_universe_daily_return=-1.0)
        self.assertFalse(result["switch_recommendation"]["active"])

    def test_default_ai_universe_daily_return_none_keeps_backward_compatible_signature(self):
        # ai_universe_daily_returnを省略しても既存呼び出しが壊れないこと
        # (既存テスト・既存呼び出し元との後方互換性)。
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        portfolio_state = {"positions": {"HELD": {"entry_date": CAL[20], "entry_price": 10.0}}}
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = app._compute_daily_actions(rows, portfolio_state, None, today, CAL)
        self.assertIn("switch_recommendation", result)
        self.assertFalse(result["switch_recommendation"]["active"])

    def test_existing_buy_list_unaffected_by_switch_recommendation(self):
        # 乗り換え推奨が出ても、既存のBUY判定(free_slots・buy_list・
        # buy_candidates_total)は一切変わらないことを確認する。
        anchor = 30
        today = CAL[anchor]
        rows = [_row("NEWQ5", 50.0, _day0_history(today))]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.8)):
            result = self._run(rows, {"HELD": {"entry_date": CAL[20], "entry_price": 10.0}}, anchor,
                                ai_universe_daily_return=-2.4)
        self.assertEqual(len(result["buy"]), 1)
        self.assertEqual(result["buy"][0]["ticker"], "NEWQ5")
        self.assertEqual(result["buy_candidates_total"], 1)
        self.assertEqual(result["free_slots"], 5)  # 6枠-1保有


if __name__ == "__main__":
    unittest.main()
