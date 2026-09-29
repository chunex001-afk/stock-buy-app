# -*- coding: utf-8 -*-
"""Q5 Day表示(q5_progress)の表示条件修正(2026-09-30監査対応)のテスト。

監査で確認された不整合(現在Q5でない・直近5取引日にQ5が無い・q5_signalが
「失効」なのに「Q5 Day N」が表示され続ける)への対応として、
app._build_quintile_view内で「q5_signalが存在し、かつstatusがexpiredでない
間だけq5_progressを表示する」というUI表示条件のみを追加した。

このテストで確認する不変条件:
- q5_progressの計算ロジック(app._compute_q5_progress)自体は無変更
- q5_price_path/history/q5_warningのデータは削除・書き換えしない
  (view["q5_progress"]がNoneになっても、元のstateは変更されない)
- current_q(Q1〜Q5判定)・pred_score・historyの内容には一切影響しない

Redis・Twelve Dataへは一切アクセスしない(すべてmock)。
"""
import unittest
from unittest import mock

import app

# 15取引日分の連続する平日カレンダー(土日を含まない)。
TRADING_CALENDAR = [
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
    "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
]


def _build_view(state):
    with mock.patch.object(app.store, "get_quintile_state", return_value=state):
        return app._build_quintile_view("TST", trading_calendar=TRADING_CALENDAR)


class Q5ProgressActiveSignalTests(unittest.TestCase):
    """「Q5→Q4/Q3だがq5_signalがactive」: Q5 Dayを表示すること。"""

    def test_recently_departed_q5_with_active_signal_shows_progress(self):
        state = {
            "current_q": "Q4", "previous_q": "Q5",
            "history": [
                {"date": "2026-08-06", "q": "Q3"},
                {"date": "2026-08-10", "q": "Q5"},
                {"date": "2026-08-11", "q": "Q4"},
            ],
            "q5_price_path": [
                {"date": "2026-08-10", "price": 100.0},
                {"date": "2026-08-11", "price": 98.0},
                {"date": "2026-08-12", "price": 99.0},
                {"date": "2026-08-13", "price": 101.0},
            ],
            "last_updated": "2026-08-13",
        }
        view = _build_view(state)

        self.assertEqual(view["current_q"], "Q4")  # Q1〜Q5判定はこの修正の影響を受けない
        self.assertIsNotNone(view["q5_signal"])
        self.assertEqual(view["q5_signal"]["status"], "active")
        self.assertIsNotNone(view["q5_progress"])
        self.assertEqual(view["q5_progress"]["day0_date"], "2026-08-10")
        self.assertEqual(view["q5_progress"]["days_elapsed"], 3)
        # 元データは無変更(参照した4件のq5_price_pathがそのまま残っている)
        self.assertEqual(len(state["q5_price_path"]), 4)


class Q5ProgressExpiredSignalTests(unittest.TestCase):
    """「q5_signal=expired」: Q5 Dayを表示しないこと。"""

    def test_expired_signal_suppresses_progress(self):
        state = {
            "current_q": "Q3", "previous_q": "Q4",
            "history": [
                {"date": "2026-08-03", "q": "Q5"},
                {"date": "2026-08-04", "q": "Q4"},
                {"date": "2026-08-05", "q": "Q3"},
            ],
            # 古いクールの"残骸"(Day0〜Day5、6件でクール完走・以後追記なし)
            "q5_price_path": [
                {"date": "2026-08-03", "price": 100.0},
                {"date": "2026-08-04", "price": 133.9},
                {"date": "2026-08-05", "price": 130.0},
                {"date": "2026-08-06", "price": 128.0},
                {"date": "2026-08-07", "price": 131.0},
                {"date": "2026-08-10", "price": 133.9},
            ],
            "last_updated": "2026-08-20",
        }
        view = _build_view(state)

        self.assertIsNotNone(view["q5_signal"])
        self.assertEqual(view["q5_signal"]["status"], "expired")
        self.assertIsNone(view["q5_progress"])
        # 失効していてもcurrent_q・q5_price_pathの生データ自体は破壊されていない
        self.assertEqual(view["current_q"], "Q3")
        self.assertEqual(len(state["q5_price_path"]), 6)


class Q5ProgressNoSignalTests(unittest.TestCase):
    """「q5_signal=None」(一度もQ5になっていない): Q5 Dayを表示しないこと。"""

    def test_never_q5_has_no_signal_and_no_progress(self):
        state = {
            "current_q": "Q3", "previous_q": "Q2",
            "history": [
                {"date": "2026-08-03", "q": "Q2"},
                {"date": "2026-08-10", "q": "Q3"},
            ],
            "q5_price_path": [],
            "last_updated": "2026-08-20",
        }
        view = _build_view(state)

        self.assertIsNone(view["q5_signal"])
        self.assertIsNone(view["q5_progress"])

    def test_stale_leftover_price_path_without_q5_in_history_is_still_suppressed(self):
        """historyにQ5が無いのにq5_price_pathだけ古いデータが残っている
        (Redisの不整合)ケースでも、q5_signal=Noneならq5_progressは表示しない
        防御的なケース。"""
        state = {
            "current_q": "Q3", "previous_q": "Q2",
            "history": [
                {"date": "2026-08-03", "q": "Q2"},
                {"date": "2026-08-10", "q": "Q3"},
            ],
            "q5_price_path": [
                {"date": "2026-07-01", "price": 50.0},
                {"date": "2026-07-02", "price": 55.0},
            ],
            "last_updated": "2026-08-20",
        }
        view = _build_view(state)

        self.assertIsNone(view["q5_signal"])
        self.assertIsNone(view["q5_progress"])
        # 生データはそのまま(削除・書き換えしない)
        self.assertEqual(len(state["q5_price_path"]), 2)


class Q5ProgressNewEntryResetTests(unittest.TestCase):
    """「新しいQ5発生時」: 既存のDay0リセットが維持されること(表示条件の
    変更が、既存のリセット挙動に影響していないことの確認)。"""

    def test_new_q5_entry_shows_fresh_day0_progress(self):
        state = {
            "current_q": "Q5", "previous_q": "Q4",
            "history": [
                {"date": "2026-08-03", "q": "Q5"},   # 古いクール(既に離脱済み)
                {"date": "2026-08-04", "q": "Q4"},
                {"date": "2026-08-17", "q": "Q5"},   # 新しいクール(現在Q5)
            ],
            "q5_price_path": [{"date": "2026-08-17", "price": 120.0}],
            "last_updated": "2026-08-17",
        }
        view = _build_view(state)

        self.assertEqual(view["current_q"], "Q5")
        self.assertEqual(view["q5_signal"]["status"], "ok")
        self.assertEqual(view["q5_signal"]["day0_date"], "2026-08-17")
        self.assertIsNotNone(view["q5_progress"])
        self.assertEqual(view["q5_progress"]["day0_date"], "2026-08-17")
        self.assertEqual(view["q5_progress"]["days_elapsed"], 0)


if __name__ == "__main__":
    unittest.main()
