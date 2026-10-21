# -*- coding: utf-8 -*-
"""Q5 48銘柄の将来成績をforward-onlyで蓄積する機能(design 2026-10-21)の
純粋ロジック(q5_universe_logic.py)のテスト。Redis/Twelve Dataには一切
アクセスしない(そもそもこのモジュール自体がアクセスしない)。
"""
import unittest
from datetime import date, timedelta

import q5_universe_logic as q5logic


def _business_days(n, start="2026-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(120)


class Day0DetectionTests(unittest.TestCase):
    """1〜4: Day0判定(前回Qの有無・Q5への新規遷移)。"""

    def test_1_no_prior_q_never_creates_day0_even_if_today_is_q5(self):
        """初回実行(prev_qが空、= そのティッカーの前回Qが未知)では、
        今日Q5でもDay0を作らない(過去を推定して初期化しない、design確定仕様)。"""
        prev_q = {}
        today_q = {"AAA": "Q5", "BBB": "Q3"}
        self.assertEqual(q5logic.detect_day0_tickers(prev_q, today_q), [])

    def test_2_q4_to_q5_creates_day0(self):
        prev_q = {"AAA": "Q4"}
        today_q = {"AAA": "Q5"}
        self.assertEqual(q5logic.detect_day0_tickers(prev_q, today_q), ["AAA"])

    def test_3_q5_to_q5_does_not_create_new_day0(self):
        prev_q = {"AAA": "Q5"}
        today_q = {"AAA": "Q5"}
        self.assertEqual(q5logic.detect_day0_tickers(prev_q, today_q), [])

    def test_4_q5_then_q4_then_q5_again_creates_second_day0(self):
        # 1回目: Q4->Q5 (Day0)
        self.assertEqual(q5logic.detect_day0_tickers({"AAA": "Q4"}, {"AAA": "Q5"}), ["AAA"])
        # その後Q5->Q4(離脱、Day0ではない)
        self.assertEqual(q5logic.detect_day0_tickers({"AAA": "Q5"}, {"AAA": "Q4"}), [])
        # 2回目: Q4->Q5 (別のDay0)
        self.assertEqual(q5logic.detect_day0_tickers({"AAA": "Q4"}, {"AAA": "Q5"}), ["AAA"])

    def test_other_quintiles_never_trigger_day0(self):
        prev_q = {"AAA": "Q1", "BBB": "Q2", "CCC": "Q3"}
        today_q = {"AAA": "Q2", "BBB": "Q3", "CCC": "Q4"}
        self.assertEqual(q5logic.detect_day0_tickers(prev_q, today_q), [])

    def test_multiple_tickers_day0_same_day(self):
        prev_q = {"AAA": "Q4", "BBB": "Q3", "CCC": "Q5"}
        today_q = {"AAA": "Q5", "BBB": "Q5", "CCC": "Q5"}
        self.assertEqual(sorted(q5logic.detect_day0_tickers(prev_q, today_q)), ["AAA", "BBB"])


class BuildNewEntriesTests(unittest.TestCase):
    def test_new_entry_has_day0_fields_and_null_checkpoints_no_shares(self):
        entries = q5logic.build_new_entries(["AAA"], "2026-02-10", {"AAA": 123.45})
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["ticker"], "AAA")
        self.assertEqual(e["day0_date"], "2026-02-10")
        self.assertEqual(e["day0_price"], 123.45)
        for n in (5, 20, 60):
            self.assertIsNone(e[f"day{n}_date"])
            self.assertIsNone(e[f"day{n}_price"])
        self.assertNotIn("shares", e)


class CheckpointFillTests(unittest.TestCase):
    """5〜8: Day5/20/60の記録、Q5離脱後も記録継続。

    design 2026-10-21修正: fill_checkpointsの第4引数はticker別の
    「今日の価格」ではなく、ticker別の「日付→終値」マップ
    (close_by_date_by_ticker、当日取得したOHLC応答に含まれる履歴全体)に
    変わった。「本来のチェックポイント日そのものの終値」を遡って探すため。
    """

    def setUp(self):
        self.day0_idx = 10
        self.day0_date = CAL[self.day0_idx]
        self.entry = {
            "ticker": "AAA", "day0_date": self.day0_date, "day0_price": 100.0,
            "day5_date": None, "day5_price": None,
            "day20_date": None, "day20_price": None,
            "day60_date": None, "day60_price": None,
        }

    def test_5_day5_recorded_exactly_at_5_business_days(self):
        target = CAL[self.day0_idx + 5]
        closes = {"AAA": {target: 110.0}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, target, closes)
        e = updated[0]
        self.assertEqual(e["day5_date"], target)
        self.assertEqual(e["day5_price"], 110.0)
        self.assertIsNone(e["day20_date"])
        self.assertIsNone(e["day60_date"])

    def test_6_day20_recorded_exactly_at_20_business_days(self):
        target = CAL[self.day0_idx + 20]
        closes = {"AAA": {target: 130.0}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, target, closes)
        e = updated[0]
        self.assertEqual(e["day20_date"], target)
        self.assertEqual(e["day20_price"], 130.0)

    def test_7_day60_recorded_exactly_at_60_business_days(self):
        target = CAL[self.day0_idx + 60]
        closes = {"AAA": {target: 150.0}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, target, closes)
        e = updated[0]
        self.assertEqual(e["day60_date"], target)
        self.assertEqual(e["day60_price"], 150.0)

    def test_8_checkpoints_continue_even_if_ticker_left_q5(self):
        """fill_checkpointsはQ(現在Q5かどうか)を一切参照しないため、
        Q5から離脱していても5/20/60の記録が継続することの確認
        (呼び出し側がtoday_qの状態に関わらず全entryへ適用する設計)。"""
        target = CAL[self.day0_idx + 20]
        closes = {"AAA": {target: 90.0}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, target, closes)
        self.assertEqual(updated[0]["day20_price"], 90.0)  # Q5離脱で値が下がっていても記録する

    def test_checkpoints_never_overwritten_once_set(self):
        target = CAL[self.day0_idx + 5]
        step1 = q5logic.fill_checkpoints([self.entry], CAL, target, {"AAA": {target: 110.0}})
        # 同じエントリに対し、別の(誤った)価格で再度呼んでも上書きされない
        step2 = q5logic.fill_checkpoints(step1, CAL, target, {"AAA": {target: 999.0}})
        self.assertEqual(step2[0]["day5_price"], 110.0)
        self.assertEqual(step2[0]["day5_date"], target)

    def test_day0_date_and_price_never_mutated_by_fill_checkpoints(self):
        target = CAL[self.day0_idx + 20]
        updated = q5logic.fill_checkpoints([self.entry], CAL, target, {"AAA": {target: 130.0}})
        self.assertEqual(updated[0]["day0_date"], self.day0_date)
        self.assertEqual(updated[0]["day0_price"], 100.0)

    def test_fill_checkpoints_does_not_mutate_input_list_or_dicts(self):
        target = CAL[self.day0_idx + 5]
        original = dict(self.entry)
        q5logic.fill_checkpoints([self.entry], CAL, target, {"AAA": {target: 110.0}})
        self.assertEqual(self.entry, original)  # 引数のentry自体は変更されない


class CheckpointCatchUpTests(unittest.TestCase):
    """design 2026-10-21修正の核心: チェックポイント日にちょうど実行でき
    なかった場合でも、「今日の価格で代用」せず、本来のチェックポイント日
    そのものの終値を、その後の取得分の履歴から遡って探して確定する。"""

    def setUp(self):
        self.day0_idx = 10
        self.day0_date = CAL[self.day0_idx]
        self.entry = {
            "ticker": "AAA", "day0_date": self.day0_date, "day0_price": 100.0,
            "day5_date": None, "day5_price": None,
            "day20_date": None, "day20_price": None,
            "day60_date": None, "day60_price": None,
        }

    def test_missed_exact_day_is_backfilled_from_later_fetch_history(self):
        """day0+20営業日目にジョブが実行できず、day0+21営業日目に復旧した
        場合。day0+21自身の終値(999.0)ではなく、履歴の中にあるday0+20
        そのものの終値(130.0)が記録される。"""
        target_day20 = CAL[self.day0_idx + 20]
        anchor_day21 = CAL[self.day0_idx + 21]
        closes = {"AAA": {target_day20: 130.0, anchor_day21: 999.0}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, anchor_day21, closes)
        e = updated[0]
        self.assertEqual(e["day20_date"], target_day20)  # 本来のチェックポイント日
        self.assertEqual(e["day20_price"], 130.0)  # 「今日の価格」(999.0)ではない

    def test_target_date_present_in_history_is_used(self):
        target = CAL[self.day0_idx + 5]
        anchor = CAL[self.day0_idx + 6]
        closes = {"AAA": {target: 111.11, anchor: 222.22}}
        updated = q5logic.fill_checkpoints([self.entry], CAL, anchor, closes)
        self.assertEqual(updated[0]["day5_date"], target)
        self.assertEqual(updated[0]["day5_price"], 111.11)

    def test_target_date_absent_from_history_stays_null(self):
        """対象日が取得した履歴の範囲に存在しない場合は、無理に埋めず
        nullのままにする(今日の価格での代用は行わない)。"""
        anchor = CAL[self.day0_idx + 21]
        closes = {"AAA": {anchor: 999.0}}  # target(day0_idx+20)が無い
        updated = q5logic.fill_checkpoints([self.entry], CAL, anchor, closes)
        self.assertIsNone(updated[0]["day20_date"])
        self.assertIsNone(updated[0]["day20_price"])

    def test_once_found_on_a_later_run_it_is_not_recomputed_again(self):
        target = CAL[self.day0_idx + 20]
        anchor_first = CAL[self.day0_idx + 21]
        step1 = q5logic.fill_checkpoints([self.entry], CAL, anchor_first, {"AAA": {target: 130.0}})
        self.assertEqual(step1[0]["day20_price"], 130.0)

        anchor_second = CAL[self.day0_idx + 25]
        step2 = q5logic.fill_checkpoints(step1, CAL, anchor_second, {"AAA": {target: 999.0}})
        self.assertEqual(step2[0]["day20_price"], 130.0)  # 既に確定済みなので上書きされない


class DuplicateDay0PerTickerTests(unittest.TestCase):
    """11: 同じtickerで複数回Day0が発生した場合、(ticker, day0_date)ごとに
    別イベントとして扱われることの確認(build_new_entriesは呼ばれた分だけ
    別entryを作るだけの単純な関数であることの確認、実際の重複実行防止は
    refresh_q5_universe.py側のlast_processed_dateチェックが担う)。"""

    def test_same_ticker_different_day0_dates_become_separate_entries(self):
        e1 = q5logic.build_new_entries(["AAA"], CAL[10], {"AAA": 100.0})
        e2 = q5logic.build_new_entries(["AAA"], CAL[30], {"AAA": 120.0})
        combined = e1 + e2
        self.assertEqual(len(combined), 2)
        self.assertEqual(combined[0]["day0_date"], CAL[10])
        self.assertEqual(combined[1]["day0_date"], CAL[30])
        # 2件は独立して以後の追記が行われる(混同されない)
        self.assertIsNot(combined[0], combined[1])


class LookAheadBiasTests(unittest.TestCase):
    """13: Look-ahead biasが発生しないことの確認。"""

    def test_detect_day0_uses_only_prev_and_today_q_nothing_else(self):
        """detect_day0_tickersはprev_q/today_qの2つの辞書以外、一切の
        外部情報(将来の日付・将来の価格)を受け取れない関数シグネチャに
        なっている(引数自体にそもそも未来情報を渡す経路が存在しない)。"""
        import inspect
        sig = inspect.signature(q5logic.detect_day0_tickers)
        self.assertEqual(list(sig.parameters.keys()), ["prev_q", "today_q"])

    def test_fill_checkpoints_result_for_past_entry_is_unaffected_by_future_anchor_change(self):
        """day20を既に確定したentryは、その後(day60等)より先のanchor_dateで
        再度fill_checkpointsを呼んでも、day20の値は変化しない(=後から
        将来情報を使って過去の確定値を書き換えない)ことの確認。"""
        day0_idx = 10
        entry = {
            "ticker": "AAA", "day0_date": CAL[day0_idx], "day0_price": 100.0,
            "day5_date": None, "day5_price": None,
            "day20_date": None, "day20_price": None,
            "day60_date": None, "day60_price": None,
        }
        target20, target60 = CAL[day0_idx + 20], CAL[day0_idx + 60]
        after_day20 = q5logic.fill_checkpoints([entry], CAL, target20, {"AAA": {target20: 140.0}})
        after_day60 = q5logic.fill_checkpoints(after_day20, CAL, target60, {"AAA": {target60: 300.0}})
        self.assertEqual(after_day60[0]["day20_price"], 140.0)  # day60到達後も day20 の値は不変
        self.assertEqual(after_day60[0]["day60_price"], 300.0)

    def test_day0_detection_for_a_date_does_not_depend_on_later_dates_data(self):
        """同じ(prev_q, today_q)を与えれば、呼び出す「時点」に関わらず常に
        同じDay0判定結果になる(将来のデータを一切参照しないため、入力が
        同じなら結果が変わらない=純粋関数であることの確認)。"""
        prev_q = {"AAA": "Q4"}
        today_q = {"AAA": "Q5"}
        result1 = q5logic.detect_day0_tickers(prev_q, today_q)
        result2 = q5logic.detect_day0_tickers(prev_q, today_q)
        self.assertEqual(result1, result2)


if __name__ == "__main__":
    unittest.main()
