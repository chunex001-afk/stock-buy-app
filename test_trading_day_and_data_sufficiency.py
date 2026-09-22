# -*- coding: utf-8 -*-
"""取引日ベース化(2026-09-22)・Q5シグナル8取引日化・データ不足時の
「Q判定保留」の3点セットに対するテスト(土日・米国市場休場日調査を受けた
正式仕様変更)。

Twelve Data・Redisへは一切アクセスしない(refresh._td_fetch_oneとstoreを
すべてfake/mockに差し替える、test_backfill_new_ticker.pyと同じ方針)。

対応するユーザー要求のテスト番号(コメントに①〜⑩として付記):
 1. 同じ取引日で2回refreshしてもQ状態が進まない
 2. 土日相当の同一取引日ではQ5 Dayが進まない
 3. 米国休場日相当でもQ5 Dayが進まない
 4. 新しい取引日になったら1日だけ進む
 5. Q5 8取引日の有効期限
 6. 土日を挟んでも8取引日のカウントが増えない
 7. データ不足銘柄がQ1固定にならず「Q判定保留」になる
 8. データが十分になったら通常Q1〜Q5判定へ移行する
 9. SKHYでQ1固定表示にならない
10. 既存Q5履歴・Q5警告がデータ不足処理によって壊れない
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import app
import quintile_logic
import refresh


def _fake_series(n, end="2026-09-18", start_price=100.0):
    """テスト用の合成株価データ(平日のみの日付・単純な周期変動の終値)。
    test_backfill_new_ticker.py の _fake_series と同じ考え方。"""
    end_d = date.fromisoformat(end)
    dates = []
    d = end_d
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= timedelta(days=1)
    dates.reverse()

    closes = []
    price = start_price
    for i in range(n):
        price *= 1 + ((i % 7) - 3) * 0.002
        closes.append(round(price, 2))
    volumes = [1_000_000 + (i % 5) * 10_000 for i in range(n)]
    return dates, closes, volumes


class FakeStore:
    """refresh.store / app.store を丸ごと差し替える、最小限のインメモリfake。"""

    def __init__(self):
        self.quintile_states = {}
        self.pool_history = None
        self.refpool = {}

    def get_quintile_state(self, ticker):
        return self.quintile_states.get(ticker.upper())

    def set_quintile_state(self, ticker, data):
        self.quintile_states[ticker.upper()] = data
        return True

    def get_pool_history(self):
        return self.pool_history

    def set_pool_history(self, data):
        self.pool_history = data
        return True

    def get_refpool_score(self, ticker):
        return self.refpool.get(ticker.upper())

    def set_refpool_score(self, ticker, data):
        self.refpool[ticker.upper()] = data
        return True

    def get_ticker_record(self, ticker):
        return None

    def set_ticker_record(self, ticker, data):
        return True


def _fetch_side_effect(fixture_lengths, end_date, default_n=260):
    """refresh._td_fetch_one の代わりに使うside_effect。ticker毎に長さを
    変えられるようにし(データ不足銘柄の再現用)、それ以外は十分な長さ
    (デフォルト260件)の合成データを返す。"""
    def _side_effect(ticker, api_key, date_key):
        n = fixture_lengths.get(ticker.upper(), default_n)
        dates, closes, volumes = _fake_series(n, end=end_date)
        return (dates, closes, volumes), False, None, None
    return _side_effect


class TradingDayGatingTests(unittest.TestCase):
    """①土日・休場日問題: 「実際に取引日が進んだ場合のみ更新する」の検証。"""

    def setUp(self):
        self.store = FakeStore()
        store_patcher = mock.patch.object(refresh, "store", self.store)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

        budget_patcher = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)

        # knn_predict_scoreを固定値に差し替え、参照母集団・監視銘柄すべてが
        # 同じscoreになるようにする(=全銘柄Q5境界上で、確実にQ5になる)。
        # compute_features自体(9特徴量の定義)は無変更、呼び出し結果を使わない
        # だけなのでロジックには触れていない。
        score_patcher = mock.patch.object(quintile_logic, "knn_predict_score", return_value=999.0)
        score_patcher.start()
        self.addCleanup(score_patcher.stop)

    def _run(self, end_date, fixture_lengths=None):
        fixture_lengths = fixture_lengths or {}
        with mock.patch.object(refresh, "_td_fetch_one", side_effect=_fetch_side_effect(fixture_lengths, end_date)):
            return refresh.run_quintile_refresh("dummy-key", ["TESTCO"])

    # --- 1. 同じ取引日で2回refreshしてもQ状態が進まない -----------------
    def test_1_same_trading_day_twice_no_update(self):
        result1 = self._run(end_date="2026-09-18")
        self.assertTrue(result1["quintile_updated"])
        self.assertEqual(result1["trading_date"], "2026-09-18")
        state_after_1 = dict(self.store.get_quintile_state("TESTCO"))
        pool_dates_after_1 = list(self.store.pool_history["dates"])

        result2 = self._run(end_date="2026-09-18")  # 同じ最終取引日で再実行(土日再現)
        self.assertFalse(result2["quintile_updated"])
        self.assertEqual(result2["trading_date"], "2026-09-18")

        state_after_2 = self.store.get_quintile_state("TESTCO")
        self.assertEqual(state_after_1, state_after_2, "同じ取引日の再実行ではQ状態が一切変化しない")
        self.assertEqual(pool_dates_after_1, self.store.pool_history["dates"], "pool_historyの日付も増えない")

    # --- 2. 土日相当の同一取引日ではQ5 Dayが進まない ---------------------
    def test_2_weekend_simulated_no_q5_day_advance(self):
        self._run(end_date="2026-09-18")  # 金曜(仮)、ここでQ5突入・Day0記録
        price_path_after_friday = list(self.store.get_quintile_state("TESTCO")["q5_price_path"])
        self.assertEqual(len(price_path_after_friday), 1, "Day0の1件が記録されている")

        # 土曜・日曜にGitHub Actionsが起動しても、Twelve Dataの最終取引日は
        # 金曜のまま変わらない、という状況を再現する(end_dateを変えない)。
        self._run(end_date="2026-09-18")
        self._run(end_date="2026-09-18")

        price_path_after_weekend = self.store.get_quintile_state("TESTCO")["q5_price_path"]
        self.assertEqual(
            price_path_after_friday, price_path_after_weekend,
            "土日に2回ジョブが走ってもQ5 Day(q5_price_path)は増えない",
        )

    # --- 3. 米国休場日相当でもQ5 Dayが進まない ----------------------------
    def test_3_us_holiday_simulated_no_q5_day_advance(self):
        # 2026-09-07(月, Labor Day想定=休場)を最終取引日として2回連続実行
        # (休場日にジョブが起動しても、Twelve Dataの最終取引日は前営業日の
        # ままという状況を、同一end_dateの連続呼び出しで再現する)。
        self._run(end_date="2026-09-04")
        price_path_before = list(self.store.get_quintile_state("TESTCO")["q5_price_path"])

        self._run(end_date="2026-09-04")  # 休場日相当、取引日は進んでいない

        price_path_after = self.store.get_quintile_state("TESTCO")["q5_price_path"]
        self.assertEqual(price_path_before, price_path_after, "休場日相当の再実行でもQ5 Dayは増えない")

    # --- 4. 新しい取引日になったら1日だけ進む -----------------------------
    def test_4_new_trading_day_advances_by_exactly_one(self):
        self._run(end_date="2026-09-18")  # 金曜
        price_path_day0 = list(self.store.get_quintile_state("TESTCO")["q5_price_path"])
        pool_dates_day0 = list(self.store.pool_history["dates"])
        self.assertEqual(len(price_path_day0), 1)

        result = self._run(end_date="2026-09-21")  # 次の実際の取引日(月曜、週末を挟む)
        self.assertTrue(result["quintile_updated"])
        self.assertEqual(result["trading_date"], "2026-09-21")

        price_path_day1 = self.store.get_quintile_state("TESTCO")["q5_price_path"]
        pool_dates_day1 = self.store.pool_history["dates"]
        self.assertEqual(len(price_path_day1), 2, "新しい取引日1回につきDayが1つだけ進む")
        self.assertEqual(len(pool_dates_day1), len(pool_dates_day0) + 1, "pool_historyも1日分だけ増える")
        self.assertEqual(price_path_day1[0], price_path_day0[0], "Day0のエントリは変わらない")


class Q5SignalTradingDayTests(unittest.TestCase):
    """②前回Q5シグナル8取引日化: app._compute_q5_signal の検証(純粋関数、
    Redis/Twelve Dataへのアクセスなし)。"""

    def setUp(self):
        # 平日のみの取引日カレンダーを作る(9/14(月)〜10/2(金)の3週間分)。
        d = date.fromisoformat("2026-09-14")
        dates = []
        while d <= date.fromisoformat("2026-10-02"):
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += timedelta(days=1)
        self.calendar = dates  # 実取引日のみ、土日は含まれない

    def _history_departing_on(self, depart_date):
        # Q5エントリ(離脱の前営業日)→離脱日(Q4)、という最小のhistory。
        idx = self.calendar.index(depart_date)
        q5_date = self.calendar[idx - 1]
        return [{"date": q5_date, "q": "Q5"}, {"date": depart_date, "q": "Q4"}]

    # --- 5. Q5 8取引日の有効期限 -------------------------------------------
    def test_5_q5_signal_expiry_at_8_trading_days(self):
        depart_date = "2026-09-15"  # 火曜離脱
        history = self._history_departing_on(depart_date)
        depart_idx = self.calendar.index(depart_date)

        # 離脱日を1取引日目として、7取引日目まではactive
        anchor_day7 = self.calendar[depart_idx + 6]
        sig_day7 = app._compute_q5_signal("Q4", history, anchor_day7, self.calendar)
        self.assertEqual(sig_day7["status"], "active")
        self.assertEqual(sig_day7["days_elapsed"], 7)

        # 8取引日目でexpired
        anchor_day8 = self.calendar[depart_idx + 7]
        sig_day8 = app._compute_q5_signal("Q4", history, anchor_day8, self.calendar)
        self.assertEqual(sig_day8["status"], "expired")
        self.assertEqual(sig_day8["days_elapsed"], 8)

    # --- 6. 土日を挟んでも8取引日のカウントが増えない -----------------------
    def test_6_weekend_does_not_inflate_trading_day_count(self):
        # 金曜に離脱 → 暦日では翌営業日の月曜は3暦日後だが、取引日としては
        # 2取引日目(金=1, 月=2)であることを確認する。
        depart_date = "2026-09-18"  # 金曜
        history = self._history_departing_on(depart_date)
        monday = "2026-09-21"

        sig = app._compute_q5_signal("Q4", history, monday, self.calendar)
        self.assertEqual(sig["days_elapsed"], 2, "金→月は暦日3日だが取引日は2日")
        self.assertEqual(sig["status"], "active")

        # 暦日ベースの旧実装なら(9/18→10/... 8暦日後)で失効していたはずの
        # 日付でも、間に土日を4回挟むだけなら取引日はまだ8日に届かないことを確認。
        anchor_still_active = self.calendar[self.calendar.index(depart_date) + 6]  # 7取引日目
        sig2 = app._compute_q5_signal("Q4", history, anchor_still_active, self.calendar)
        self.assertEqual(sig2["days_elapsed"], 7)
        self.assertEqual(sig2["status"], "active")


class InsufficientDataTests(unittest.TestCase):
    """③データ不足銘柄(SKHY等)の「Q判定保留」化の検証。"""

    def setUp(self):
        self.store = FakeStore()
        # refresh.py(日次バッチ)とapp.py(表示)は別々にredis_storeをimportして
        # いるため(それぞれ独立したstore名)、両方を同じFakeStoreインスタンスに
        # 差し替えないと、refreshが書いた状態をapp側が見えない。
        refresh_store_patcher = mock.patch.object(refresh, "store", self.store)
        refresh_store_patcher.start()
        self.addCleanup(refresh_store_patcher.stop)
        app_store_patcher = mock.patch.object(app, "store", self.store)
        app_store_patcher.start()
        self.addCleanup(app_store_patcher.stop)

        budget_patcher = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)

        score_patcher = mock.patch.object(quintile_logic, "knn_predict_score", return_value=999.0)
        score_patcher.start()
        self.addCleanup(score_patcher.stop)

    def _run(self, end_date, fixture_lengths):
        with mock.patch.object(refresh, "_td_fetch_one", side_effect=_fetch_side_effect(fixture_lengths, end_date)):
            return refresh.run_quintile_refresh("dummy-key", ["SKHY"])

    # --- 7. データ不足銘柄がQ1固定にならず「Q判定保留」になる ---------------
    def test_7_insufficient_data_becomes_pending_not_q1(self):
        # SKHY実データ相当(51営業日)で実行する。
        self._run(end_date="2026-09-21", fixture_lengths={"SKHY": 51})

        state = self.store.get_quintile_state("SKHY")
        self.assertIsNone(state["current_q"], "current_qはNone(=保留)であり、Q1が書き込まれてはいけない")
        self.assertEqual(state["data_status"], "insufficient")
        self.assertEqual(state["data_days"], 51)
        self.assertEqual(state["data_days_required"], quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES)

        # app.py側の表示もQ1バッジではなく「pending」になることを確認する。
        view = app._build_quintile_view("SKHY", trading_calendar=[])
        self.assertEqual(view["status"], "pending")
        self.assertIsNone(view["current_q"])
        self.assertIn("51", view["message"])
        self.assertIn("200", view["message"])

    # --- 8. データが十分になったら通常Q1〜Q5判定へ移行する -------------------
    def test_8_transitions_to_normal_judgment_once_enough_data(self):
        self._run(end_date="2026-09-21", fixture_lengths={"SKHY": 51})
        self.assertIsNone(self.store.get_quintile_state("SKHY")["current_q"])

        # 後日、データが200営業日分に達した状態で次の取引日の更新が走った場合。
        result = self._run(end_date="2026-09-22", fixture_lengths={"SKHY": 200})
        self.assertTrue(result["quintile_updated"])

        state = self.store.get_quintile_state("SKHY")
        self.assertEqual(state["current_q"], "Q5", "score=999固定のため、十分なデータになればQ5と判定される")
        self.assertIsNone(state.get("data_status"), "通常判定に移行したらdata_status(保留マーカー)は残らない")

        view = app._build_quintile_view("SKHY", trading_calendar=[])
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["current_q"], "Q5")

    # --- 9. SKHYでQ1固定表示にならない --------------------------------------
    def test_9_skhy_does_not_show_fixed_q1(self):
        # 51営業日 → 60営業日(まだ不足)と、複数回の取引日更新を経ても、
        # 一度もcurrent_q="Q1"が書き込まれないことを確認する。
        for end_date, n in [("2026-09-18", 51), ("2026-09-21", 52), ("2026-09-22", 53)]:
            self._run(end_date=end_date, fixture_lengths={"SKHY": n})
            state = self.store.get_quintile_state("SKHY")
            self.assertNotEqual(state.get("current_q"), "Q1", f"{end_date}時点でQ1が書き込まれてはいけない")
            self.assertIsNone(state.get("current_q"))


class PreserveExistingQ5StateTests(unittest.TestCase):
    """③の重要要件: 既存の正常なQ5履歴・Q5クール・Q5警告を、データ不足処理が
    破壊しないことの検証(refresh._mark_quintile_state_insufficientを直接テスト)。"""

    def test_10_existing_q5_history_and_warning_survive_insufficient_marking(self):
        store_ = FakeStore()
        existing_history = [
            {"date": "2026-08-01", "q": "Q3"},
            {"date": "2026-08-10", "q": "Q5"},
            {"date": "2026-08-20", "q": "Q4"},
        ]
        existing_price_path = [
            {"date": "2026-08-10", "price": 100.0},
            {"date": "2026-08-11", "price": 105.0},
        ]
        existing_warning = {"triggered_date": "2026-08-17", "triggered_return_pct": -8.2, "days_since_trigger": 3}
        store_.quintile_states["LEGIT"] = {
            "current_q": "Q4",
            "previous_q": "Q5",
            "pred_score": 12.3,
            "history": existing_history,
            "q5_price_path": existing_price_path,
            "q5_warning": existing_warning,
            "last_updated": "2026-08-20",
        }

        with mock.patch.object(refresh, "store", store_):
            refresh._mark_quintile_state_insufficient("LEGIT", "2026-08-21", 55, 200)

        new_state = store_.get_quintile_state("LEGIT")
        self.assertIsNone(new_state["current_q"])
        self.assertEqual(new_state["data_status"], "insufficient")
        self.assertEqual(new_state["data_days"], 55)
        self.assertEqual(new_state["data_days_required"], 200)
        # 既存のQ5関連フィールドは一切変更されていないこと。
        self.assertEqual(new_state["history"], existing_history)
        self.assertEqual(new_state["q5_price_path"], existing_price_path)
        self.assertEqual(new_state["q5_warning"], existing_warning)
        self.assertEqual(new_state["previous_q"], "Q5")
        self.assertEqual(new_state["pred_score"], 12.3)


if __name__ == "__main__":
    unittest.main()
