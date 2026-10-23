# -*- coding: utf-8 -*-
"""AI市場下落状況パネル(design 2026-10-23、2026-10-23に「毎日41銘柄を同日
取得する」方式へ再設計、同日2026-10-23に冪等判定を実取引日(trading_date)
ベースへ修正)のテスト。

refresh._update_ai_market_decline_state(集計ロジック)とapp._build_ai_market_decline_view
/_ai_market_decline_bucket/_ai_market_decline_state_label(表示専用ビュー)を
対象とする。Twelve Data・Redisへは一切アクセスしない(他のテストと同じ方針、
refresh.store/refresh._td_fetch_oneをmockで差し替える)。

このパネルはQ1〜Q5判定・Q5 Day0判定・BUY/SELL判定・Risk-off判定・投資比率・
Q5ランキングには一切接続していないことを確認する(完全に独立した表示専用
機能であることの検証)。

【最重要1】41銘柄のうち1つでも取得に失敗した日は、古い価格と新しい価格が
混在した「本日分」を絶対に作らないこと(過去検証が41銘柄を同一営業日の
終値で評価していることとの整合性を守るため)。この安全性を
PartialFailureSafetyTestsで重点的に検証する。

【最重要2】冪等判定・保存する"date"・「時点」表示が、壁時計日付(date_key)
ではなく実取引日(trading_date、main()がtd_result["trading_date"]から渡す
値)を基準にしていること。これにより、UTC22:00の実行とUTC1:00の実行で
実際の取引日が進んでいない場合、41銘柄の再取得が一切発生しないことを
TradingDateIdempotencyTestsで重点的に検証する。
"""
import unittest
from unittest import mock

import app
import refresh


def _series(ath, current, n=300):
    """ATH(最高値)とcurrent(最新終値)だけを指定し、他は単調な系列で埋めた
    (dates, closes, volumes)を返す(_update_ai_market_decline_stateのテスト用)。"""
    dates = [f"2024-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)]
    closes = [ath * 0.9] * (n - 2) + [ath, current]
    volumes = [1000] * n
    return (dates, closes, volumes)


class UpdateAiMarketDeclineStateTests(unittest.TestCase):
    """refresh._update_ai_market_decline_stateの集計ロジックを検証する
    (毎日41銘柄すべてを逐次取得する新方式)。date_key(壁時計、予算カウンタ用)
    とtrading_date(実取引日、冪等判定・保存用)は別々の引数として渡す。"""

    def setUp(self):
        self.fake_store = mock.Mock()
        self.saved_state = None
        self.fake_store.get_ai_market_decline_state.side_effect = lambda: self.saved_state
        self.fake_store.set_ai_market_decline_state.side_effect = self._save
        p1 = mock.patch.object(refresh, "store", self.fake_store)
        p1.start()
        self.addCleanup(p1.stop)

        self.budget_left = 700
        p2 = mock.patch.object(refresh, "remaining_td_budget", side_effect=lambda: (self.budget_left, 0))
        p2.start()
        self.addCleanup(p2.stop)

    def _save(self, data):
        self.saved_state = data

    def _patch_fetch(self, price_by_ticker):
        """price_by_ticker: {ticker: (ath, current)} または {ticker: None(取得失敗)}。
        未指定のtickerは(100.0, 95.0)をデフォルトとする。"""
        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            spec = price_by_ticker.get(ticker, (100.0, 95.0))
            if spec is None:
                return None, False, "NO_DATA", "test", None
            ath, current = spec
            return _series(ath, current), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

    def test_fetches_all_41_tickers_in_a_single_call(self):
        calls = []

        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            calls.append(ticker)
            self.assertEqual(outputsize, refresh.AI_MARKET_DECLINE_ATH_OUTPUTSIZE)
            return _series(100.0, 95.0), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

        refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(sorted(calls), sorted(refresh.AI_UNIVERSE))
        self.assertEqual(len(calls), 41)

    def test_counts_tickers_down_30_percent_or_more_matching_user_example(self):
        spec = {}
        for i, t in enumerate(refresh.AI_UNIVERSE):
            spec[t] = (100.0, 69.0) if i < 29 else (100.0, 95.0)
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state["n_down"], 29)
        self.assertEqual(state["n_pop"], 41)
        self.assertAlmostEqual(state["pct_down"], 70.7, places=1)
        self.assertEqual(state["date"], "2026-10-22")  # trading_date(実取引日)で保存される

    def test_boundary_exactly_30_percent_counts_as_down(self):
        spec = {t: (100.0, 70.0) for t in refresh.AI_UNIVERSE}  # ちょうど-30.0%
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state["n_down"], 41)

    def test_boundary_just_above_30_percent_does_not_count(self):
        spec = {t: (100.0, 70.01) for t in refresh.AI_UNIVERSE}  # -29.99%
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state["n_down"], 0)

    def test_ath_accumulates_as_running_max_across_days(self):
        # Day1はATH=100。Day2に新高値120を付けた後90まで下落(-25%、閾値未満)
        # しても、蓄積されたATHは120のままで下方修正されないことを確認する。
        ticker = refresh.AI_UNIVERSE[0]
        self._patch_fetch({ticker: (100.0, 100.0)})
        refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertAlmostEqual(self.saved_state["tickers"][ticker]["ath"], 100.0)

        self._patch_fetch({ticker: (120.0, 90.0)})  # 新高値120、現在90(-25%)
        refresh._update_ai_market_decline_state("fake_key", "2026-10-24", "2026-10-23")
        self.assertAlmostEqual(self.saved_state["tickers"][ticker]["ath"], 120.0)
        self.assertAlmostEqual(self.saved_state["tickers"][ticker]["current_price"], 90.0)


class TradingDateIdempotencyTests(unittest.TestCase):
    """【最重要2】冪等判定・保存・表示が実取引日(trading_date)を基準にして
    おり、壁時計日付(date_key)の違いには影響されないことを検証する。
    UTC22:00とUTC1:00のように、date_keyは日付が変わってもtrading_dateが
    変わらない限り、41銘柄の再取得が一切発生しないことがここでの核心。"""

    def setUp(self):
        self.fake_store = mock.Mock()
        self.saved_state = None
        self.fake_store.get_ai_market_decline_state.side_effect = lambda: self.saved_state
        self.fake_store.set_ai_market_decline_state.side_effect = self._save
        p1 = mock.patch.object(refresh, "store", self.fake_store)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        p2.start()
        self.addCleanup(p2.stop)

    def _save(self, data):
        self.saved_state = data

    def _patch_fetch(self, price_by_ticker=None):
        price_by_ticker = price_by_ticker or {}

        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            spec = price_by_ticker.get(ticker, (100.0, 95.0))
            if spec is None:
                return None, False, "NO_DATA", "test", None
            ath, current = spec
            return _series(ath, current), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

    def test_same_trading_date_with_different_wall_clock_date_does_not_refetch(self):
        # UTC22:00(壁時計日付=2026-10-22)で実取引日2026-10-22のデータを取得・
        # 保存。UTC1:00相当(壁時計日付が2026-10-23に進む)でも、実取引日が
        # まだ2026-10-22のままなら、41銘柄を1件も再取得しないことを確認する。
        self._patch_fetch({t: (100.0, 95.0) for t in refresh.AI_UNIVERSE})
        state1 = refresh._update_ai_market_decline_state("fake_key", "2026-10-22", "2026-10-22")
        self.assertEqual(state1["date"], "2026-10-22")
        self.fake_store.set_ai_market_decline_state.assert_called_once()

        # 以降、_td_fetch_oneが呼ばれたら即座に失敗させる(=再取得してはいけない)。
        fetch_mock = mock.patch.object(refresh, "_td_fetch_one", side_effect=AssertionError("41銘柄を再取得してはいけない"))
        fetch_mock.start()
        self.addCleanup(fetch_mock.stop)

        # 壁時計日付は2026-10-23に進んだが、実取引日はまだ2026-10-22のまま。
        state2 = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state2, state1)
        self.fake_store.set_ai_market_decline_state.assert_called_once()  # 2回目は呼ばれていない

    def test_new_trading_date_triggers_fresh_fetch_even_with_same_wall_clock_date(self):
        # 逆に、実取引日が進んでいれば(壁時計日付が同じでも)、きちんと
        # 再取得が行われることを確認する(取引日が進んだのに固まったままに
        # ならないことの確認)。
        self._patch_fetch({t: (100.0, 95.0) for t in refresh.AI_UNIVERSE})
        state1 = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state1["date"], "2026-10-22")

        self._patch_fetch({t: (100.0, 69.0) for t in refresh.AI_UNIVERSE})  # 新しい取引日は全銘柄-31%
        state2 = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-23")
        self.assertEqual(state2["date"], "2026-10-23")
        self.assertEqual(state2["n_down"], 41)
        self.assertEqual(self.fake_store.set_ai_market_decline_state.call_count, 2)

    def test_trading_date_none_does_not_fetch_or_save(self):
        # SPY等が1件も取得できず実取引日を特定できない場合、安全側に倒して
        # 41銘柄の新規取得・保存を一切行わない(架空の判定をしない)。
        fetch_mock = mock.patch.object(refresh, "_td_fetch_one", side_effect=AssertionError("取得してはいけない"))
        fetch_mock.start()
        self.addCleanup(fetch_mock.stop)

        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", None)
        self.assertEqual(state, {})  # prevがそもそも無かったため
        self.fake_store.set_ai_market_decline_state.assert_not_called()

    def test_trading_date_none_preserves_existing_previous_snapshot(self):
        self._patch_fetch({t: (100.0, 95.0) for t in refresh.AI_UNIVERSE})
        prev_state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")

        fetch_mock = mock.patch.object(refresh, "_td_fetch_one", side_effect=AssertionError("取得してはいけない"))
        fetch_mock.start()
        self.addCleanup(fetch_mock.stop)

        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-24", None)
        self.assertEqual(state, prev_state)
        self.fake_store.set_ai_market_decline_state.assert_called_once()  # None呼び出しでは追加保存されない

    def test_stored_date_is_trading_date_not_wall_clock_date(self):
        # 「時点」に使われるstate["date"]は、date_key(壁時計)ではなく
        # trading_date(実取引日)であることを直接確認する。
        self._patch_fetch({t: (100.0, 95.0) for t in refresh.AI_UNIVERSE})
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state["date"], "2026-10-22")
        self.assertNotEqual(state["date"], "2026-10-23")


class PartialFailureSafetyTests(unittest.TestCase):
    """【最重要1】41銘柄のうち一部が取得失敗した場合に、古いデータと新しい
    データを混在させて「本日分」を作らないことを検証する。
    - 全41銘柄成功 → 本日分として保存・表示
    - 1銘柄でも失敗 → 保存せず、前回の完全なスナップショットをそのまま維持
    """

    def setUp(self):
        self.fake_store = mock.Mock()
        self.saved_state = None
        self.fake_store.get_ai_market_decline_state.side_effect = lambda: self.saved_state
        self.fake_store.set_ai_market_decline_state.side_effect = self._save
        p1 = mock.patch.object(refresh, "store", self.fake_store)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        p2.start()
        self.addCleanup(p2.stop)

    def _save(self, data):
        self.saved_state = data

    def _patch_fetch(self, price_by_ticker):
        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            spec = price_by_ticker.get(ticker, (100.0, 95.0))
            if spec is None:
                return None, False, "NO_DATA", "test", None
            ath, current = spec
            return _series(ath, current), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

    def test_all_41_succeed_saves_and_returns_todays_snapshot(self):
        spec = {t: (100.0, 95.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(spec)
        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(state["date"], "2026-10-22")
        self.assertEqual(state["n_pop"], 41)
        self.fake_store.set_ai_market_decline_state.assert_called_once()

    def test_one_ticker_failure_does_not_mix_old_and_new_prices(self):
        # Day1: 全41銘柄成功、current_price=95・下落なし。
        day1_spec = {t: (100.0, 95.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(day1_spec)
        day1_state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(day1_state["n_down"], 0)

        # Day2: 実取引日が進み(2026-10-23)、1銘柄だけ取得失敗(残り40銘柄は
        # -31%の深い下落に変化)。
        day2_spec = {t: (100.0, 69.0) for t in refresh.AI_UNIVERSE}
        day2_spec[refresh.AI_UNIVERSE[0]] = None  # 1銘柄だけ取得失敗
        self._patch_fetch(day2_spec)
        day2_state = refresh._update_ai_market_decline_state("fake_key", "2026-10-24", "2026-10-23")

        # Day2は保存されず(Redisへの書き込みは1回=Day1分のみ)、Day1の
        # 完全なスナップショットがそのまま返っていることを確認する。
        # 「40銘柄は新しい-31%、1銘柄だけ古い95」のような混在状態には
        # 絶対になっていないこと(戻り値がDay1と完全に一致する)を検証する。
        self.assertEqual(self.fake_store.set_ai_market_decline_state.call_count, 1)
        self.assertEqual(day2_state, day1_state)
        self.assertEqual(day2_state["date"], "2026-10-22")  # Day2の取引日ではない
        self.assertEqual(day2_state["n_down"], 0)  # Day2の(本来29以上になるはずの)値ではない

    def test_rate_limit_mid_fetch_does_not_save_partial_day(self):
        call_order = list(refresh.AI_UNIVERSE)

        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            if ticker == call_order[5]:
                return None, True, "RATE_LIMIT", "test", None  # 6件目でRATE_LIMIT
            return _series(100.0, 50.0), False, None, None, None
        p = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p.start()
        self.addCleanup(p.stop)

        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertFalse(state.get("date"))  # prevがそもそも無かったため、本日分は公開されない
        self.fake_store.set_ai_market_decline_state.assert_not_called()

    def test_budget_exhaustion_mid_fetch_does_not_save_partial_day(self):
        budget_left = {"n": 10}  # 10銘柄分の予算しかない

        def fake_budget():
            return budget_left["n"], 0

        def fake_fetch(ticker, api_key, date_key, outputsize=None):
            budget_left["n"] -= 1
            return _series(100.0, 50.0), False, None, None, None

        p1 = mock.patch.object(refresh, "remaining_td_budget", side_effect=fake_budget)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(refresh, "_td_fetch_one", side_effect=fake_fetch)
        p2.start()
        self.addCleanup(p2.stop)

        state = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertFalse(state.get("date"))
        self.fake_store.set_ai_market_decline_state.assert_not_called()

    def test_retry_on_later_cron_firing_same_day_succeeds_after_earlier_failure(self):
        # 同じ取引日のまま、取りこぼし対策のcronが複数回走るケースを想定:
        # 1回目は1銘柄失敗で保存されず、2回目(同じtrading_date)で全銘柄
        # 成功すれば、その回はきちんと保存されることを確認する。
        failing_spec = {t: (100.0, 95.0) for t in refresh.AI_UNIVERSE}
        failing_spec[refresh.AI_UNIVERSE[0]] = None
        self._patch_fetch(failing_spec)
        first = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertFalse(first.get("date"))
        self.fake_store.set_ai_market_decline_state.assert_not_called()

        succeeding_spec = {t: (100.0, 95.0) for t in refresh.AI_UNIVERSE}
        self._patch_fetch(succeeding_spec)
        second = refresh._update_ai_market_decline_state("fake_key", "2026-10-23", "2026-10-22")
        self.assertEqual(second["date"], "2026-10-22")
        self.fake_store.set_ai_market_decline_state.assert_called_once()


class AiMarketDeclineBucketTests(unittest.TestCase):
    """app._ai_market_decline_bucketの境界値を検証する
    (0〜20/20〜40/40〜60/60〜80/80〜100の5区分、下限を含み上限を含まない)。"""

    def test_zero_is_in_first_bucket(self):
        b = app._ai_market_decline_bucket(0.0)
        self.assertEqual(b["label"], "0〜20%")

    def test_19_99_is_in_first_bucket(self):
        b = app._ai_market_decline_bucket(19.99)
        self.assertEqual(b["label"], "0〜20%")

    def test_20_0_is_in_second_bucket(self):
        b = app._ai_market_decline_bucket(20.0)
        self.assertEqual(b["label"], "20〜40%")

    def test_40_0_is_in_third_bucket(self):
        b = app._ai_market_decline_bucket(40.0)
        self.assertEqual(b["label"], "40〜60%")

    def test_60_0_is_in_fourth_bucket(self):
        b = app._ai_market_decline_bucket(60.0)
        self.assertEqual(b["label"], "60〜80%")

    def test_70_7_is_in_fourth_bucket_matching_user_example(self):
        b = app._ai_market_decline_bucket(70.7)
        self.assertEqual(b["label"], "60〜80%")
        self.assertAlmostEqual(b["mean_ret20"], 3.80)
        self.assertAlmostEqual(b["win_rate"], 56.3)

    def test_80_0_is_in_fifth_bucket(self):
        b = app._ai_market_decline_bucket(80.0)
        self.assertEqual(b["label"], "80〜100%")

    def test_100_0_is_in_fifth_bucket(self):
        b = app._ai_market_decline_bucket(100.0)
        self.assertEqual(b["label"], "80〜100%")


class BuildAiMarketDeclineViewTests(unittest.TestCase):
    """app._build_ai_market_decline_viewが、Redisの状態をそのまま表示用に
    組み立てるだけであることを検証する(他のロジックには一切接続しない)。"""

    def setUp(self):
        self.state = None
        p = mock.patch.object(app.store, "get_ai_market_decline_state", side_effect=lambda: self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_returns_none_when_not_yet_computed(self):
        self.state = None
        self.assertIsNone(app._build_ai_market_decline_view())

    def test_matches_user_provided_example_70_7_percent(self):
        self.state = {"date": "2026-10-23", "n_down": 29, "n_pop": 41, "pct_down": 70.7}
        view = app._build_ai_market_decline_view()
        self.assertEqual(view["n_down"], 29)
        self.assertEqual(view["n_pop"], 41)
        self.assertAlmostEqual(view["pct_down"], 70.7)
        self.assertEqual(view["date"], "2026-10-23")
        self.assertEqual(view["backtest"]["label"], "60〜80%")
        self.assertAlmostEqual(view["backtest"]["mean_ret20"], 3.80)
        self.assertAlmostEqual(view["backtest"]["win_rate"], 56.3)
        self.assertEqual(view["state_label"], "AI市場全体で調整が広がっている状態")

    def test_exposes_stale_date_when_falling_back_to_previous_complete_snapshot(self):
        # refresh側が「前回の完全なスナップショット」を返した場合、その
        # dateは当日ではない可能性がある。app側はそれをそのまま表示用に
        # 渡すだけで、架空の「今日」に書き換えたりしないことを確認する。
        self.state = {"date": "2026-10-20", "n_down": 10, "n_pop": 41, "pct_down": 24.4}
        view = app._build_ai_market_decline_view()
        self.assertEqual(view["date"], "2026-10-20")


class MainCallSiteTests(unittest.TestCase):
    """main()が_update_ai_market_decline_stateにtd_result["trading_date"]を
    そのまま渡していること(追加のAPI呼び出しなしで実取引日を再利用する
    design 2026-10-23修正)を検証する。"""

    def setUp(self):
        for target, value in (
            ("refresh.store.get_watchlist", ["AAPL"]),
            ("refresh.remaining_td_budget", (800, 0)),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p2 = mock.patch("refresh.update_rank_snapshot")
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch("refresh.store.set_last_refresh", return_value=True)
        p3.start()
        self.addCleanup(p3.stop)
        p4 = mock.patch.object(refresh.push_notify, "notify_refresh_success")
        p4.start()
        self.addCleanup(p4.stop)

    def test_main_passes_trading_date_from_td_result_without_extra_fetch(self):
        td_result = {
            "fetched": {}, "failed": [], "rate_limited": False,
            "trading_date": "2026-10-22", "quintile_updated": True,
            "raw_watchlist_data": {},
        }
        with mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "dummy-key"}, clear=True), \
             mock.patch("refresh.run_quintile_refresh", return_value=td_result), \
             mock.patch.object(refresh, "_update_ai_market_decline_state") as m:
            refresh.main()
        m.assert_called_once()
        args = m.call_args[0]
        self.assertEqual(args[2], "2026-10-22")  # trading_date位置引数

    def test_main_passes_none_trading_date_when_td_result_is_none(self):
        with mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "dummy-key"}, clear=True), \
             mock.patch("refresh.run_quintile_refresh", return_value=None), \
             mock.patch.object(refresh, "_update_ai_market_decline_state") as m:
            refresh.main()
        m.assert_called_once()
        args = m.call_args[0]
        self.assertIsNone(args[2])


class ExistingLogicUnaffectedTests(unittest.TestCase):
    """本パネルの追加・再設計によって、既存のQ1〜Q5判定・Q5 Day0判定・
    BUY判定・Risk-off判定に一切影響が出ていないことを確認する。"""

    def test_ai_universe_still_41_tickers(self):
        self.assertEqual(len(refresh.AI_UNIVERSE), 41)

    def test_riskoff_constants_unchanged(self):
        self.assertEqual(refresh.RISKOFF_ENTRY_DAYS, 10)
        self.assertEqual(refresh.RISKOFF_EXIT_DAYS, 10)
        self.assertEqual(refresh.RISKOFF_RATIO, 0.70)

    def test_ai_market_env_constants_unchanged(self):
        self.assertEqual(refresh.AI_MARKET_ENV_MIN_POP, 5)
        self.assertEqual(refresh.AI_MARKET_ENV_ADJUSTMENT_THRESHOLD, 50.0)

    def test_new_constants_independent_from_existing_ones(self):
        self.assertEqual(refresh.AI_MARKET_DECLINE_THRESHOLD_PCT, 30.0)
        self.assertNotEqual(refresh.AI_MARKET_DECLINE_THRESHOLD_PCT, refresh.AI_MARKET_ENV_ADJUSTMENT_THRESHOLD)

    def test_required_success_defaults_to_all_41(self):
        self.assertEqual(refresh.AI_MARKET_DECLINE_REQUIRED_SUCCESS, len(refresh.AI_UNIVERSE))

    def test_backtest_buckets_unchanged(self):
        buckets = refresh.AI_MARKET_DECLINE_BACKTEST_BUCKETS
        self.assertEqual(len(buckets), 5)
        expected = [
            (0, 20, 4.84, 56.4), (20, 40, 1.81, 50.4), (40, 60, 3.48, 55.9),
            (60, 80, 3.80, 56.3), (80, 101, 10.46, 56.8),
        ]
        for b, (lo, hi, mean_ret20, win_rate) in zip(buckets, expected):
            self.assertEqual(b["lo"], lo)
            self.assertEqual(b["hi"], hi)
            self.assertAlmostEqual(b["mean_ret20"], mean_ret20)
            self.assertAlmostEqual(b["win_rate"], win_rate)

    def test_td_fetch_one_default_outputsize_unchanged_for_existing_callers(self):
        # outputsize引数を追加したが、省略時はtd.OUTPUTSIZE(既存の全呼び出し元
        # と同じ挙動)であることを確認する(デフォルト引数が既存動作を変えない)。
        import inspect
        sig = inspect.signature(refresh._td_fetch_one)
        self.assertEqual(sig.parameters["outputsize"].default, refresh.td.OUTPUTSIZE)

    def test_compute_daily_actions_still_exists_and_callable(self):
        self.assertTrue(callable(app._compute_daily_actions))

    def test_build_riskoff_view_still_exists_and_callable(self):
        self.assertTrue(callable(app._build_riskoff_view))


if __name__ == "__main__":
    unittest.main()
