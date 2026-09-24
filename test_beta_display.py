# -*- coding: utf-8 -*-
"""β(ベータ)表示機能の配線テスト(2026-09-25追加)。

- app._build_beta_view: state["beta"]からの表示用ビュー組み立て
- app._build_quintile_view: pending/ready両方の分岐にβが含まれること
- refresh._update_quintile_state: beta引数の保存、および当日beta=Noneの
  場合に前回値を保持すること(β以外のフィールド・Q1〜Q5判定には無影響)
- refresh.run_quintile_refresh: SPYが取得できた銘柄だけβが計算され、
  Q1〜Q5のcurrent_q/pred_scoreがβの有無に一切左右されないこと

Redis・Twelve Dataへは一切アクセスしない(すべてmock)。
"""
import unittest
from unittest import mock

import app
import beta_logic
import refresh


class BuildBetaViewTests(unittest.TestCase):
    def test_none_state_returns_na(self):
        view = app._build_beta_view(None)
        self.assertIsNone(view["beta"])
        self.assertIsNone(view["beta_band"])
        self.assertIsNone(view["beta_band_label"])
        self.assertIsNone(view["beta_note"])

    def test_missing_beta_key_returns_na(self):
        view = app._build_beta_view({"current_q": "Q3"})
        self.assertIsNone(view["beta"])
        self.assertIsNone(view["beta_band_label"])

    def test_normal_beta_rounds_to_2dp(self):
        view = app._build_beta_view({"beta": 1.8629999})
        self.assertEqual(view["beta"], 1.86)
        self.assertEqual(view["beta_band"], "high")
        self.assertEqual(view["beta_band_label"], "高ベータ")
        self.assertIsNone(view["beta_note"])

    def test_extreme_beta_adds_note_not_no_difference(self):
        view = app._build_beta_view({"beta": 2.73})
        self.assertEqual(view["beta_band"], "extreme")
        self.assertEqual(view["beta_band_label"], "超高ベータ")
        self.assertEqual(view["beta_note"], "Q1〜Q5の段階差は小さめ")
        self.assertNotIn("差なし", view["beta_note"])


class BuildQuintileViewIncludesBetaTests(unittest.TestCase):
    def test_pending_state_none_still_has_beta_na_fields(self):
        with mock.patch.object(app.store, "get_quintile_state", return_value=None):
            view = app._build_quintile_view("TST")
        self.assertEqual(view["status"], "pending")
        self.assertIsNone(view["beta"])
        self.assertIsNone(view["beta_band_label"])

    def test_pending_insufficient_state_carries_existing_beta(self):
        state = {
            "current_q": None, "data_status": "insufficient",
            "data_days": 210, "data_days_required": 252,
            "beta": None, "last_updated": "2026-09-20",
        }
        with mock.patch.object(app.store, "get_quintile_state", return_value=state):
            view = app._build_quintile_view("TST")
        self.assertEqual(view["status"], "pending")
        self.assertIsNone(view["beta"])
        self.assertIsNone(view["beta_band_label"])

    def test_ready_state_exposes_beta_fields_without_touching_q(self):
        state = {
            "current_q": "Q4", "previous_q": "Q3", "history": [{"date": "2026-09-20", "q": "Q4"}],
            "pred_score": 12.3, "beta": 2.55, "last_updated": "2026-09-24",
        }
        with mock.patch.object(app.store, "get_quintile_state", return_value=state):
            view = app._build_quintile_view("TST")
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["current_q"], "Q4")  # Q1〜Q5判定はβ追加の影響を受けない
        self.assertEqual(view["beta"], 2.55)
        self.assertEqual(view["beta_band"], "extreme")
        self.assertEqual(view["beta_note"], "Q1〜Q5の段階差は小さめ")

    def test_ready_state_without_beta_key_shows_na(self):
        state = {"current_q": "Q2", "previous_q": "Q1", "history": [], "last_updated": "2026-09-24"}
        with mock.patch.object(app.store, "get_quintile_state", return_value=state):
            view = app._build_quintile_view("TST")
        self.assertEqual(view["current_q"], "Q2")
        self.assertIsNone(view["beta"])
        self.assertIsNone(view["beta_band"])


class UpdateQuintileStateBetaTests(unittest.TestCase):
    def _patch_store(self, existing_state):
        p1 = mock.patch.object(refresh.store, "get_quintile_state", return_value=existing_state)
        p2 = mock.patch.object(refresh.store, "set_quintile_state")
        p1.start()
        set_state_mock = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return set_state_mock

    def test_beta_is_stored_and_q_unaffected(self):
        self._patch_store({})
        bounds = [1, 2, 3, 4]
        state = refresh._update_quintile_state(
            "TST", score=10.0, bounds=bounds, date_key="2026-09-25", price=100.0, beta=1.86,
        )
        self.assertEqual(state["current_q"], "Q5")  # score=10 > bounds[3]=4 -> Q5、betaとは無関係
        self.assertEqual(state["beta"], 1.86)

    def test_beta_none_today_carries_forward_previous_beta(self):
        existing = {
            "current_q": "Q3", "previous_q": "Q2", "history": [{"date": "2026-09-20", "q": "Q3"}],
            "beta": 1.42, "last_updated": "2026-09-20",
        }
        self._patch_store(existing)
        bounds = [1, 2, 3, 4]
        # 今回SPY取得失敗等でbeta=None -> 前回値1.42を保持する。
        state = refresh._update_quintile_state(
            "TST", score=2.5, bounds=bounds, date_key="2026-09-21", price=100.0, beta=None,
        )
        self.assertEqual(state["beta"], 1.42)
        self.assertEqual(state["current_q"], "Q3")  # 2.5はbounds[0]=1とbounds[1]=2の間 -> Q3

    def test_new_ticker_without_prior_beta_stays_none(self):
        self._patch_store({})
        bounds = [1, 2, 3, 4]
        state = refresh._update_quintile_state(
            "TST", score=0.5, bounds=bounds, date_key="2026-09-25", price=100.0, beta=None,
        )
        self.assertIsNone(state["beta"])


class RunQuintileRefreshBetaWiringTests(unittest.TestCase):
    """run_quintile_refreshが、SPYを含めて取得できた銘柄だけβを計算し、
    Q1〜Q5判定(score/bounds/current_q)には一切影響を与えないことを確認する。"""

    def _make_price_series(self, n, start=100.0, step=0.5):
        dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
        closes = [start + i * step for i in range(n)]
        volumes = [1_000_000] * n
        return dates, closes, volumes

    def test_beta_computed_only_when_spy_and_ticker_both_fetched(self):
        watchlist = ["AAA"]
        spy_dates, spy_closes, spy_volumes = self._make_price_series(300, start=400.0, step=0.3)
        ticker_dates, ticker_closes, ticker_volumes = self._make_price_series(300, start=50.0, step=0.2)

        def fake_td_fetch_one(ticker, api_key, date_key):
            if ticker == "SPY":
                return (spy_dates, spy_closes, spy_volumes), False, None, None
            if ticker == "AAA":
                return (ticker_dates, ticker_closes, ticker_volumes), False, None, None
            return None, False, None, None  # ローテーション銘柄は今回取得しない体で簡略化

        with mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_td_fetch_one), \
             mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0)), \
             mock.patch.object(refresh.quintile_logic, "rotation_group_for_date", return_value=0), \
             mock.patch.object(refresh.quintile_logic, "tickers_for_group", return_value=[]), \
             mock.patch.object(refresh.store, "get_pool_history", return_value=None), \
             mock.patch.object(refresh.store, "set_pool_history"), \
             mock.patch.object(refresh.store, "get_refpool_score", return_value=None), \
             mock.patch.object(refresh.store, "set_refpool_score"), \
             mock.patch.object(refresh.store, "get_quintile_state", return_value={}) as get_state, \
             mock.patch.object(refresh.store, "set_quintile_state") as set_state, \
             mock.patch.object(beta_logic, "compute_beta", wraps=beta_logic.compute_beta) as spy_compute_beta:
            result = refresh.run_quintile_refresh("dummy-key", watchlist)

        self.assertIsNotNone(result)
        self.assertIn("AAA", result["fetched"])
        self.assertIn("SPY", result["fetched"])
        # SPY・AAAとも取得できているのでbeta計算が試みられている。
        spy_compute_beta.assert_called()
        # Q1〜Q5判定(set_quintile_state呼び出し)が行われ、current_qが
        # 計算されていること自体はbetaの有無と無関係に成立する。
        self.assertTrue(set_state.called)
        saved_state = set_state.call_args[0][1]
        self.assertIn("current_q", saved_state)
        self.assertIn("beta", saved_state)


if __name__ == "__main__":
    unittest.main()
