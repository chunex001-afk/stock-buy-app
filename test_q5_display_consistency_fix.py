# -*- coding: utf-8 -*-
"""Q5履歴・経過日数表示の不整合修正(2026-09-29)のテスト。

ユーザー監査で確認された4件の不整合と、その修正内容:

① 過去5日のQ表示が暦日ベースになっていた
   → app._build_daily_breakdown / _trading_day_offset を取引日ベースに変更。
② q5_price_pathが、価格取得失敗があった日だけ追記されず、経過日数が
   ズレたまま復旧できなかった(BEの事例)
   → refresh._reconcile_price_path が、翌日以降のoutputsize=full取得
     (dates/closes)を使って欠測日を実際の終値で補完する。
③ Q5→Q4/Q3→再びQ5となった場合に、q5_price_pathが古いDay0を引きずり、
   q5_signal(常に最新のQ5を起点にする)と食い違っていた(MXL/AXTI/AEHRの事例)
   → refresh._true_q5_day0 / _reconcile_price_path が、historyから見た
     「本当のDay0」と食い違っていれば必ずリセットするように統一。
④ 現在Q5継続中の銘柄で、シグナルバッジが常に固定文言「Q5 Day 0」を表示し、
   実際の継続日数を示す経過バッジ(「Q5 Day N」)と数値が食い違って見えた
   (CIFR/AEHR/AXTI/MXL/NBISの事例)
   → app.q5SignalHtmlの「ok」時の固定文言を廃止。

Q1〜Q5判定ロジック(quintile_logic.py)・スコア計算・購入判定・ランキング・
Q5の8取引日シグナル有効期限・Q5警告ロジックは、いずれも変更していない
(既存のtest_quintile_logic.py/test_q5_cool_and_warning.py/test_beta_*.py等が
そのまま通ることで確認済み)。Redis・Twelve Dataへは一切アクセスしない。
"""
import unittest
from unittest import mock

import app
import refresh

BOUNDS = [1, 2, 3, 4]  # score>=4ならQ5、3<=score<4ならQ4、2<=<3ならQ3(assign_quintileの境界)
SCORE_Q5 = 10.0
SCORE_Q4 = 3.5
SCORE_Q3 = 2.5


class FakeStore:
    def __init__(self):
        self.states = {}

    def get_quintile_state(self, ticker):
        return self.states.get(ticker)

    def set_quintile_state(self, ticker, state):
        self.states[ticker] = dict(state)


def run_days(fake_store, ticker, days):
    """days: [(date_key, score, price, dates, closes, trading_calendar), ...] を
    日付順に処理する。dates/closes/trading_calendarを渡さない日(None)は
    「その日の取得が失敗した」ことを表す(historyはキャッシュscoreで更新
    されうるが、q5_price_pathへの当日分追記・補完はできない)。"""
    state = None
    with mock.patch.object(refresh.store, "get_quintile_state", side_effect=fake_store.get_quintile_state), \
            mock.patch.object(refresh.store, "set_quintile_state", side_effect=fake_store.set_quintile_state):
        for date_key, score, price, dates, closes, trading_calendar in days:
            state = refresh._update_quintile_state(
                ticker, score, BOUNDS, date_key, price=price,
                trading_calendar=trading_calendar, dates=dates, closes=closes,
            )
    return state


class BEScenarioTests(unittest.TestCase):
    """ユーザー報告の実例: BE 9/22 Q5→9/23 Q4→9/24 Q3→9/25(取得失敗でQ3のまま
    price_pathだけ欠測)→9/28 Q4。9/28時点で「前回Q5から4日」「Q5 Day 4」の
    両方が一致すること(修正前は3 vs 4でズレていた)。"""

    def test_be_gap_is_backfilled_and_day_counts_match(self):
        store = FakeStore()
        full_calendar = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
                          "2026-09-25", "2026-09-28"]

        def cal_upto(date_key):
            return [d for d in full_calendar if d <= date_key]

        days = [
            # 09-22: Q5突入、正常取得
            ("2026-09-22", SCORE_Q5, 100.0,
             ["2026-09-21", "2026-09-22"], [101.0, 100.0], cal_upto("2026-09-22")),
            # 09-23: Q4に降格、正常取得
            ("2026-09-23", SCORE_Q4, 98.0,
             ["2026-09-21", "2026-09-22", "2026-09-23"], [101.0, 100.0, 98.0], cal_upto("2026-09-23")),
            # 09-24: Q3に降格、正常取得
            ("2026-09-24", SCORE_Q3, 97.0,
             ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"],
             [101.0, 100.0, 98.0, 97.0], cal_upto("2026-09-24")),
            # 09-25: 取得失敗(BEの実例を再現)。scoreはキャッシュ値でQ3のまま
            # (=historyへの新規追記は無い)、price/dates/closesはNone。
            ("2026-09-25", SCORE_Q3, None, None, None, cal_upto("2026-09-25")),
            # 09-28: 取得が復旧。outputsize=full相当で09-25分の終値も含めて
            # 取得できたとする(実際のTwelve Data outputsize=fullの挙動)。
            ("2026-09-28", SCORE_Q4, 96.0,
             ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"],
             [101.0, 100.0, 98.0, 97.0, 97.5, 96.0], cal_upto("2026-09-28")),
        ]
        state = run_days(store, "BE", days)

        self.assertEqual(state["current_q"], "Q4")
        self.assertEqual(
            state["history"][-5:],
            [
                {"date": "2026-09-22", "q": "Q5"},
                {"date": "2026-09-23", "q": "Q4"},
                {"date": "2026-09-24", "q": "Q3"},
                {"date": "2026-09-28", "q": "Q4"},
            ][-5:],
        )
        # 09-25の欠測が補完され、5営業日分(09-22〜09-25,09-28)が揃っている。
        self.assertEqual(
            [e["date"] for e in state["q5_price_path"]],
            ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"],
        )
        # 09-25分は実際の終値(97.5)で補完されており、架空の値ではない。
        self.assertEqual(state["q5_price_path"][3], {"date": "2026-09-25", "price": 97.5})

        # app.py側の表示ロジックで、Q5 Day(progress)と前回Q5から○日(signal)が
        # 一致することを確認する(修正前は3 vs 4でズレていた)。
        progress = app._compute_q5_progress(state["q5_price_path"])
        self.assertEqual(progress["days_elapsed"], 4)
        self.assertEqual(progress["day0_date"], "2026-09-22")

        trading_calendar = full_calendar
        signal = app._compute_q5_signal(state["current_q"], state["history"], "2026-09-28", trading_calendar)
        self.assertEqual(signal["days_elapsed"], 4)
        self.assertEqual(signal["day0_date"], "2026-09-22")
        self.assertEqual(signal["status"], "active")


class MXLScenarioTests(unittest.TestCase):
    """ユーザー報告の実例: MXL 9/21 Q5→9/22 Q4→(取引日を挟み)9/28 Q5(再発)。
    9/28のQ5が新しいDay0になり、q5_progress/q5_signalの両方が一致すること
    (修正前はprogress側だけ9/21を起点にしたまま食い違っていた)。"""

    def test_mxl_reentry_uses_new_day0(self):
        store = FakeStore()
        full_calendar = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23",
                          "2026-09-24", "2026-09-25", "2026-09-28"]

        def cal_upto(date_key):
            return [d for d in full_calendar if d <= date_key]

        days = [
            ("2026-09-21", SCORE_Q5, 50.0, ["2026-09-18", "2026-09-21"], [49.0, 50.0], cal_upto("2026-09-21")),
            ("2026-09-22", SCORE_Q4, 48.0,
             ["2026-09-18", "2026-09-21", "2026-09-22"], [49.0, 50.0, 48.0], cal_upto("2026-09-22")),
            # 09-23〜09-25はQ4のまま変化なし(historyへの追記なし、取得は成功)。
            ("2026-09-23", SCORE_Q4, 47.0,
             ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"],
             [49.0, 50.0, 48.0, 47.0], cal_upto("2026-09-23")),
            # 09-28: 再びQ5に(genuineな再突入)。
            ("2026-09-28", SCORE_Q5, 55.0,
             ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"],
             [49.0, 50.0, 48.0, 47.0, 47.5, 47.2, 55.0], cal_upto("2026-09-28")),
        ]
        state = run_days(store, "MXL", days)

        self.assertEqual(state["current_q"], "Q5")
        self.assertEqual(state["history"][-1], {"date": "2026-09-28", "q": "Q5"})
        # 新しいDay0(09-28)だけの1件になっている(09-21の古いDay0を引きずらない)。
        self.assertEqual(state["q5_price_path"], [{"date": "2026-09-28", "price": 55.0}])

        progress = app._compute_q5_progress(state["q5_price_path"])
        self.assertEqual(progress["day0_date"], "2026-09-28")
        self.assertEqual(progress["days_elapsed"], 0)

        signal = app._compute_q5_signal(state["current_q"], state["history"], "2026-09-28", full_calendar)
        self.assertEqual(signal["day0_date"], "2026-09-28")
        self.assertEqual(signal["status"], "ok")


class DailyBreakdownTradingDayTests(unittest.TestCase):
    """①: app._build_daily_breakdownが暦日ではなく実取引日ベースで
    「今日〜5日前」を組み立てることを確認する(土日を挟んでもズレない)。"""

    def test_weekend_is_skipped_not_counted(self):
        # 2026-09-28(月)を起点に、直近の実取引日は09-25(金)まで連続、
        # 09-26/27(土日)はtrading_calendarに含まれない。
        trading_calendar = ["2026-09-21", "2026-09-22", "2026-09-23",
                             "2026-09-24", "2026-09-25", "2026-09-28"]
        history = [
            {"date": "2026-09-21", "q": "Q3"},
            {"date": "2026-09-22", "q": "Q5"},
            {"date": "2026-09-23", "q": "Q4"},
            {"date": "2026-09-24", "q": "Q3"},
            {"date": "2026-09-28", "q": "Q4"},
        ]
        breakdown = app._build_daily_breakdown(history, "Q4", "2026-09-28", trading_calendar)
        dates = [d["date"] for d in breakdown]
        qs = [d["q"] for d in breakdown]
        # 今日,昨日(金09-25),2日前(木09-24),3日前(水09-23),4日前(火09-22),5日前(月09-21)
        self.assertEqual(dates, [
            "2026-09-28", "2026-09-25", "2026-09-24", "2026-09-23", "2026-09-22", "2026-09-21",
        ])
        self.assertEqual(qs, ["Q4", "Q3", "Q3", "Q4", "Q5", "Q3"])
        # 修正前(暦日ベース)は3日前=Q3・4日前=Q3という誤表示になっていた。
        self.assertNotEqual(qs[3], "Q3")  # 3日前は正しくはQ4
        self.assertEqual(qs[4], "Q5")     # 4日前が正しくQ5と一致する

    def test_no_trading_calendar_returns_none_not_calendar_fallback(self):
        """trading_calendarが取得できない場合、暦日にフォールバックせず
        「今日」以外はNone(表示側で「—」)になることを確認する
        (架空の取引日を作らない設計)。"""
        history = [{"date": "2026-09-22", "q": "Q5"}]
        breakdown = app._build_daily_breakdown(history, "Q5", "2026-09-28", [])
        self.assertEqual(breakdown[0], {"label": "今日", "date": "2026-09-28", "q": "Q5"})
        for d in breakdown[1:]:
            self.assertIsNone(d["date"])
            self.assertIsNone(d["q"])


class Q5SignalDay0TextRemovedTests(unittest.TestCase):
    """④: シグナルバッジの固定文言「Q5 Day 0」が廃止されたことを確認する
    (CIFR等で経過バッジ「Q5 Day N」と数値が食い違って見えた問題の修正)。"""

    def test_hardcoded_q5_day0_text_not_in_source(self):
        # 修正前の実際のHTML断片(q5sigsub要素の中身)がもう存在しないことを
        # 確認する。コメント中の説明文はプレーンテキストで一致しないよう、
        # タグ込みの断片で厳密に判定する。
        self.assertNotIn('<span class="q5sigsub">Q5 Day 0</span>', app.HTML)


class ContinuationDaysTradingDayTests(unittest.TestCase):
    """①関連: 「Q5継続：N日」も取引日ベースになっていることを確認する。"""

    def test_continuation_days_uses_trading_calendar(self):
        trading_calendar = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23",
                             "2026-09-24", "2026-09-25", "2026-09-28"]
        history = [{"date": "2026-09-18", "q": "Q4"}, {"date": "2026-09-22", "q": "Q5"}]
        n = app._trading_days_between(trading_calendar, "2026-09-22", "2026-09-28")
        # 09-22,23,24,25,28の5取引日(土日はカウントしない)。
        self.assertEqual(n, 5)


if __name__ == "__main__":
    unittest.main()
