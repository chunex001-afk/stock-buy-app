# -*- coding: utf-8 -*-
"""Q5中線割れ：購入中断 補助表示(design 2026-10-02ユーザー確定仕様)のテスト。

refresh._update_quintile_state / _update_q5_midline_halt を、日次バッチの
複数回呼び出しを再現するend-to-endテストとして検証する
(test_q5_cool_and_warning.pyと同じ方針: Redis・Twelve Dataへは一切
アクセスせず、storeをフェイクに差し替える)。

仕様(ユーザー確定、2026-10-02):
- 判定は終値ベース。SMA20はstock_logic.sma(closes, 20)(既存実装と同一)。
- Q5 Day0〜Day7(8取引日)以内に一度でも終値がSMA20を下回ったら
  「購入中断」をactiveにする。Day8以降の初回割れは対象外。
- activeになったら、終値がSMA20以上を3取引日連続で維持するまで解除しない
  (Q4への降格・Q5シグナル失効・新しいQ5サイクル開始のいずれでもリセット
  されない)。解除前に再び割れたら連続維持日数を0にリセットする。
- データ不足・取得失敗の日は状態を変更しない。

Q1〜Q5判定ロジック(quintile_logic.assign_quintile)は変更していない。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import refresh

# score>=4ならQ5、3<=score<4ならQ4、2<=score<3ならQ3(quintile_logic.assign_quintile)
BOUNDS = [1, 2, 3, 4]
SCORE_Q5 = 10.0
SCORE_Q4 = 3.5
SCORE_Q3 = 2.5

WARMUP_N = 60
BASE_PRICE = 100.0   # 中線(SMA20)がおおよそ100になるよう、ウォームアップ期間は一定値にする
DOWN_PRICE = 50.0    # 明確に中線を下回る値
UP_PRICE = 150.0     # 明確に中線を上回る値


class FakeStore:
    """refresh.store の get/set_quintile_state だけを差し替える簡易フェイク。
    「前回状態を読み、今回状態を書く」というRedis相当の動作だけを再現する。"""

    def __init__(self):
        self.states = {}

    def get_quintile_state(self, ticker):
        return self.states.get(ticker)

    def set_quintile_state(self, ticker, state):
        self.states[ticker] = dict(state)


def _dates(n):
    start = date(2026, 2, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def run_days(fake_store, ticker, days):
    """days: [{"date":..., "score":..., "price": float|None}, ...] を日付順に
    処理し、最終状態を返す(各呼び出し後の状態もlistで返す)。

    priceがNoneの日は「当日の取得に失敗した」ケースを模し、
    price/dates/closesのすべてをNoneのまま_update_quintile_stateに渡す
    (run_quintile_refresh本体が未取得銘柄に対して行うのと同じ渡し方)。
    """
    states = []
    trading_calendar = []
    live_prices = []
    warmup_dates = [f"W{i:03d}" for i in range(WARMUP_N)]

    with mock.patch.object(refresh.store, "get_quintile_state", side_effect=fake_store.get_quintile_state), \
            mock.patch.object(refresh.store, "set_quintile_state", side_effect=fake_store.set_quintile_state):
        for day in days:
            trading_calendar.append(day["date"])
            if day["price"] is None:
                state = refresh._update_quintile_state(
                    ticker, day["score"], BOUNDS, day["date"],
                    price=None, trading_calendar=list(trading_calendar), dates=None, closes=None,
                )
            else:
                live_prices.append(day["price"])
                closes = [BASE_PRICE] * WARMUP_N + live_prices
                dates = warmup_dates + [d["date"] for d in days[:len(live_prices)]]
                state = refresh._update_quintile_state(
                    ticker, day["score"], BOUNDS, day["date"],
                    price=day["price"], trading_calendar=list(trading_calendar), dates=dates, closes=closes,
                )
            states.append(state)
    return states


def halt_of(state):
    return state.get("q5_midline_halt") or {}


class Day0To7TriggerTests(unittest.TestCase):
    """テスト1〜3: Q5 Day0〜Day7(8取引日)以内の初回割れのみ購入中断にする。"""

    def test_1_break_on_day0_triggers_halt(self):
        d = _dates(1)
        days = [{"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE}]  # Day0で割れ
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertTrue(halt["active"])
        self.assertEqual(halt["break_date"], d[0])
        self.assertEqual(halt["recover_streak"], 0)

    def test_2_first_break_on_day7_triggers_halt(self):
        d = _dates(8)
        days = [{"date": d[i], "score": SCORE_Q5, "price": UP_PRICE} for i in range(7)]  # Day0〜Day6: 維持
        days.append({"date": d[7], "score": SCORE_Q5, "price": DOWN_PRICE})  # Day7で初めて割れ
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertTrue(halt["active"])
        self.assertEqual(halt["break_date"], d[7])

    def test_3_first_break_on_day8_does_not_trigger_halt(self):
        d = _dates(9)
        days = [{"date": d[i], "score": SCORE_Q5, "price": UP_PRICE} for i in range(8)]  # Day0〜Day7: 維持
        days.append({"date": d[8], "score": SCORE_Q5, "price": DOWN_PRICE})  # Day8で初めて割れ(対象外)
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertFalse(halt["active"])
        self.assertIsNone(halt["break_date"])


class UnlockStreakTests(unittest.TestCase):
    """テスト4〜7: 解除は終値がSMA20以上を3取引日連続維持した時点。
    解除前に再び割れたら連続維持日数は0にリセットされる。"""

    def test_4_continues_during_recovery_day1_and_day2(self):
        d = _dates(3)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # Day0: 割れ→active
            {"date": d[1], "score": SCORE_Q5, "price": UP_PRICE},    # 回復1日目
            {"date": d[2], "score": SCORE_Q5, "price": UP_PRICE},    # 回復2日目
        ]
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertTrue(halt["active"])  # まだ解除されない
        self.assertEqual(halt["recover_streak"], 2)

    def test_5_unlocks_after_3_consecutive_days_above(self):
        d = _dates(4)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},
            {"date": d[1], "score": SCORE_Q5, "price": UP_PRICE},
            {"date": d[2], "score": SCORE_Q5, "price": UP_PRICE},
            {"date": d[3], "score": SCORE_Q5, "price": UP_PRICE},  # 3取引日連続維持→解除
        ]
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertFalse(halt["active"])
        self.assertEqual(halt["recover_streak"], 0)
        self.assertIsNone(halt["break_date"])

    def test_6_rebreak_before_unlock_resets_streak_to_zero(self):
        d = _dates(3)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # 割れ→active
            {"date": d[1], "score": SCORE_Q5, "price": UP_PRICE},    # 回復1日目(streak=1)
            {"date": d[2], "score": SCORE_Q5, "price": DOWN_PRICE},  # 再び割れ→streak=0にリセット
        ]
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertTrue(halt["active"])
        self.assertEqual(halt["recover_streak"], 0)

    def test_7_unlocks_after_recovering_again_post_rebreak(self):
        d = _dates(6)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # 割れ→active
            {"date": d[1], "score": SCORE_Q5, "price": UP_PRICE},    # streak=1
            {"date": d[2], "score": SCORE_Q5, "price": DOWN_PRICE},  # 再び割れ→streak=0
            {"date": d[3], "score": SCORE_Q5, "price": UP_PRICE},    # 回復1日目
            {"date": d[4], "score": SCORE_Q5, "price": UP_PRICE},    # 回復2日目
            {"date": d[5], "score": SCORE_Q5, "price": UP_PRICE},    # 回復3日目→解除
        ]
        states = run_days(FakeStore(), "TST", days)
        halt = halt_of(states[-1])
        self.assertFalse(halt["active"])


class PersistenceAcrossStateChangesTests(unittest.TestCase):
    """テスト8〜10: Q4への降格・Q5シグナル失効相当の経過・新しいQ5サイクルの
    開始のいずれでも、既存のactive状態は勝手にリセットされない。"""

    def test_8_continues_through_drop_to_q4(self):
        d = _dates(4)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # Q5で割れ→active
            {"date": d[1], "score": SCORE_Q4, "price": UP_PRICE},    # Q4に降格、回復1日目
            {"date": d[2], "score": SCORE_Q4, "price": UP_PRICE},    # Q4のまま、回復2日目
            {"date": d[3], "score": SCORE_Q4, "price": UP_PRICE},    # Q4のまま、回復3日目→解除
        ]
        states = run_days(FakeStore(), "TST", days)
        # 降格の影響を受けず、途中までactiveのまま続いたことを確認
        self.assertTrue(halt_of(states[1])["active"])
        self.assertTrue(halt_of(states[2])["active"])
        # Q4のままでも3日連続維持で解除されることを確認(Qレベルに依存しない)
        self.assertFalse(halt_of(states[3])["active"])

    def test_9_continues_past_signal_expiry_length(self):
        """Q5シグナル(app.py、8取引日で失効)がとっくに失効している経過日数が
        経っても、中断・解除判定自体は独立して継続することを確認する
        (ここではrefresh.py側のロジックのみを検証し、app.py側のq5_signalは
        呼び出していない=独立性の裏付け)。"""
        d = _dates(14)
        days = [{"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE}]  # Day0で割れ→active
        for i in range(1, 11):  # 10取引日(Q5シグナルの有効期限8日を超える)割れたまま継続
            days.append({"date": d[i], "score": SCORE_Q5, "price": DOWN_PRICE})
        days.append({"date": d[11], "score": SCORE_Q5, "price": UP_PRICE})  # 回復1日目
        days.append({"date": d[12], "score": SCORE_Q5, "price": UP_PRICE})  # 回復2日目
        days.append({"date": d[13], "score": SCORE_Q5, "price": UP_PRICE})  # 回復3日目→解除
        states = run_days(FakeStore(), "TST", days)
        self.assertTrue(halt_of(states[10])["active"])  # 10取引日経過時点でもactiveのまま
        self.assertFalse(halt_of(states[-1])["active"])  # 3日連続維持でようやく解除

    def test_10_new_q5_cycle_does_not_reset_existing_active_halt(self):
        d = _dates(5)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # Q5 Day0で割れ→active
            {"date": d[1], "score": SCORE_Q3, "price": DOWN_PRICE},  # Q5から完全に離脱(Q3)
            {"date": d[2], "score": SCORE_Q3, "price": DOWN_PRICE},  # Q3のまま
            {"date": d[3], "score": SCORE_Q5, "price": DOWN_PRICE},  # 新しいQ5サイクル開始(新Day0)
            {"date": d[4], "score": SCORE_Q5, "price": DOWN_PRICE},  # 新サイクルのDay1
        ]
        states = run_days(FakeStore(), "TST", days)
        for s in states:
            h = halt_of(s)
            self.assertTrue(h["active"])
            # 新しいQ5サイクルが始まっても、break_dateは最初の割れ日のまま
            # (新サイクルによってリセット・上書きされていないことの確認)
            self.assertEqual(h["break_date"], d[0])


class DataGapTests(unittest.TestCase):
    """テスト11〜12: データ不足・取得失敗時に誤って解除・リセットされないこと、
    解除後は次回更新でも解除状態が維持されること。"""

    def test_11_data_gap_day_does_not_change_state(self):
        d = _dates(3)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},  # 割れ→active
            {"date": d[1], "score": SCORE_Q5, "price": None},         # 当日の取得失敗
            {"date": d[2], "score": SCORE_Q5, "price": UP_PRICE},     # 翌日は正常(回復1日目)
        ]
        states = run_days(FakeStore(), "TST", days)
        halt_after_gap = halt_of(states[1])
        self.assertTrue(halt_after_gap["active"])
        self.assertEqual(halt_after_gap["recover_streak"], 0)  # 変化していない
        self.assertEqual(halt_after_gap["break_date"], d[0])   # 変化していない
        # 取得失敗を挟んでも、翌日の回復判定自体は正しく継続する
        self.assertEqual(halt_of(states[2])["recover_streak"], 1)

    def test_12_unlocked_state_persists_on_subsequent_normal_days(self):
        d = _dates(5)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},
            {"date": d[1], "score": SCORE_Q5, "price": UP_PRICE},
            {"date": d[2], "score": SCORE_Q5, "price": UP_PRICE},
            {"date": d[3], "score": SCORE_Q5, "price": UP_PRICE},  # 3日連続維持→解除
            {"date": d[4], "score": SCORE_Q5, "price": UP_PRICE},  # 解除後の通常日
        ]
        states = run_days(FakeStore(), "TST", days)
        self.assertFalse(halt_of(states[3])["active"])
        self.assertFalse(halt_of(states[4])["active"])  # 誤って再active化していない


class ExistingLogicUnaffectedTests(unittest.TestCase):
    """既存のQ1〜Q5判定(current_q/history/pred_score)が、今回の追加ロジックの
    影響を一切受けていないことを確認する(test_q5_cool_and_warning.pyの
    ExistingQuintileLogicUnaffectedTestsと同じ方針)。"""

    def test_current_q_and_history_unaffected_by_midline_halt(self):
        d = _dates(4)
        days = [
            {"date": d[0], "score": SCORE_Q5, "price": DOWN_PRICE},
            {"date": d[1], "score": SCORE_Q4, "price": UP_PRICE},
            {"date": d[2], "score": SCORE_Q4, "price": UP_PRICE},
            {"date": d[3], "score": SCORE_Q4, "price": UP_PRICE},
        ]
        states = run_days(FakeStore(), "TST", days)
        self.assertEqual(states[0]["current_q"], "Q5")
        self.assertEqual(states[0]["pred_score"], SCORE_Q5)
        self.assertEqual(states[-1]["current_q"], "Q4")
        self.assertEqual(states[-1]["previous_q"], "Q4")
        self.assertEqual(
            states[-1]["history"],
            [{"date": d[0], "q": "Q5"}, {"date": d[1], "q": "Q4"}],
        )


if __name__ == "__main__":
    unittest.main()
