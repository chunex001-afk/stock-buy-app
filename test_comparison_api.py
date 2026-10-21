# -*- coding: utf-8 -*-
"""GET /api/compare/q5universe(design 2026-10-21)の結合テスト。読み取り
専用APIで、trade_history・q5universe:state・positions等、既存のどのキー
にも書き込まない。Twelve Data・Upstashへは一切アクセスしない(store側の
該当関数のみmockで差し替える)。
"""
import json
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


CAL = _business_days(150)


class CompareApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._q5_state = None
        self._calendar = list(CAL)
        self._ticker_prices = {}  # 保有中ポジションの現在価格(テスト専用、ticker->price)

        def fake_get_ticker_record(ticker):
            price = self._ticker_prices.get(ticker)
            return {"price": price} if price is not None else None

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=lambda: self._portfolio_state),
            mock.patch.object(app.store, "get_q5universe_state", side_effect=lambda: self._q5_state),
            mock.patch.object(app.store, "get_ticker_record", side_effect=fake_get_ticker_record),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self):
        resp = self.client.get("/api/compare/q5universe")
        return resp, resp.get_json()


class BasicResponseShapeTests(CompareApiTestCase):
    def test_9_response_has_expected_shape(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[95], "exit_price": 110.0, "return_pct": 10.0},
        ]}
        self._q5_state = {"entries": [
            {"ticker": "BBB", "day0_date": CAL[0], "day0_price": 100.0,
             "day5_date": None, "day5_price": None,
             "day20_date": CAL[20], "day20_price": 120.0,
             "day60_date": None, "day60_price": None},
        ]}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["recent_window"].keys()), {"5", "20", "60", "cumulative"})
        self.assertEqual(set(data["all_time_by_horizon"].keys()), {"5", "20", "60"})
        self.assertEqual(len(data["q5_entries"]), 1)
        self.assertIn("day20_return_pct", data["q5_entries"][0])
        self.assertAlmostEqual(data["q5_entries"][0]["day20_return_pct"], 20.0)

    def test_cumulative_row_reflects_my_trade_and_q5_day20(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[10], "exit_price": 130.0, "return_pct": 30.0},
        ]}
        self._q5_state = {"entries": [
            {"ticker": "BBB", "day0_date": CAL[0], "day0_price": 100.0,
             "day5_date": None, "day5_price": None,
             "day20_date": CAL[20], "day20_price": 110.0,
             "day60_date": None, "day60_price": None},
        ]}
        resp, data = self._get()
        cumulative = data["recent_window"]["cumulative"]
        self.assertEqual(cumulative["mine"]["n"], 1)
        self.assertAlmostEqual(cumulative["mine"]["mean"], 30.0)
        self.assertEqual(cumulative["q5"]["n"], 1)
        self.assertAlmostEqual(cumulative["q5"]["mean"], 10.0)
        self.assertAlmostEqual(cumulative["diff"], 20.0)


class EmptyStateTests(CompareApiTestCase):
    """10: trade_history/q5universe:stateが未初期化(None)でもエラーに
    ならず0件として返る。"""

    def test_10_both_states_uninitialized_returns_zero_counts_not_error(self):
        self._portfolio_state = None
        self._q5_state = None
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        for horizon in ("5", "20", "60", "cumulative"):
            self.assertEqual(data["recent_window"][horizon]["mine"]["n"], 0)
            self.assertEqual(data["recent_window"][horizon]["q5"]["n"], 0)
        self.assertEqual(data["q5_entries"], [])

    def test_empty_trading_calendar_does_not_error(self):
        self._calendar = []
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": "2026-01-01", "entry_price": 100.0,
             "exit_date": "2026-01-10", "exit_price": 90.0, "return_pct": -10.0},
        ]}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        # 直近N営業日版はカレンダー無しのため0件、累計はカレンダー非依存なので反映される
        self.assertEqual(data["recent_window"]["20"]["mine"]["n"], 0)
        self.assertEqual(data["recent_window"]["cumulative"]["mine"]["n"], 1)


class ReadOnlyTests(CompareApiTestCase):
    """この新規APIが既存のどのキーにも書き込まないことの確認。"""

    def test_does_not_write_to_portfolio_or_q5universe_state(self):
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[5], "exit_price": 105.0, "return_pct": 5.0},
        ]}
        self._q5_state = {"entries": []}
        with mock.patch.object(app.store, "set_portfolio_state") as set_portfolio, \
             mock.patch.object(app.store, "set_q5universe_state") as set_q5:
            resp, data = self._get()
            self.assertEqual(resp.status_code, 200)
            set_portfolio.assert_not_called()
            set_q5.assert_not_called()


class MomentumResponseTests(CompareApiTestCase):
    """Q5直近勢い(design 2026-10-21)のAPIレベルテスト。"""

    def test_momentum_key_present_in_response_with_expected_shape(self):
        self._q5_state = {
            "entries": [
                {"ticker": "AMD", "day0_date": CAL[98], "day0_price": 100.0,
                 "day5_date": None, "day5_price": None,
                 "day20_date": None, "day20_price": None,
                 "day60_date": None, "day60_price": None},
            ],
            "latest_price": {"AMD": 110.0},
            "latest_price_date": CAL[100],
        }
        self._calendar = CAL[:101]  # 今日=CAL[100]
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["momentum"].keys()), {"5", "20", "60"})
        self.assertEqual(data["latest_price_date"], CAL[100])

    def test_momentum_includes_unresolved_day0(self):
        """未経過(day5/20/60が全てNone)のDay0もmomentumには含まれることを
        APIレベルで確認する。"""
        self._q5_state = {
            "entries": [
                {"ticker": "AMD", "day0_date": CAL[99], "day0_price": 100.0,
                 "day5_date": None, "day5_price": None,
                 "day20_date": None, "day20_price": None,
                 "day60_date": None, "day60_price": None},
            ],
            "latest_price": {"AMD": 105.0},
            "latest_price_date": CAL[100],
        }
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["momentum"]["5"]["n"], 1)
        self.assertAlmostEqual(data["momentum"]["5"]["mean"], 5.0)

    def test_momentum_empty_when_latest_price_key_missing(self):
        """12: latest_priceキーが存在しない古いq5universe:state(本機能
        追加前に保存されたもの)でもエラーにならず0件になる。"""
        self._q5_state = {
            "entries": [
                {"ticker": "AMD", "day0_date": CAL[99], "day0_price": 100.0,
                 "day5_date": None, "day5_price": None,
                 "day20_date": None, "day20_price": None,
                 "day60_date": None, "day60_price": None},
            ],
            # latest_price/latest_price_dateキー自体が存在しない
        }
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        for horizon in ("5", "20", "60"):
            self.assertEqual(data["momentum"][horizon]["n"], 0)
        self.assertIsNone(data["latest_price_date"])

    def test_momentum_with_stale_latest_price_date_still_returns_data(self):
        """14: latest_price_dateが古い(ジョブが数日止まっている)場合でも、
        エラーにならずその古い日付のまま結果を返す。"""
        self._q5_state = {
            "entries": [
                {"ticker": "AMD", "day0_date": CAL[95], "day0_price": 100.0,
                 "day5_date": None, "day5_price": None,
                 "day20_date": None, "day20_price": None,
                 "day60_date": None, "day60_price": None},
            ],
            "latest_price": {"AMD": 120.0},
            "latest_price_date": CAL[96],  # last_processed_dateより古い想定
        }
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["latest_price_date"], CAL[96])
        self.assertEqual(data["momentum"]["60"]["n"], 1)

    def test_existing_recent_window_and_all_time_unaffected_by_momentum_addition(self):
        """既存のQ5確定成績が変わらないことのAPIレベル確認。"""
        self._portfolio_state = {"trade_history": [
            {"ticker": "AAA", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[95], "exit_price": 110.0, "return_pct": 10.0},
        ]}
        self._q5_state = {
            "entries": [
                {"ticker": "BBB", "day0_date": CAL[0], "day0_price": 100.0,
                 "day5_date": None, "day5_price": None,
                 "day20_date": CAL[20], "day20_price": 120.0,
                 "day60_date": None, "day60_price": None},
            ],
            "latest_price": {"BBB": 999.0},  # momentum側だけに影響する想定の値
            "latest_price_date": CAL[100],
        }
        resp, data = self._get()
        self.assertEqual(set(data["recent_window"].keys()), {"5", "20", "60", "cumulative"})
        self.assertAlmostEqual(data["q5_entries"][0]["day20_return_pct"], 20.0)
        # momentumキーの追加によって既存キーの値がぶれていないこと
        self.assertIn("momentum", data)


class MyConfirmedBlockResponseTests(CompareApiTestCase):
    """①【保有確定】(design 2026-10-21)のAPIレベルテスト。
    私側は実際の購入日起算(Q5側はQ5 Day0起算、既存のrecent_windowを再利用)。"""

    def test_my_confirmed_key_present_with_expected_shape(self):
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._q5_state = {"entries": []}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["my_confirmed"].keys()), {"5", "20", "60"})

    def test_sold_trade_past_threshold_uses_real_exit_return(self):
        self._portfolio_state = {"positions": {}, "trade_history": [
            {"ticker": "AMD", "entry_date": CAL[0], "entry_price": 100.0,
             "exit_date": CAL[25], "exit_price": 130.0, "return_pct": 30.0},
        ]}
        self._q5_state = {"entries": []}
        self._calendar = CAL[:30]
        resp, data = self._get()
        self.assertEqual(data["my_confirmed"]["20"]["n"], 1)
        self.assertAlmostEqual(data["my_confirmed"]["20"]["mean"], 30.0)

    def test_open_position_past_threshold_uses_current_price(self):
        self._portfolio_state = {"positions": {"AMD": {"entry_date": CAL[0], "entry_price": 100.0}},
                                  "trade_history": []}
        self._q5_state = {"entries": []}
        self._ticker_prices = {"AMD": 125.0}
        self._calendar = CAL[:30]
        resp, data = self._get()
        self.assertEqual(data["my_confirmed"]["20"]["n"], 1)
        self.assertAlmostEqual(data["my_confirmed"]["20"]["mean"], 25.0)

    def test_my_confirmed_does_not_require_q5_entries(self):
        """Q5側はrecent_window(既存)をそのまま再利用するだけで、
        my_confirmed自体はQ5側のデータが空でも正常に計算される。"""
        self._portfolio_state = {"positions": {"AMD": {"entry_date": CAL[0], "entry_price": 100.0}},
                                  "trade_history": []}
        self._q5_state = None
        self._ticker_prices = {"AMD": 110.0}
        self._calendar = CAL[:30]
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["my_confirmed"]["20"]["n"], 1)


class MyCurrentPriceBlockResponseTests(CompareApiTestCase):
    """②【現在価格】(design 2026-10-21)のAPIレベルテスト。
    私側は実際の購入日起算、保有中(open)と売却済み(sold)を分けて返す。"""

    def test_my_current_price_key_present_with_open_and_sold(self):
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._q5_state = {"entries": []}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        for h in ("5", "20", "60"):
            self.assertEqual(set(data["my_current_price"][h].keys()), {"open", "sold"})

    def test_open_position_recently_purchased_uses_mark_to_market(self):
        self._portfolio_state = {"positions": {"AMD": {"entry_date": CAL[98], "entry_price": 100.0}},
                                  "trade_history": []}
        self._q5_state = {"entries": []}
        self._ticker_prices = {"AMD": 115.0}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["my_current_price"]["5"]["open"]["n"], 1)
        self.assertAlmostEqual(data["my_current_price"]["5"]["open"]["mean"], 15.0)
        self.assertEqual(data["my_current_price"]["5"]["sold"]["n"], 0)

    def test_recently_purchased_and_sold_trade_reported_separately(self):
        self._portfolio_state = {"positions": {}, "trade_history": [
            {"ticker": "AMD", "entry_date": CAL[98], "entry_price": 100.0,
             "exit_date": CAL[99], "exit_price": 108.0, "return_pct": 8.0},
        ]}
        self._q5_state = {"entries": []}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["my_current_price"]["5"]["open"]["n"], 0)
        self.assertEqual(data["my_current_price"]["5"]["sold"]["n"], 1)
        self.assertAlmostEqual(data["my_current_price"]["5"]["sold"]["mean"], 8.0)


class NewBlocksExistingKeysUnaffectedTests(CompareApiTestCase):
    """既存のQ5直近勢い・確定成績・既存の詳細テーブル用キーが、新しい
    2ブロック追加によって変化しないことのAPIレベル確認(12)。"""

    def test_existing_keys_unchanged_when_new_blocks_present(self):
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[98], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": CAL[20], "day20_price": 110.0,
                         "day60_date": None, "day60_price": None}],
            "latest_price": {"AMD": 112.0}, "latest_price_date": CAL[100],
        }
        self._portfolio_state = {"positions": {"AMD": {"entry_date": CAL[99], "entry_price": 90.0}},
                                  "trade_history": []}
        self._ticker_prices = {"AMD": 99.0}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertAlmostEqual(data["momentum"]["5"]["mean"], 12.0)  # (112/100-1)*100、新ブロックと無関係
        self.assertEqual(set(data["recent_window"].keys()), {"5", "20", "60", "cumulative"})
        self.assertIn("my_confirmed", data)
        self.assertIn("my_current_price", data)


class ActualPerformanceBlockResponseTests(CompareApiTestCase):
    """③【実運用成績】(design 2026-10-21最終確定仕様)のAPIレベルテスト。
    Q5側はQ5 Day0起算で20営業日運用後に固定、私側は実際の購入日起算。"""

    def test_actual_performance_key_present_with_5_20_60_120_180_keys(self):
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._q5_state = {"entries": []}
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["actual_performance"].keys()), {"5", "20", "60", "120", "180"})
        for window_key in data["actual_performance"]:
            row = data["actual_performance"][window_key]
            self.assertEqual(set(row.keys()), {"q5", "mine", "mean_diff", "median_diff", "win_rate_diff"})
            self.assertEqual(set(row["q5"].keys()),
                              {"n", "mean", "median", "win_rate", "total", "unconfirmed"})
            self.assertEqual(set(row["mine"].keys()),
                              {"n", "mean", "median", "win_rate", "total", "excluded"})

    def test_q5_entry_overdue_without_day20_price_excluded_not_current_price(self):
        """2026-10-21追加修正: 20営業日以上経過してday20_priceが未確定の
        Q5 Day0は、latest_priceがどれだけ動いても集計(n/mean)に入らず、
        unconfirmedとしてカウントされる(現在価格を絶対に代用しない)。"""
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[0], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": None, "day20_price": None,  # 未確定のまま
                         "day60_date": None, "day60_price": None}],
            "latest_price": {"AMD": 200.0},  # +100%、これが使われてはいけない
            "latest_price_date": CAL[40],
        }
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._calendar = CAL[:41]
        resp, data = self._get()
        row = data["actual_performance"]["60"]["q5"]
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["n"], 0)
        self.assertEqual(row["unconfirmed"], 1)
        self.assertIsNone(row["mean"])

    def test_q5_side_freezes_at_day20_price_not_current_price(self):
        """2・3: 20営業日後価格が確定済みのQ5 Day0は、latest_priceが
        どれだけ動いても20営業日後価格のリターンで固定される。"""
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[0], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": CAL[20], "day20_price": 130.0,
                         "day60_date": None, "day60_price": None}],
            "latest_price": {"AMD": 999.0},
            "latest_price_date": CAL[100],
        }
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["actual_performance"]["180"]["q5"]["n"], 1)
        self.assertAlmostEqual(data["actual_performance"]["180"]["q5"]["mean"], 30.0)

    def test_q5_side_under_20_days_uses_current_price(self):
        """1: 20営業日未満のQ5 Day0はlatest_priceでmark-to-marketする。"""
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[90], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": None, "day20_price": None,
                         "day60_date": None, "day60_price": None}],
            "latest_price": {"AMD": 108.0},
            "latest_price_date": CAL[100],
        }
        self._portfolio_state = {"positions": {}, "trade_history": []}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["actual_performance"]["20"]["q5"]["n"], 1)
        self.assertAlmostEqual(data["actual_performance"]["20"]["q5"]["mean"], 8.0)

    def test_my_side_uses_entry_date_for_both_sold_and_open(self):
        """6・5・7: 私側は売却済み(実売却価格)・保有中(現在価格)を
        混在させ、購入日(entry_date)でウィンドウ判定する。"""
        self._portfolio_state = {
            "positions": {"AMD": {"entry_date": CAL[90], "entry_price": 100.0}},
            "trade_history": [
                {"ticker": "NVDA", "entry_date": CAL[90], "entry_price": 50.0,
                 "exit_date": CAL[95], "exit_price": 70.0, "return_pct": 40.0},
            ],
        }
        self._q5_state = {"entries": []}
        self._ticker_prices = {"AMD": 108.0}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertEqual(data["actual_performance"]["20"]["mine"]["n"], 2)
        self.assertAlmostEqual(data["actual_performance"]["20"]["mine"]["mean"], 24.0)

    def test_mean_diff_is_mine_minus_q5(self):
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[0], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": CAL[20], "day20_price": 110.0,
                         "day60_date": None, "day60_price": None}],
            "latest_price": {},
        }
        self._portfolio_state = {"positions": {}, "trade_history": [
            {"ticker": "NVDA", "entry_date": CAL[0], "entry_price": 50.0,
             "exit_date": CAL[20], "exit_price": 75.0, "return_pct": 50.0},
        ]}
        self._calendar = CAL[:30]
        resp, data = self._get()
        self.assertAlmostEqual(data["actual_performance"]["60"]["mean_diff"], 40.0)

    def test_rotation_universe_outside_ticker_fully_excluded_from_mine(self):
        """【1】Q5 48ユニバース(quintile_logic.ROTATION_UNIVERSE)外の
        tickerは私側の比較対象から完全に除外される(total/nいずれにも
        カウントされない)。IONQはROTATION_UNIVERSE外。"""
        self._portfolio_state = {
            "positions": {"IONQ": {"entry_date": CAL[90], "entry_price": 100.0}},
            "trade_history": [],
        }
        self._q5_state = {"entries": []}
        self._ticker_prices = {"IONQ": 150.0}
        self._calendar = CAL[:101]
        resp, data = self._get()
        row = data["actual_performance"]["20"]["mine"]
        self.assertEqual(row["n"], 0)
        self.assertEqual(row["total"], 0)
        self.assertEqual(row["excluded"], 0)

    def test_mine_sold_beyond_20_days_is_excluded_not_unconfirmed(self):
        """【2】私側・売却済みで保有20営業日を超えている場合、実データ
        (return_pct)は存在するが「比較対象外」としてexcludedにカウント
        される(未確定=データ不存在とは異なる概念)。"""
        self._portfolio_state = {
            "positions": {},
            "trade_history": [
                {"ticker": "AMD", "entry_date": CAL[0], "entry_price": 100.0,
                 "exit_date": CAL[40], "exit_price": 999.0, "return_pct": 899.0},
            ],
        }
        self._q5_state = {"entries": []}
        self._calendar = CAL[:41]
        resp, data = self._get()
        row = data["actual_performance"]["60"]["mine"]
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["n"], 0)
        self.assertEqual(row["excluded"], 1)
        self.assertIsNone(row["mean"])

    def test_mine_open_20_days_or_more_excluded_current_price_never_used(self):
        """【2】私側・保有中で20営業日以上経過している場合、現在価格が
        どれだけ高くてもexcludedとして集計から除外される。"""
        self._portfolio_state = {
            "positions": {"AMD": {"entry_date": CAL[0], "entry_price": 100.0}},
            "trade_history": [],
        }
        self._q5_state = {"entries": []}
        self._ticker_prices = {"AMD": 999.0}  # +899%、これが使われてはいけない
        self._calendar = CAL[:41]
        resp, data = self._get()
        row = data["actual_performance"]["60"]["mine"]
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["n"], 0)
        self.assertEqual(row["excluded"], 1)
        self.assertIsNone(row["mean"])

    def test_existing_keys_unchanged_when_actual_performance_present(self):
        """9: 既存のmomentum/recent_window/my_confirmed/my_current_priceが
        ③実運用成績の追加によって変化しないことのAPIレベル確認。"""
        self._q5_state = {
            "entries": [{"ticker": "AMD", "day0_date": CAL[98], "day0_price": 100.0,
                         "day5_date": None, "day5_price": None,
                         "day20_date": CAL[20], "day20_price": 110.0,
                         "day60_date": None, "day60_price": None}],
            "latest_price": {"AMD": 112.0}, "latest_price_date": CAL[100],
        }
        self._portfolio_state = {"positions": {"AMD": {"entry_date": CAL[99], "entry_price": 90.0}},
                                  "trade_history": []}
        self._ticker_prices = {"AMD": 99.0}
        self._calendar = CAL[:101]
        resp, data = self._get()
        self.assertAlmostEqual(data["momentum"]["5"]["mean"], 12.0)
        self.assertEqual(set(data["recent_window"].keys()), {"5", "20", "60", "cumulative"})
        self.assertIn("my_confirmed", data)
        self.assertIn("my_current_price", data)
        self.assertIn("actual_performance", data)

    def test_empty_states_return_zero_counts_not_error(self):
        """8: 資金/NAV等を一切使わないため、空データでもn=0として正常に
        返る(エラーにならない)。"""
        self._portfolio_state = None
        self._q5_state = None
        resp, data = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        for window_key in ("5", "20", "60", "120", "180"):
            self.assertEqual(data["actual_performance"][window_key]["q5"]["n"], 0)
            self.assertEqual(data["actual_performance"][window_key]["mine"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
