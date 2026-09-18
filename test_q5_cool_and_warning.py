# -*- coding: utf-8 -*-
"""Q5後5営業日固定クール・Day5注意喚起の正式実装テスト(design 2026-09-18)。

過去データ検証(scratchpadのq5_analysis、step13〜step16)で確認した以下の
仕様を、refresh._update_quintile_state / _advance_q5_warning の実装として
end-to-endで検証する(日次バッチの複数回呼び出しを再現する)。

- 「現在Q5かどうか」と「Q5後5営業日のクール」は別管理: クールの途中で
  Q4以下に戻っても、途中で再びQ5になってもクールはリセットされず、Day5で
  終了する。クール終了後に前日Q5でない→当日Q5となる genuine な再突入が
  起きた場合のみ、新しいクールDay0を開始する。
- 注意喚起はクールのDay5時点のQ5起点騰落率が-7.5%以下の場合にのみ発生する
  (Day1・Day3等の中間値やその経路は使わない)。
- 注意喚起は現在のQ5クールの状態とは別管理で、発生から最大40営業日保持し、
  それ以降は自動的に解除する。新しいQ5クールが始まっても、そのクールの
  Day5が-7.5%より上に回復しても、自動解除しない。

Redis・Twelve Dataへは一切アクセスしない(storeをフェイクに差し替える)。
Q1〜Q5判定ロジック自体(quintile_logic.assign_quintile)は変更していない。
"""
import unittest
from unittest import mock

import refresh

# score>=4ならQ5、3<=score<4ならQ4(quintile_logic.assign_quintileの境界仕様)
BOUNDS = [1, 2, 3, 4]
SCORE_Q5 = 10.0
SCORE_Q4 = 3.5


class FakeStore:
    """refresh.store の get/set_quintile_state だけを差し替える簡易フェイク。
    「前回状態を読み、今回状態を書く」というRedis相当の動作だけを再現する。"""

    def __init__(self):
        self.states = {}

    def get_quintile_state(self, ticker):
        return self.states.get(ticker)

    def set_quintile_state(self, ticker, state):
        self.states[ticker] = dict(state)


def run_days(fake_store, ticker, days):
    """days: [(date_key, score, price), ...] を日付順に処理し、最終状態を返す。"""
    state = None
    with mock.patch.object(refresh.store, "get_quintile_state", side_effect=fake_store.get_quintile_state), \
            mock.patch.object(refresh.store, "set_quintile_state", side_effect=fake_store.set_quintile_state):
        for date_key, score, price in days:
            state = refresh._update_quintile_state(ticker, score, BOUNDS, date_key, price=price)
    return state


class Q5CoolTests(unittest.TestCase):
    """テストA/B/C: 5営業日固定クールの進行(design 8章A/B/C)。"""

    def test_a_normal_5_business_day_progression(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 98.0),   # Day1
            ("2026-01-07", SCORE_Q5, 97.0),   # Day2
            ("2026-01-08", SCORE_Q5, 96.0),   # Day3
            ("2026-01-09", SCORE_Q5, 95.0),   # Day4
            ("2026-01-12", SCORE_Q5, 94.0),   # Day5
        ]
        state = run_days(store, "TST", days)
        self.assertEqual(len(state["q5_price_path"]), 6)
        self.assertEqual(state["q5_price_path"][0], {"date": "2026-01-05", "price": 100.0})
        self.assertEqual(state["q5_price_path"][-1], {"date": "2026-01-12", "price": 94.0})

    def test_b_q4_dip_mid_cool_does_not_end_cool(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 98.0),   # Day1
            ("2026-01-07", SCORE_Q4, 97.0),   # Day2: Q4に降格してもクールは継続
            ("2026-01-08", SCORE_Q4, 96.0),   # Day3
            ("2026-01-09", SCORE_Q4, 95.0),   # Day4
            ("2026-01-12", SCORE_Q4, 94.0),   # Day5
        ]
        state = run_days(store, "TST", days)
        self.assertEqual(len(state["q5_price_path"]), 6)
        self.assertEqual(state["q5_price_path"][0], {"date": "2026-01-05", "price": 100.0})

    def test_c_reentering_q5_mid_cool_does_not_reset_day0(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 98.0),   # Day1
            ("2026-01-07", SCORE_Q4, 97.0),   # Day2: Q4に降格
            ("2026-01-08", SCORE_Q4, 96.0),   # Day3
            ("2026-01-09", SCORE_Q5, 95.0),   # Day4: 再びQ5になっても新Day0にしない
            ("2026-01-12", SCORE_Q5, 94.0),   # Day5
        ]
        state = run_days(store, "TST", days)
        self.assertEqual(len(state["q5_price_path"]), 6)
        self.assertEqual(state["q5_price_path"][0], {"date": "2026-01-05", "price": 100.0})
        self.assertEqual(state["current_q"], "Q5")


class Q5WarningTriggerTests(unittest.TestCase):
    """テストD/E/F: 注意喚起はDay5時点の値のみで判定する(design 8章D/E/F)。"""

    def test_d_day1_minus10_but_day5_minus3_no_warning(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 90.0),   # Day1: -10%
            ("2026-01-07", SCORE_Q5, 95.0),   # Day2
            ("2026-01-08", SCORE_Q5, 95.0),   # Day3
            ("2026-01-09", SCORE_Q5, 96.0),   # Day4
            ("2026-01-12", SCORE_Q5, 97.0),   # Day5: -3%
        ]
        state = run_days(store, "TST", days)
        self.assertIsNone(state["q5_warning"])

    def test_e_day3_minus8_but_day5_minus3_no_warning(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 99.0),   # Day1
            ("2026-01-07", SCORE_Q5, 97.0),   # Day2
            ("2026-01-08", SCORE_Q5, 92.0),   # Day3: -8%
            ("2026-01-09", SCORE_Q5, 95.0),   # Day4
            ("2026-01-12", SCORE_Q5, 97.0),   # Day5: -3%
        ]
        state = run_days(store, "TST", days)
        self.assertIsNone(state["q5_warning"])

    def test_f_day3_minus5_and_day5_minus8_triggers_warning(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),  # Day0
            ("2026-01-06", SCORE_Q5, 98.0),   # Day1
            ("2026-01-07", SCORE_Q5, 96.0),   # Day2
            ("2026-01-08", SCORE_Q5, 95.0),   # Day3: -5%
            ("2026-01-09", SCORE_Q5, 93.0),   # Day4
            ("2026-01-12", SCORE_Q5, 92.0),   # Day5: -8%
        ]
        state = run_days(store, "TST", days)
        self.assertIsNotNone(state["q5_warning"])
        self.assertAlmostEqual(state["q5_warning"]["triggered_return_pct"], -8.0, places=2)
        self.assertEqual(state["q5_warning"]["days_since_trigger"], 0)
        self.assertEqual(state["q5_warning"]["triggered_date"], "2026-01-12")


class Q5WarningPersistenceTests(unittest.TestCase):
    """テストG/H/I/J: 注意喚起の保持・非解除・40営業日失効(design 8章G/H/I/J)。"""

    TRIGGER_DAYS = [
        ("2026-01-05", SCORE_Q5, 100.0),  # Day0
        ("2026-01-06", SCORE_Q5, 98.0),   # Day1
        ("2026-01-07", SCORE_Q5, 96.0),   # Day2
        ("2026-01-08", SCORE_Q5, 95.0),   # Day3
        ("2026-01-09", SCORE_Q5, 93.0),   # Day4
        ("2026-01-12", SCORE_Q5, 90.0),   # Day5: -10% → 注意喚起発生
    ]

    def test_g_warning_persists_after_dropping_to_q4(self):
        store = FakeStore()
        days = list(self.TRIGGER_DAYS) + [
            ("2026-01-13", SCORE_Q4, 91.0),
            ("2026-01-14", SCORE_Q4, 92.0),
        ]
        state = run_days(store, "TST", days)
        self.assertIsNotNone(state["q5_warning"])
        self.assertEqual(state["q5_warning"]["triggered_date"], "2026-01-12")
        self.assertEqual(state["q5_warning"]["days_since_trigger"], 2)

    def test_h_new_cool_recovery_does_not_clear_warning(self):
        store = FakeStore()
        days = list(self.TRIGGER_DAYS) + [
            ("2026-01-13", SCORE_Q4, 91.0),   # Q4に降格
            ("2026-01-14", SCORE_Q4, 92.0),
            ("2026-01-15", SCORE_Q5, 95.0),   # 新クール②Day0(genuineな再突入、旧クールは既にDay5終了済み)
            ("2026-01-16", SCORE_Q5, 96.0),   # Day1
            ("2026-01-19", SCORE_Q5, 97.0),   # Day2
            ("2026-01-20", SCORE_Q5, 98.0),   # Day3
            ("2026-01-21", SCORE_Q5, 99.0),   # Day4
            ("2026-01-22", SCORE_Q5, 99.75),  # Day5: 旧クール②Day0(95.0)から+5%(回復)
        ]
        state = run_days(store, "TST", days)
        # クール②のDay0(95.0)から見て、Day5(99.75)は約+5%(回復)
        self.assertAlmostEqual((99.75 / 95.0 - 1) * 100, 5.0, places=1)
        # それでも古い注意(2026-01-12発生)は解除されず、値もそのまま
        self.assertIsNotNone(state["q5_warning"])
        self.assertEqual(state["q5_warning"]["triggered_date"], "2026-01-12")
        self.assertAlmostEqual(state["q5_warning"]["triggered_return_pct"], -10.0, places=2)

    def test_i_warning_expires_after_40_business_days(self):
        store = FakeStore()
        days = list(self.TRIGGER_DAYS)
        # トリガー日(2026-01-12)以降、39日分の追加呼び出し → まだ解除されない
        for i in range(39):
            days.append((f"2026-D{i:03d}", SCORE_Q4, 100.0))
        state = run_days(store, "TST", days)
        self.assertIsNotNone(state["q5_warning"])
        self.assertEqual(state["q5_warning"]["days_since_trigger"], 39)

        # さらに1日進める(トリガーから合計40営業日経過)→ 解除される
        state = run_days(store, "TST", [("2026-D039", SCORE_Q4, 100.0)])
        self.assertIsNone(state["q5_warning"])

    def test_j_expiry_timing_unaffected_by_new_cool_starting(self):
        store = FakeStore()
        days = list(self.TRIGGER_DAYS)
        # トリガー日(2026-01-12)自体はdays_since_trigger=0(この呼び出し以降の
        # 各呼び出しが1営業日ずつ経過日数を進める)
        elapsed = 0

        # トリガー後、旧クール終了後に新クール③を1本挟む(Day5も-7.5%以下で
        # 再度悪化するが、既存の注意は上書きされない=最初の発生日が基準のまま)
        extra_days = [
            ("2026-D000", SCORE_Q4, 100.0),
            ("2026-D001", SCORE_Q4, 100.0),
            ("2026-D002", SCORE_Q5, 100.0),  # 新クール③Day0
            ("2026-D003", SCORE_Q5, 100.0),  # Day1
            ("2026-D004", SCORE_Q5, 100.0),  # Day2
            ("2026-D005", SCORE_Q5, 100.0),  # Day3
            ("2026-D006", SCORE_Q5, 100.0),  # Day4
            ("2026-D007", SCORE_Q5, 80.0),   # Day5: -20%(再度悪化するが上書きしない)
        ]
        days += extra_days
        elapsed += len(extra_days)

        # トリガーから合計39営業日経過するまで、汎用のQ4呼び出しで埋める
        remaining = 39 - elapsed
        for i in range(remaining):
            days.append((f"2026-E{i:03d}", SCORE_Q4, 100.0))
        elapsed += remaining
        self.assertEqual(elapsed, 39)

        state = run_days(store, "TST", days)
        self.assertIsNotNone(state["q5_warning"])
        self.assertEqual(state["q5_warning"]["triggered_date"], "2026-01-12")  # 上書きされていない
        self.assertEqual(state["q5_warning"]["days_since_trigger"], 39)

        state = run_days(store, "TST", [("2026-E999", SCORE_Q4, 100.0)])
        self.assertIsNone(state["q5_warning"])  # 40営業日経過で解除


class ExistingQuintileLogicUnaffectedTests(unittest.TestCase):
    """テストK: Q1〜Q5判定結果(current_q/previous_q)が変更されていないこと。"""

    def test_current_q_follows_only_score_and_bounds(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),
            ("2026-01-06", SCORE_Q4, 90.0),
            ("2026-01-07", SCORE_Q5, 80.0),
        ]
        state = run_days(store, "TST", days)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["previous_q"], "Q4")

    def test_current_q_unaffected_when_price_is_missing(self):
        store = FakeStore()
        days = [
            ("2026-01-05", SCORE_Q5, 100.0),
            ("2026-01-06", SCORE_Q5, None),  # 価格取得失敗でもQ判定自体は動く
        ]
        state = run_days(store, "TST", days)
        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["previous_q"], "Q5")


if __name__ == "__main__":
    unittest.main()
