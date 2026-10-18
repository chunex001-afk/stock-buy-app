# -*- coding: utf-8 -*-
"""PF一覧の保有期間表示(design 2026-10-18)のテスト。

表示専用機能であり、既存の20営業日SELL判定ロジック(_compute_daily_actions)
には一切手を加えていない。ここでは:
1. app._build_portfolio_view()のdays_held/hold_daysが、既存の
   _compute_daily_actionsのSELL判定と完全に同じ_trading_day_indexベースの
   計算で求められていること(整合性)
2. PROFIT後もentry_dateが維持され、保有期間が0に戻らないこと
3. 既存のSELL判定結果が変わっていないこと(回帰確認)
4. 営業日のみをカウントし、土日は数えないこと
を検証する。Twelve Data・Redisへは一切アクセスしない。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import app


def _business_days(n, start="2026-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(60)


class PortfolioViewHoldingPeriodTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        self.portfolio_state = None
        p1 = mock.patch.object(app.store, "get_portfolio_state", side_effect=lambda: self.portfolio_state)
        p2 = mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self.records.get(t))
        p3 = mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: CAL)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def _set_holding(self, entry_idx, price=120.0):
        self.portfolio_state = {
            "positions": {"AAA": {"entry_date": CAL[entry_idx], "entry_price": 100.0,
                                   "profit_taken": False, "profit_taken_date": None}},
            "last_updated": CAL[-1],
        }
        self.records["AAA"] = {"price": price}

    def test_hold_days_field_equals_20(self):
        view = app._build_portfolio_view()
        self.assertEqual(view["hold_days"], 20)

    def test_1_buy_same_day_is_0(self):
        # 「今日」= CAL[-1]固定のため、エントリーも同じ日にする
        self._set_holding(entry_idx=len(CAL) - 1)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 0)

    def test_2_5_business_days(self):
        self._set_holding(entry_idx=len(CAL) - 1 - 5)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 5)

    def test_3_14_business_days(self):
        self._set_holding(entry_idx=len(CAL) - 1 - 14)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 14)

    def test_4_15_business_days(self):
        self._set_holding(entry_idx=len(CAL) - 1 - 15)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 15)

    def test_5_19_business_days(self):
        self._set_holding(entry_idx=len(CAL) - 1 - 19)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 19)

    def test_6_20_business_days(self):
        self._set_holding(entry_idx=len(CAL) - 1 - 20)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 20)

    def test_7_weekends_not_counted(self):
        # CAL自体が平日のみのリストなので、暦日ベースではなく_trading_day_index
        # (カレンダー上のインデックス差)で数えていることを、実際の暦日との
        # ズレで確認する(entry_idxから5営業日 = 暦日では土日を挟んで7日以上)。
        entry_idx = len(CAL) - 1 - 5
        self._set_holding(entry_idx=entry_idx)
        calendar_days_elapsed = (date.fromisoformat(CAL[-1]) - date.fromisoformat(CAL[entry_idx])).days
        self.assertGreater(calendar_days_elapsed, 5)  # 暦日では5日を超えている(土日を含むため)
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"][0]["days_held"], 5)  # 営業日ベースでは正しく5

    def test_8_profit_taken_does_not_reset_entry_date_or_days_held(self):
        entry_idx = len(CAL) - 1 - 8
        self._set_holding(entry_idx=entry_idx, price=150.0)  # +50%、PROFIT対象
        self.portfolio_state["positions"]["AAA"]["profit_taken"] = True
        self.portfolio_state["positions"]["AAA"]["profit_taken_date"] = CAL[entry_idx + 2]
        self.portfolio_state["positions"]["AAA"]["shares"] = 6.666666
        view = app._build_portfolio_view()
        h = view["holdings"][0]
        self.assertEqual(h["entry_date"], CAL[entry_idx])  # entry_dateは変化しない
        self.assertEqual(h["days_held"], 8)  # PROFIT後も0に戻らず継続カウント

    def test_9_existing_sell_judgement_unchanged_at_20_days(self):
        """今回の変更前後でSELL判定結果が変わらないことの回帰確認。
        _compute_daily_actionsは今回一切変更していないため、20営業日経過時に
        従来通りSELL判定されることをそのまま再確認する。"""
        entry_idx = len(CAL) - 1 - 20
        positions = {"AAA": {"entry_date": CAL[entry_idx], "entry_price": 100.0,
                              "profit_taken": False, "profit_taken_date": None}}
        self.records["AAA"] = {"price": 110.0}
        state = {"positions": positions, "last_updated": CAL[-1]}
        result = app._compute_daily_actions([], state, None, CAL[-1], CAL)
        self.assertEqual([s["ticker"] for s in result["sell"]], ["AAA"])
        self.assertEqual(result["sell"][0]["days_held"], 20)

    def test_9b_existing_sell_judgement_unchanged_at_19_days(self):
        entry_idx = len(CAL) - 1 - 19
        positions = {"AAA": {"entry_date": CAL[entry_idx], "entry_price": 100.0,
                              "profit_taken": False, "profit_taken_date": None}}
        self.records["AAA"] = {"price": 110.0}
        state = {"positions": positions, "last_updated": CAL[-1]}
        result = app._compute_daily_actions([], state, None, CAL[-1], CAL)
        self.assertEqual(result["sell"], [])  # 19日ではまだSELLしない(従来通り)


if __name__ == "__main__":
    unittest.main()
