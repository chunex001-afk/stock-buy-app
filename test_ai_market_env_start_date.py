# -*- coding: utf-8 -*-
"""AI関連一斉調整パネルの「開始日」表示変更(2026-10-15ユーザー要望)に対する
テスト。

背景: 本番表示では「初回確認：10/14」と出ていたが、実データ上は10/8に
一斉調整が始まっていた(アプリが母集団不足等で10/8〜10/13を判定できず、
10/14に初めてactiveを確認したため)。これを「アプリが最初にactiveを
確認した日」ではなく「一斉調整が実際に始まった日」に変更する
(refresh._find_ai_market_env_backfill、2026-10-15追加)。

このテストでは10/8や10/14という特定の日付を一切使わない(ハードコード
していないことの確認を兼ねる)。合成データの中で「いつ下落が始まったか」
を変えても、その日が正しくstart_dateとして検出されることを確認する。

Twelve Data・Redisへは一切アクセスしない(test_trading_day_and_data_sufficiency.py
と同じ方針、_find_ai_market_env_backfill/_update_ai_market_envはRedisアクセス
なしの関数なので直接呼び出すだけで十分)。
"""
import copy
import unittest
from datetime import date, timedelta
from unittest import mock

import refresh

# テストに使う5銘柄(AI_UNIVERSEに実在するもの、AI_MARKET_ENV_MIN_POP=5件を満たす)。
_POP_TICKERS = ["NVDA", "AMD", "MU", "ON", "AVGO"]
for _t in _POP_TICKERS:
    assert _t in refresh.AI_UNIVERSE, f"{_t} はAI_UNIVERSEに存在しない想定外のテスト前提"


def _business_days(n, end):
    """endまでのn営業日(平日のみ)の日付文字列リストを古い順に返す。"""
    end_d = date.fromisoformat(end)
    dates = []
    d = end_d
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= timedelta(days=1)
    dates.reverse()
    return dates


def _build_scenario(drop_idx, n_days=40, end="2026-11-30"):
    """calendar(n_days営業日、末尾=today)の中で、drop_idx日目(0始まり)から
    全銘柄が「SMA20/25を安値が下回る」状態に変わる合成シナリオを作る。
    drop_idx未満の日は closeとlowが一致したフラットな価格(=割れない)。

    戻り値: (calendar, states_today, fetched, fetched_lows, today)。
    どの銘柄も一貫してQ5(calendar[0]からQ5、q5_warningは無効)という前提。
    drop_idxを変えるだけで「いつ一斉調整が始まったか」を自由に動かせる
    ことを確認するためのヘルパー(=本番コードが特定日をハードコードして
    いないことの検証に使う)。"""
    calendar = _business_days(n_days, end)
    today = calendar[-1]

    states_today = {}
    fetched = {}
    fetched_lows = {}
    for ticker in _POP_TICKERS:
        closes = [100.0 if i < drop_idx else 60.0 for i in range(n_days)]
        lows = [100.0 if i < drop_idx else 55.0 for i in range(n_days)]
        volumes = [1_000_000] * n_days
        fetched[ticker] = (calendar, closes, volumes)
        fetched_lows[ticker] = lows
        states_today[ticker] = {
            "current_q": "Q5",
            "history": [{"date": calendar[0], "q": "Q5"}],
            "q5_price_path": [{"date": calendar[0], "price": 100.0}],
            "q5_warning": None,
        }
    return calendar, states_today, fetched, fetched_lows, today


class FindBackfillStartDateTests(unittest.TestCase):
    """_find_ai_market_env_backfillが「実際に下落が始まった日」を遡って
    特定できることの検証(特定の日付をハードコードしていないことの確認を
    兼ねて、drop_idxを変えて複数パターンで検証する)。"""

    def test_backfill_finds_true_start_not_first_confirmed_day(self):
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=30)
        expected_start = calendar[30]

        start_date, backfilled = refresh._find_ai_market_env_backfill(
            states_today, fetched, fetched_lows, calendar, today,
        )

        self.assertEqual(start_date, expected_start)
        # 10/14のような「today」ではなく、実際に下落が始まった過去日になっている。
        self.assertNotEqual(start_date, today)
        # backfilled(start_date〜前日)にF値が積まれている。
        self.assertEqual(backfilled[0]["date"], expected_start)
        self.assertEqual(backfilled[-1]["date"], calendar[-2])
        self.assertEqual(len(backfilled), (len(calendar) - 1) - 30)

    def test_backfill_start_date_moves_with_the_data_not_hardcoded(self):
        """drop_idxを変えて「本当の開始日」が変わっても、検出結果が
        そのまま追従することを確認する(=特定日付のハードコードではない)。
        SMA25の算出に最低25日分の終値が必要なため、drop_idx>=24のケースで
        検証する(drop_idx<24のケースはデータ不足側の挙動として別テストで
        確認する)。"""
        for drop_idx in (24, 28, 33):
            with self.subTest(drop_idx=drop_idx):
                calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=drop_idx)
                expected_start = calendar[drop_idx]
                start_date, _ = refresh._find_ai_market_env_backfill(
                    states_today, fetched, fetched_lows, calendar, today,
                )
                self.assertEqual(start_date, expected_start)

    def test_backfill_floors_at_earliest_computable_day_when_data_insufficient(self):
        """下落開始がSMA25計算に必要な最低日数(25日)より前だった場合、
        それより過去は「架空の値は作らない」既存方針どおり判定不能として
        遡及を止める(=計算可能な最も古い日が開始日になる)。これは
        データ不足側の挙動であり、特定日付のハードコードではない。"""
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=15)
        start_date, _ = refresh._find_ai_market_env_backfill(
            states_today, fetched, fetched_lows, calendar, today,
        )
        self.assertEqual(start_date, calendar[24])

    def test_backfill_does_not_mutate_inputs(self):
        """states_today/fetched/fetched_lowsは読み取り専用(Q1〜Q5判定・
        history・q5_price_path・q5_warningを一切書き換えない)ことの確認。"""
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=25)
        before = copy.deepcopy(states_today)

        refresh._find_ai_market_env_backfill(states_today, fetched, fetched_lows, calendar, today)

        self.assertEqual(states_today, before)

    def test_backfill_falls_back_to_today_when_no_usable_data(self):
        """遡及に使えるデータが無い(fetchedが空等)場合は、現状と同じ
        「今日を開始日とする」挙動にフォールバックする。"""
        calendar, states_today, _fetched, _fetched_lows, today = _build_scenario(drop_idx=30)
        start_date, backfilled = refresh._find_ai_market_env_backfill(
            states_today, {}, {}, calendar, today,
        )
        self.assertEqual(start_date, today)
        self.assertEqual(backfilled, [])


class UpdateAiMarketEnvStartDateTests(unittest.TestCase):
    """_update_ai_market_env全体を通した検証: 初めてactiveを検知した日に
    start_dateが過去に遡って設定され、以降継続中の日はその値を保持し続ける
    (再度遡及計算をやり直さない)ことを確認する。"""

    def setUp(self):
        self.fake_store = mock.Mock()
        self.fake_store.get_ai_market_env_state.return_value = None
        store_patcher = mock.patch.object(refresh, "store", self.fake_store)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    def test_first_activation_backdates_start_date(self):
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=30)
        expected_start = calendar[30]

        new_state = refresh._update_ai_market_env(states_today, fetched, fetched_lows, calendar, today)

        self.assertTrue(new_state["active"])
        self.assertEqual(new_state["start_date"], expected_start)
        self.assertNotEqual(new_state["start_date"], today)
        # F値推移は開始日から当日まで(backfill分 + 当日分)。
        self.assertEqual(new_state["f_history"][0]["date"], expected_start)
        self.assertEqual(new_state["f_history"][-1]["date"], today)

    def test_continuing_day_keeps_backdated_start_date(self):
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=30, n_days=41)
        day1 = calendar[-2]
        day1_calendar = calendar[:-1]
        day2 = calendar[-1]

        fetched_day1 = {t: (v[0][:-1], v[1][:-1], v[2][:-1]) for t, v in fetched.items()}
        fetched_lows_day1 = {t: v[:-1] for t, v in fetched_lows.items()}
        state_after_day1 = refresh._update_ai_market_env(
            states_today, fetched_day1, fetched_lows_day1, day1_calendar, day1,
        )
        self.assertTrue(state_after_day1["active"])
        expected_start = calendar[30]
        self.assertEqual(state_after_day1["start_date"], expected_start)

        self.fake_store.get_ai_market_env_state.return_value = state_after_day1
        state_after_day2 = refresh._update_ai_market_env(
            states_today, fetched, fetched_lows, calendar, day2,
        )
        # 継続中はstart_dateを保持し、再遡及しない(F履歴に1日分だけ追記)。
        self.assertEqual(state_after_day2["start_date"], expected_start)
        self.assertEqual(len(state_after_day2["f_history"]), len(state_after_day1["f_history"]) + 1)
        self.assertEqual(state_after_day2["f_history"][-1]["date"], day2)
        self.assertTrue(state_after_day2.get("backfill_applied"))


class ExistingActiveStateBackfillRepairTests(unittest.TestCase):
    """2026-10-15ユーザー報告: 本番でactive=True・start_date=初回確認日のまま
    保存されていた既存状態(=今回の機能追加より前にinactive→activeの遷移が
    確定していたケース)が、「backfill未適用」のまま残っていても、次回の
    通常更新でstart_date/f_historyがセットで正しく復元されることの検証。"""

    def setUp(self):
        self.fake_store = mock.Mock()
        store_patcher = mock.patch.object(refresh, "store", self.fake_store)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    def _legacy_prev(self, today, confirmed_f_score=56.0):
        """旧実装(今回の遡及機能より前)が保存していたと想定される状態:
        「アプリが最初にactiveを確認した日」だけがstart_dateになっており、
        f_historyもその1日分しか無い。backfill_appliedフィールドは存在しない
        (旧実装はこのフィールドを書いていなかった)。"""
        return {
            "active": True,
            "start_date": today,
            "f_history": [{"date": today, "f_score": confirmed_f_score}],
            "n_pop": 5,
            "last_updated": today,
        }

    def test_legacy_active_state_is_repaired_with_matching_start_date_and_history(self):
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=28)
        true_start = calendar[28]
        self.fake_store.get_ai_market_env_state.return_value = self._legacy_prev(today)

        new_state = refresh._update_ai_market_env(states_today, fetched, fetched_lows, calendar, today)

        self.assertTrue(new_state["active"])
        self.assertTrue(new_state["backfill_applied"])
        self.assertEqual(new_state["start_date"], true_start)
        self.assertNotEqual(new_state["start_date"], today)
        # start_dateとf_historyが必ずセットで復元される(先頭日付が完全一致、
        # 「start_dateだけ更新してf_historyが古いまま」という不整合が無い)。
        self.assertEqual(new_state["f_history"][0]["date"], new_state["start_date"])
        self.assertEqual(new_state["f_history"][-1]["date"], today)
        self.assertGreater(len(new_state["f_history"]), 1)

    def test_repair_moves_with_the_data_for_different_start_dates(self):
        """開始日が異なる複数パターンでも、ハードコードではなく実データに
        追従して正しくstart_date/f_historyが復元されることの確認。"""
        for drop_idx in (24, 28, 33):
            with self.subTest(drop_idx=drop_idx):
                calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=drop_idx)
                true_start = calendar[drop_idx]
                self.fake_store.get_ai_market_env_state.return_value = self._legacy_prev(today)

                new_state = refresh._update_ai_market_env(states_today, fetched, fetched_lows, calendar, today)

                self.assertEqual(new_state["start_date"], true_start)
                self.assertEqual(new_state["f_history"][0]["date"], true_start)
                self.assertEqual(new_state["f_history"][0]["date"], new_state["start_date"])

    def test_already_backfilled_state_is_not_recomputed(self):
        """backfill_applied: Trueが既についている状態は、遡及処理を
        再実行しない(毎日の不要な再計算をしないことの確認)。"""
        calendar, states_today, fetched, fetched_lows, today = _build_scenario(drop_idx=30)
        prev = {
            "active": True,
            "start_date": calendar[30],
            "f_history": [{"date": calendar[30], "f_score": 46.0}, {"date": calendar[31], "f_score": 52.0}],
            "backfill_applied": True,
            "n_pop": 5,
            "last_updated": calendar[31],
        }
        self.fake_store.get_ai_market_env_state.return_value = prev

        with mock.patch.object(refresh, "_find_ai_market_env_backfill") as backfill_mock:
            new_state = refresh._update_ai_market_env(states_today, fetched, fetched_lows, calendar, today)

        backfill_mock.assert_not_called()
        self.assertEqual(new_state["start_date"], calendar[30])
        self.assertEqual(len(new_state["f_history"]), 3)
        self.assertEqual(new_state["f_history"][-1]["date"], today)
        self.assertEqual(new_state["f_history"][0]["date"], new_state["start_date"])


if __name__ == "__main__":
    unittest.main()
