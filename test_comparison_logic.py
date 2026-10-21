# -*- coding: utf-8 -*-
"""自分の実績とQ5 48銘柄の比較集計(design 2026-10-21、comparison_logic.py)
のテスト。Redis/Twelve Dataには一切アクセスしない(モジュール自体が非依存)。
"""
import unittest
from datetime import date, timedelta

import comparison_logic as cmp


def _business_days(n, start="2026-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(200)


def _trade(exit_date, return_pct):
    return {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
            "exit_date": exit_date, "exit_price": 100.0 * (1 + return_pct / 100), "return_pct": return_pct}


def _q5_entry(day0_price=100.0, day5=None, day20=None, day60=None):
    e = {"ticker": "AAA", "day0_date": CAL[0], "day0_price": day0_price,
         "day5_date": None, "day5_price": None,
         "day20_date": None, "day20_price": None,
         "day60_date": None, "day60_price": None}
    if day5:
        e["day5_date"], e["day5_price"] = day5
    if day20:
        e["day20_date"], e["day20_price"] = day20
    if day60:
        e["day60_date"], e["day60_price"] = day60
    return e


class AggregateTests(unittest.TestCase):
    def test_basic_mean_median_winrate(self):
        agg = cmp._aggregate([10.0, -10.0, 20.0])
        self.assertEqual(agg["n"], 3)
        self.assertAlmostEqual(agg["mean"], 20 / 3)
        self.assertEqual(agg["median"], 10.0)
        self.assertAlmostEqual(agg["win_rate"], 2 / 3 * 100)

    def test_empty_returns_none_not_crash(self):
        agg = cmp._aggregate([])
        self.assertEqual(agg, {"n": 0, "mean": None, "median": None, "win_rate": None})

    def test_zero_return_is_not_counted_as_win(self):
        agg = cmp._aggregate([0.0, 5.0])
        self.assertAlmostEqual(agg["win_rate"], 50.0)


class WindowBoundaryTests(unittest.TestCase):
    """境界値: anchor_idx - date_idx < N(0〜N-1営業日前を含み、N営業日前は除外)。"""

    def test_boundary_n_minus_1_days_ago_is_included(self):
        anchor_idx = 50
        self.assertTrue(cmp._within_recent_window(anchor_idx - 19, anchor_idx, 20))

    def test_boundary_exactly_n_days_ago_is_excluded(self):
        anchor_idx = 50
        self.assertFalse(cmp._within_recent_window(anchor_idx - 20, anchor_idx, 20))

    def test_today_zero_days_ago_is_included(self):
        anchor_idx = 50
        self.assertTrue(cmp._within_recent_window(anchor_idx, anchor_idx, 20))

    def test_none_date_idx_is_excluded(self):
        self.assertFalse(cmp._within_recent_window(None, 50, 20))

    def test_none_anchor_idx_is_excluded(self):
        self.assertFalse(cmp._within_recent_window(10, None, 20))


class TradeReturnsTests(unittest.TestCase):
    def test_trade_within_window_included(self):
        anchor = CAL[100]
        trades = [_trade(CAL[90], 15.0)]  # 10営業日前
        returns = cmp.trade_returns_in_window(trades, CAL, anchor, 20)
        self.assertEqual(returns, [15.0])

    def test_trade_outside_window_excluded(self):
        anchor = CAL[100]
        trades = [_trade(CAL[70], 15.0)]  # 30営業日前(20営業日ウィンドウ外)
        returns = cmp.trade_returns_in_window(trades, CAL, anchor, 20)
        self.assertEqual(returns, [])

    def test_cumulative_ignores_date_entirely(self):
        trades = [_trade(CAL[5], 1.0), _trade(CAL[150], 2.0)]
        self.assertEqual(sorted(cmp.trade_returns_cumulative(trades)), [1.0, 2.0])


class Q5ReturnsTests(unittest.TestCase):
    def test_q5_return_computed_from_day0_and_dayN_price(self):
        entry = _q5_entry(day0_price=100.0, day20=(CAL[20], 120.0))
        self.assertAlmostEqual(cmp._q5_return(entry, 20), 20.0)

    def test_q5_return_none_when_checkpoint_unresolved(self):
        """3: 未確定のdayN_price(None)は当該ホライズンの集計から除外される。"""
        entry = _q5_entry(day0_price=100.0)  # day20未確定
        self.assertIsNone(cmp._q5_return(entry, 20))
        self.assertEqual(cmp.q5_returns_all_time([entry], 20), [])

    def test_q5_returns_in_window_filters_by_dayN_date_not_day0_date(self):
        anchor = CAL[100]
        # day0は80営業日前(ウィンドウ外)だが、day20確定日は95営業日前->ではなく
        # 直近10営業日以内に設定して、フィルタがday0_dateでなくday20_dateを
        # 見ていることを確認する。
        entry = _q5_entry(day0_price=100.0, day20=(CAL[95], 110.0))
        returns = cmp.q5_returns_in_window([entry], CAL, anchor, 10, 20)
        self.assertEqual(len(returns), 1)
        self.assertAlmostEqual(returns[0], 10.0)

    def test_q5_returns_in_window_excludes_outside_window(self):
        anchor = CAL[100]
        entry = _q5_entry(day0_price=100.0, day20=(CAL[50], 110.0))  # 50営業日前
        returns = cmp.q5_returns_in_window([entry], CAL, anchor, 20, 20)
        self.assertEqual(returns, [])


class CumulativeDefinitionTests(unittest.TestCase):
    """4: 累計はday20リターンのみを使用し、day5/day60が混入しない。"""

    def test_cumulative_uses_only_day20_not_day5_or_day60(self):
        entry = _q5_entry(day0_price=100.0, day5=(CAL[5], 999.0),
                           day20=(CAL[20], 120.0), day60=(CAL[60], 1.0))
        returns = cmp.q5_returns_cumulative([entry])
        self.assertEqual(len(returns), 1)
        self.assertAlmostEqual(returns[0], 20.0)  # day5(899%相当)・day60が混ざらない

    def test_cumulative_excludes_entries_without_day20(self):
        entry_no_day20 = _q5_entry(day0_price=100.0, day5=(CAL[5], 110.0))
        self.assertEqual(cmp.q5_returns_cumulative([entry_no_day20]), [])


class DuplicateTickerSampleTests(unittest.TestCase):
    """5: 同一ticker・複数Day0が別サンプルとして数えられる。"""

    def test_two_day0_events_for_same_ticker_both_counted(self):
        e1 = _q5_entry(day0_price=100.0, day20=(CAL[20], 120.0))
        e2 = dict(e1)
        e2["day0_date"] = CAL[30]
        e2["day20_date"], e2["day20_price"] = CAL[50], 90.0
        returns = sorted(cmp.q5_returns_cumulative([e1, e2]))
        self.assertEqual(len(returns), 2)
        self.assertAlmostEqual(returns[0], -10.0)
        self.assertAlmostEqual(returns[1], 20.0)


class BuildComparisonReportTests(unittest.TestCase):
    def test_diff_sign_is_mine_minus_q5(self):
        anchor = CAL[100]
        trades = [_trade(CAL[95], 30.0)]
        entries = [_q5_entry(day0_price=100.0, day20=(CAL[95], 110.0))]  # +10%
        report = cmp.build_comparison_report(trades, entries, CAL, anchor)
        row = report["recent_window"]["20"]
        self.assertAlmostEqual(row["mine"]["mean"], 30.0)
        self.assertAlmostEqual(row["q5"]["mean"], 10.0)
        self.assertAlmostEqual(row["diff"], 20.0)  # 私が勝っていればプラス

    def test_diff_is_none_when_either_side_empty(self):
        anchor = CAL[100]
        report = cmp.build_comparison_report([], [], CAL, anchor)
        self.assertIsNone(report["recent_window"]["20"]["diff"])
        self.assertEqual(report["recent_window"]["20"]["mine"]["n"], 0)

    def test_recent_window_has_5_20_60_and_cumulative_rows(self):
        report = cmp.build_comparison_report([], [], CAL, CAL[100])
        self.assertEqual(set(report["recent_window"].keys()), {"5", "20", "60", "cumulative"})

    def test_all_time_by_horizon_independent_of_recency_window(self):
        """8: 全期間版(all_time_by_horizon)が直近N営業日フィルタの有無に
        関わらず正しく独立して計算される。"""
        old_entry = _q5_entry(day0_price=100.0, day5=(CAL[5], 150.0))  # 遠い過去
        report = cmp.build_comparison_report([], [old_entry], CAL, CAL[199])
        # recent_windowの5営業日行では(直近5営業日に確定していないため)対象外
        self.assertEqual(report["recent_window"]["5"]["q5"]["n"], 0)
        # all_time_by_horizonでは日付フィルタが無いため含まれる
        self.assertEqual(report["all_time_by_horizon"]["5"]["n"], 1)
        self.assertAlmostEqual(report["all_time_by_horizon"]["5"]["mean"], 50.0)

    def test_handles_empty_trading_calendar_without_crashing(self):
        trades = [_trade(CAL[5], 10.0)]
        entries = [_q5_entry(day0_price=100.0, day20=(CAL[20], 120.0))]
        report = cmp.build_comparison_report(trades, entries, [], None)
        self.assertEqual(report["recent_window"]["20"]["mine"]["n"], 0)
        self.assertEqual(report["recent_window"]["20"]["q5"]["n"], 0)
        # 累計・全期間版はカレンダー非依存のため影響されない
        self.assertEqual(report["recent_window"]["cumulative"]["mine"]["n"], 1)
        self.assertEqual(report["all_time_by_horizon"]["20"]["n"], 1)

    def test_unresolved_checkpoint_excluded_from_both_recent_and_all_time(self):
        anchor = CAL[100]
        entries = [_q5_entry(day0_price=100.0)]  # day5/20/60すべて未確定
        report = cmp.build_comparison_report([], entries, CAL, anchor)
        for horizon in ("5", "20", "60"):
            self.assertEqual(report["recent_window"][horizon]["q5"]["n"], 0)
            self.assertEqual(report["all_time_by_horizon"][horizon]["n"], 0)
        self.assertEqual(report["recent_window"]["cumulative"]["q5"]["n"], 0)


class EnrichEntriesTests(unittest.TestCase):
    def test_enrich_adds_return_fields_without_mutating_input(self):
        entry = _q5_entry(day0_price=100.0, day20=(CAL[20], 130.0))
        original = dict(entry)
        enriched = cmp.enrich_entries_with_returns([entry])
        self.assertEqual(entry, original)  # 引数は変更されない
        self.assertAlmostEqual(enriched[0]["day20_return_pct"], 30.0)
        self.assertIsNone(enriched[0]["day5_return_pct"])
        self.assertIsNone(enriched[0]["day60_return_pct"])


if __name__ == "__main__":
    unittest.main()
