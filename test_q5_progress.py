# -*- coding: utf-8 -*-
"""Q5「経過状態」表示機能のテスト(2026-09-18追加)。

- refresh._update_quintile_state の q5_price_path 積み上げロジック
  (新規Q5突入でリセット、Q5から外れても記録継続、再Q5突入で再リセット)
- app._compute_q5_progress / _progress_bucket の表示用ロジック(純粋関数)

既存のQ1〜Q5判定(current_q/previous_q/history/pred_score)への影響が
ないことも併せて確認する。Redis・Twelve Dataへは一切アクセスしない。
"""
import unittest
from unittest import mock

import app
import refresh


class ProgressBucketTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(app._progress_bucket(10.0)[0], "recover")
        self.assertEqual(app._progress_bucket(3.0)[0], "recover")  # 境界は上位側(recover)を含む
        self.assertEqual(app._progress_bucket(2.99)[0], "flat")
        self.assertEqual(app._progress_bucket(0.0)[0], "flat")
        self.assertEqual(app._progress_bucket(-2.99)[0], "flat")
        self.assertEqual(app._progress_bucket(-3.0)[0], "flat")  # 境界は上位側(flat)を含む[lo,hi)
        self.assertEqual(app._progress_bucket(-3.01)[0], "decline")
        self.assertEqual(app._progress_bucket(-7.49)[0], "decline")
        self.assertEqual(app._progress_bucket(-7.5)[0], "decline")  # 境界は上位側(decline)を含む[lo,hi)
        self.assertEqual(app._progress_bucket(-7.51)[0], "plunge")
        self.assertEqual(app._progress_bucket(-50.0)[0], "plunge")


class ComputeQ5ProgressTests(unittest.TestCase):
    def test_empty_path_returns_none(self):
        self.assertIsNone(app._compute_q5_progress([]))
        self.assertIsNone(app._compute_q5_progress(None))

    def test_day0_only(self):
        path = [{"date": "2026-09-01", "price": 100.0}]
        prog = app._compute_q5_progress(path)
        self.assertEqual(prog["days_elapsed"], 0)
        self.assertEqual(prog["current_return_pct"], 0.0)
        self.assertEqual(prog["current_state_key"], "flat")
        self.assertEqual(prog["day0_date"], "2026-09-01")
        self.assertEqual(len(prog["path"]), 1)

    def test_decline_then_recover_path(self):
        path = [
            {"date": "2026-09-01", "price": 100.0},  # Day0: 0%
            {"date": "2026-09-02", "price": 96.0},   # Day1: -4% (decline)
            {"date": "2026-09-03", "price": 92.0},   # Day2: -8% (plunge)
            {"date": "2026-09-04", "price": 90.0},   # Day3: -10% (plunge)
            {"date": "2026-09-05", "price": 99.0},   # Day4: -1% (flat, recovering)
            {"date": "2026-09-08", "price": 106.0},  # Day5: +6% (recover)
        ]
        prog = app._compute_q5_progress(path)
        self.assertEqual(prog["days_elapsed"], 5)
        self.assertAlmostEqual(prog["current_return_pct"], 6.0, places=2)
        self.assertEqual(prog["current_state_key"], "recover")
        # 経過(状態が変化した日だけ): flat(Day0) -> decline(Day1) -> plunge(Day2) -> flat(Day4) -> recover(Day5)
        state_keys = [p["state_emoji"] for p in prog["path"]]
        self.assertEqual(len(prog["path"]), 5)
        self.assertEqual(prog["path"][0]["day"], 0)
        self.assertEqual(prog["path"][-1]["day"], 5)
        # checkpointsにDay1/3/5の値が入っている
        self.assertIn(1, prog["checkpoints"])
        self.assertIn(3, prog["checkpoints"])
        self.assertIn(5, prog["checkpoints"])
        self.assertAlmostEqual(prog["checkpoints"][3], -10.0, places=2)

    def test_missing_price_entries_are_skipped_safely(self):
        path = [
            {"date": "2026-09-01", "price": 100.0},
            {"date": "2026-09-02", "price": None},
            {"date": "2026-09-03", "price": 105.0},
        ]
        prog = app._compute_q5_progress(path)
        self.assertIsNotNone(prog)
        self.assertEqual(prog["days_elapsed"], 2)  # Noneの日はスキップされ、次の有効日がday=2として使われる
        self.assertAlmostEqual(prog["current_return_pct"], 5.0, places=2)


class UpdateQuintileStatePricePathTests(unittest.TestCase):
    def _patch_store(self, existing_state):
        p1 = mock.patch.object(refresh.store, "get_quintile_state", return_value=existing_state)
        p2 = mock.patch.object(refresh.store, "set_quintile_state")
        p1.start()
        set_state_mock = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return set_state_mock

    def test_fresh_q5_entry_resets_price_path(self):
        existing = {
            "current_q": "Q3", "previous_q": "Q2", "history": [{"date": "2026-08-20", "q": "Q3"}],
            "q5_price_path": [{"date": "2026-08-10", "price": 50.0}],  # 前回のQ5サイクルの残骸
            "last_updated": "2026-08-20",
        }
        set_state = self._patch_store(existing)
        # score/boundsからq=="Q5"になるように調整
        bounds = [1, 2, 3, 4]
        state = refresh._update_quintile_state("TST", score=10.0, bounds=bounds, date_key="2026-09-01", price=100.0)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["q5_price_path"], [{"date": "2026-09-01", "price": 100.0}])
        set_state.assert_called_once()

    def test_continuing_q5_appends(self):
        existing = {
            "current_q": "Q5", "previous_q": "Q4",
            "history": [{"date": "2026-09-01", "q": "Q5"}],
            "q5_price_path": [{"date": "2026-09-01", "price": 100.0}],
            "last_updated": "2026-09-01",
        }
        self._patch_store(existing)
        bounds = [1, 2, 3, 4]
        state = refresh._update_quintile_state("TST", score=10.0, bounds=bounds, date_key="2026-09-02", price=103.0)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["q5_price_path"], [
            {"date": "2026-09-01", "price": 100.0},
            {"date": "2026-09-02", "price": 103.0},
        ])

    def test_dropping_out_of_q5_keeps_recording(self):
        """Q5から外れても(Q4に降格しても)、priceがあれば経過の記録を続ける。"""
        existing = {
            "current_q": "Q5", "previous_q": "Q5",
            "history": [{"date": "2026-09-01", "q": "Q5"}],
            "q5_price_path": [
                {"date": "2026-09-01", "price": 100.0},
                {"date": "2026-09-02", "price": 90.0},
            ],
            "last_updated": "2026-09-02",
        }
        self._patch_store(existing)
        bounds = [50, 60, 70, 80]  # scoreがこのbounds未満になるようにしてQ4に降格させる
        state = refresh._update_quintile_state("TST", score=40.0, bounds=bounds, date_key="2026-09-03", price=85.0)
        self.assertNotEqual(state["current_q"], "Q5")
        self.assertEqual(len(state["q5_price_path"]), 3)
        self.assertEqual(state["q5_price_path"][-1], {"date": "2026-09-03", "price": 85.0})

    def test_reentering_q5_resets_again(self):
        """Q5→Q4→Q5と再突入した場合、新しいDay0でリセットされる。"""
        existing = {
            "current_q": "Q4", "previous_q": "Q5",
            "history": [{"date": "2026-09-01", "q": "Q5"}, {"date": "2026-09-04", "q": "Q4"}],
            "q5_price_path": [
                {"date": "2026-09-01", "price": 100.0},
                {"date": "2026-09-02", "price": 90.0},
                {"date": "2026-09-03", "price": 88.0},
                {"date": "2026-09-04", "price": 85.0},
            ],
            "last_updated": "2026-09-04",
        }
        self._patch_store(existing)
        bounds = [1, 2, 3, 4]  # scoreがこれ以上ならQ5
        state = refresh._update_quintile_state("TST", score=10.0, bounds=bounds, date_key="2026-09-10", price=120.0)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["q5_price_path"], [{"date": "2026-09-10", "price": 120.0}])

    def test_no_price_available_does_not_crash_or_append(self):
        existing = {
            "current_q": "Q4", "previous_q": "Q5",
            "history": [{"date": "2026-09-01", "q": "Q5"}, {"date": "2026-09-04", "q": "Q4"}],
            "q5_price_path": [{"date": "2026-09-01", "price": 100.0}],
            "last_updated": "2026-09-04",
        }
        self._patch_store(existing)
        bounds = [50, 60, 70, 80]
        state = refresh._update_quintile_state("TST", score=40.0, bounds=bounds, date_key="2026-09-05", price=None)
        self.assertEqual(state["q5_price_path"], [{"date": "2026-09-01", "price": 100.0}])

    def test_max_cap_stops_growth_without_dropping_day0(self):
        old_max = refresh.MAX_Q5_PRICE_PATH_DAYS
        refresh.MAX_Q5_PRICE_PATH_DAYS = 3
        try:
            existing = {
                "current_q": "Q5", "previous_q": "Q5",
                "history": [{"date": "2026-09-01", "q": "Q5"}],
                "q5_price_path": [
                    {"date": "2026-09-01", "price": 100.0},
                    {"date": "2026-09-02", "price": 101.0},
                    {"date": "2026-09-03", "price": 102.0},
                ],
                "last_updated": "2026-09-03",
            }
            self._patch_store(existing)
            bounds = [1, 2, 3, 4]
            state = refresh._update_quintile_state("TST", score=10.0, bounds=bounds, date_key="2026-09-04", price=103.0)
            self.assertEqual(len(state["q5_price_path"]), 3)  # 増えていない
            self.assertEqual(state["q5_price_path"][0], {"date": "2026-09-01", "price": 100.0})  # Day0は失われない
        finally:
            refresh.MAX_Q5_PRICE_PATH_DAYS = old_max

    def test_existing_current_q_history_pred_score_unaffected(self):
        """既存のQ1〜Q5判定フィールド自体はq5_price_path追加の影響を受けない。"""
        existing = {
            "current_q": "Q3", "previous_q": "Q2", "history": [{"date": "2026-08-20", "q": "Q3"}],
            "last_updated": "2026-08-20",
        }  # q5_price_pathキー自体が無い(既存データ、未デプロイ時点のレコードを想定)
        self._patch_store(existing)
        bounds = [1, 2, 3, 4]
        state = refresh._update_quintile_state("TST", score=10.0, bounds=bounds, date_key="2026-09-01", price=100.0)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["previous_q"], "Q3")
        self.assertEqual(state["pred_score"], 10.0)
        self.assertEqual(state["history"][-1], {"date": "2026-09-01", "q": "Q5"})


if __name__ == "__main__":
    unittest.main()
