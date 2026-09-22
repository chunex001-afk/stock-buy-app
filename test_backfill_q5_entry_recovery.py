# -*- coding: utf-8 -*-
"""Q5バックフィル(過去エントリー復元、design 2026-09-22ユーザー確定仕様)の
テスト。新規銘柄追加時に「追加した日」をQ5 Day0にせず、過去BACKFILL_DAYS_BACK
(5)取引日以内に実際のQ5エントリー(前日Q5でない→当日Q5)があれば、その日を
Day0としてq5_price_pathを取引日ベースで復元する機能を検証する。

Twelve Data・Redisへは一切アクセスしない(test_backfill_new_ticker.pyと同じ
方針でfetchとstoreをmockする)。refresh._compute_backfill_dayをtarget_idx
キーの辞書でmockし、9特徴量・KNN・分位境界の実計算を経由せずに「各日のQが
何であるか」を直接指定する(_find_recent_q5_entry_index・q5_price_path
再構成ロジック自体は本物のコードをそのまま通す)。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import refresh


def _fake_series(n=260, end="2026-09-22", start_price=100.0):
    """test_backfill_new_ticker.pyと同じ合成株価データ生成(平日のみ)。
    このテストでは_compute_backfill_dayをmockするため中身の値自体は使われ
    ないが、len(dates)=nに基づいてrefresh側がtarget_idxを計算するために
    必要(日付・株価の実データとしての整合性は問わない)。"""
    end_d = date.fromisoformat(end)
    dates = []
    d = end_d
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= timedelta(days=1)
    dates.reverse()
    closes = [start_price] * n
    volumes = [1_000_000] * n
    return dates, closes, volumes


def _entry(date_str, q, price, score=10.0):
    return {"date": date_str, "q": q, "score": score, "price": price}


class BackfillQ5EntryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dates, self.closes, self.volumes = _fake_series()
        self.n_dates = len(self.dates)

        budget_patcher = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)

    def _patch_store(self):
        patchers = [
            mock.patch.object(refresh.store, "get_quintile_state", return_value=None),
            mock.patch.object(refresh.store, "get_ticker_record", return_value=None),
            mock.patch.object(refresh.store, "get_pool_history", return_value={
                "dates": ["2026-01-01"], "scores_by_date": {"2026-01-01": [1.0]},
            }),
            mock.patch.object(refresh.store, "set_quintile_state"),
            mock.patch.object(refresh.store, "set_ticker_record"),
        ]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        return mocks  # get_state, get_record, get_pool, set_state, set_record

    def _patch_fetch_success(self):
        return mock.patch.object(
            refresh, "_td_fetch_one",
            return_value=((self.dates, self.closes, self.volumes), False, None, None),
        )

    def _patch_backfill_days(self, fixture_by_k):
        """fixture_by_k: {k(0=今日, 5=5取引日前, 6=探索窓のさらに1日前): entry|None}
        をtarget_idxベースの辞書に変換し、refresh._compute_backfill_dayを
        差し替える。定義していないkはNoneを返す(=その日は計算不能扱い)。"""
        by_target_idx = {
            self.n_dates - 1 - k: entry for k, entry in fixture_by_k.items()
        }

        def _side_effect(ticker, dates, closes, volumes, target_idx, pool_history):
            return by_target_idx.get(target_idx)

        return mock.patch.object(refresh, "_compute_backfill_day", side_effect=_side_effect)

    def _run_backfill(self, fixture_by_k, ticker="TESTCO"):
        _, _, _, set_state, _set_record = self._patch_store()
        with self._patch_fetch_success(), self._patch_backfill_days(fixture_by_k):
            result = refresh.backfill_quintile_history_for_new_ticker(ticker, "key")
        self.assertTrue(result["ok"])
        set_state.assert_called_once()
        new_state = set_state.call_args[0][1]
        return new_state

    # ------------------------------------------------------------------
    # 1. 過去5取引日以内にQ5エントリーがあるケース(休場日を挟むケースも兼ねる、
    #    ユーザー提示の例と同一: 9/17エントリー→9/18→(9/19休場)→9/22登録)
    # ------------------------------------------------------------------
    def test_q5_entry_found_within_5_trading_days_reconstructs_price_path(self):
        fixture = {
            0: _entry("2026-09-22", "Q4", price=90.0),   # 今日(登録日) = Day2
            1: _entry("2026-09-18", "Q5", price=100.0),  # Day1 (9/19は休場のため存在しない)
            2: _entry("2026-09-17", "Q5", price=105.0),  # Day0(エントリー日)
            3: _entry("2026-09-16", "Q3", price=95.0),
            4: _entry("2026-09-15", "Q3", price=94.0),
            5: _entry("2026-09-14", "Q3", price=93.0),
            6: _entry("2026-09-11", "Q3", price=92.0),
        }
        new_state = self._run_backfill(fixture)

        self.assertEqual(new_state["current_q"], "Q4")  # 通常のcurrent_qは無変更
        price_path = new_state["q5_price_path"]
        self.assertEqual(
            price_path,
            [
                {"date": "2026-09-17", "price": 105.0},
                {"date": "2026-09-18", "price": 100.0},
                {"date": "2026-09-22", "price": 90.0},
            ],
            "9/17をDay0、9/18をDay1、休場日を挟んだ9/22をDay2として復元される",
        )
        self.assertIsNone(new_state["q5_warning"], "Day5未満のためwarningはまだ発生しない")

    # ------------------------------------------------------------------
    # 2. 過去5取引日以内にQ5エントリーがないケース
    # ------------------------------------------------------------------
    def test_no_q5_entry_within_window_does_not_create_price_path(self):
        fixture = {
            0: _entry("2026-09-22", "Q3", price=90.0),
            1: _entry("2026-09-21", "Q3", price=91.0),
            2: _entry("2026-09-18", "Q2", price=89.0),
            3: _entry("2026-09-17", "Q2", price=88.0),
            4: _entry("2026-09-16", "Q4", price=87.0),
            5: _entry("2026-09-15", "Q3", price=86.0),
            6: _entry("2026-09-14", "Q3", price=85.0),
        }
        new_state = self._run_backfill(fixture)

        self.assertEqual(new_state["current_q"], "Q3")
        self.assertNotIn("q5_price_path", new_state, "Q5エントリーが無ければq5_price_pathは作らない")
        self.assertNotIn("q5_warning", new_state, "Q5エントリーが無ければq5_warningも作らない")

    # ------------------------------------------------------------------
    # 2b. 探索窓の最古日(5取引日前)自身がQ5だが、その前日(探索窓のさらに外)も
    #     Q5だったため「エントリーではなく継続」と正しく判定されるケース
    #     (過去のクールが既に進行中だった=新規のエントリーではない、という
    #     境界ケースの確認)。
    # ------------------------------------------------------------------
    def test_oldest_day_in_window_already_q5_before_window_is_not_treated_as_entry(self):
        fixture = {
            0: _entry("2026-09-22", "Q4", price=90.0),
            1: _entry("2026-09-21", "Q4", price=91.0),
            2: _entry("2026-09-18", "Q4", price=92.0),
            3: _entry("2026-09-17", "Q4", price=93.0),
            4: _entry("2026-09-16", "Q4", price=94.0),
            5: _entry("2026-09-15", "Q5", price=95.0),  # 窓の最古日、Q5
            6: _entry("2026-09-14", "Q5", price=96.0),  # 窓の外、これもQ5 → 継続であってエントリーではない
        }
        new_state = self._run_backfill(fixture)
        self.assertNotIn("q5_price_path", new_state, "窓の外から既にQ5だった場合はエントリーとして扱わない")

    # ------------------------------------------------------------------
    # 3. Day5まで到達しているケース(-7.5%以下で警告も発生する)
    # ------------------------------------------------------------------
    def test_day5_reached_triggers_q5_warning_when_return_below_threshold(self):
        fixture = {
            6: _entry("2026-09-11", "Q3", price=99.0),   # エントリー確認用の前日(Q5でない)
            5: _entry("2026-09-14", "Q5", price=100.0),  # Day0
            4: _entry("2026-09-15", "Q5", price=100.0),  # Day1
            3: _entry("2026-09-16", "Q5", price=100.0),  # Day2
            2: _entry("2026-09-17", "Q4", price=95.0),   # Day3
            1: _entry("2026-09-18", "Q4", price=90.0),   # Day4
            0: _entry("2026-09-22", "Q4", price=90.0),   # Day5(今日) 100→90 = -10%
        }
        new_state = self._run_backfill(fixture)

        price_path = new_state["q5_price_path"]
        self.assertEqual(len(price_path), 6, "Day0〜Day5の6件が揃う")
        warning = new_state["q5_warning"]
        self.assertIsNotNone(warning, "Day5起点リターン-10%は-7.5%以下のため警告が発生する")
        self.assertEqual(warning["triggered_date"], "2026-09-22")
        self.assertAlmostEqual(warning["triggered_return_pct"], -10.0)
        self.assertEqual(warning["days_since_trigger"], 0)

    def test_day5_reached_but_return_above_threshold_no_warning(self):
        fixture = {
            6: _entry("2026-09-11", "Q3", price=99.0),
            5: _entry("2026-09-14", "Q5", price=100.0),  # Day0
            4: _entry("2026-09-15", "Q5", price=101.0),  # Day1
            3: _entry("2026-09-16", "Q5", price=102.0),  # Day2
            2: _entry("2026-09-17", "Q4", price=99.0),   # Day3
            1: _entry("2026-09-18", "Q4", price=98.0),   # Day4
            0: _entry("2026-09-22", "Q4", price=97.0),   # Day5、100→97 = -3%(閾値-7.5%より上)
        }
        new_state = self._run_backfill(fixture)
        self.assertEqual(len(new_state["q5_price_path"]), 6)
        self.assertIsNone(new_state["q5_warning"], "-3%は-7.5%以下ではないため警告は発生しない")

    # ------------------------------------------------------------------
    # 4. Day5未満のケース(かつ、そこから通常の日次更新で継続できることの確認)
    # ------------------------------------------------------------------
    def test_below_day5_continues_correctly_via_normal_daily_update(self):
        fixture = {
            0: _entry("2026-09-22", "Q4", price=90.0),   # Day2(今日)
            1: _entry("2026-09-18", "Q5", price=100.0),  # Day1
            2: _entry("2026-09-17", "Q5", price=105.0),  # Day0
            3: _entry("2026-09-16", "Q3", price=95.0),
            4: _entry("2026-09-15", "Q3", price=94.0),
            5: _entry("2026-09-14", "Q3", price=93.0),
            6: _entry("2026-09-11", "Q3", price=92.0),
        }
        new_state = self._run_backfill(fixture)
        self.assertEqual(len(new_state["q5_price_path"]), 3, "Day0〜Day2の3件、Day5未満")
        self.assertIsNone(new_state["q5_warning"])

        # バックフィルされたstateを使って、翌取引日の通常の日次更新
        # (_update_quintile_state)が正しくDay3として継続することを確認する。
        fake_store_state = {"TESTCO": new_state}
        with mock.patch.object(refresh.store, "get_quintile_state", side_effect=lambda t: fake_store_state.get(t)), \
             mock.patch.object(refresh.store, "set_quintile_state", side_effect=lambda t, v: fake_store_state.__setitem__(t, v)):
            bounds = [1, 2, 3, 4]
            updated = refresh._update_quintile_state("TESTCO", score=3.5, bounds=bounds, date_key="2026-09-23", price=88.0)

        self.assertEqual(len(updated["q5_price_path"]), 4, "通常の日次更新でDay3が1件だけ追加される")
        self.assertEqual(updated["q5_price_path"][-1], {"date": "2026-09-23", "price": 88.0})
        self.assertEqual(updated["q5_price_path"][0], {"date": "2026-09-17", "price": 105.0}, "Day0は変わらない")

    # ------------------------------------------------------------------
    # 5. 休場日を挟むケース(1のテストでも兼ねているが、日付の並びだけを
    #    明示的に確認する専用テスト)
    # ------------------------------------------------------------------
    def test_holiday_gap_between_entry_days_is_skipped_naturally(self):
        # Day0(9/17)→Day1(9/18)→(9/19,9/20,9/21は休場/週末、dates自体に存在しない)
        # →Day2(9/22)。休場日をカウントする特別なロジックは無く、単に
        # _compute_backfill_dayがその日について呼ばれない(=fixtureに存在しない)
        # だけで自然に再現される。
        fixture = {
            0: _entry("2026-09-22", "Q5", price=110.0),  # Day2、休場明けも継続してQ5
            1: _entry("2026-09-18", "Q5", price=100.0),  # Day1
            2: _entry("2026-09-17", "Q5", price=98.0),   # Day0
            3: _entry("2026-09-16", "Q2", price=90.0),
            4: _entry("2026-09-15", "Q2", price=89.0),
            5: _entry("2026-09-14", "Q2", price=88.0),
            6: _entry("2026-09-11", "Q2", price=87.0),
        }
        new_state = self._run_backfill(fixture)
        dates_in_path = [e["date"] for e in new_state["q5_price_path"]]
        self.assertEqual(dates_in_path, ["2026-09-17", "2026-09-18", "2026-09-22"])
        # 暦日では9/17→9/22は5日離れているが、休場日(9/19〜9/21)は取引日として
        # 一切カウントされず、Dayは0→1→2としか進んでいないことを確認する。
        self.assertEqual(len(new_state["q5_price_path"]), 3)


class FindRecentQ5EntryIndexUnitTests(unittest.TestCase):
    """_find_recent_q5_entry_index単体のテスト(Redisアクセスなし、純粋関数)。"""

    def test_entry_in_middle_of_window(self):
        computed = [
            _entry("d1", "Q3", 1), _entry("d2", "Q3", 1),
            _entry("d3", "Q5", 1), _entry("d4", "Q5", 1), _entry("d5", "Q4", 1),
        ]
        self.assertEqual(refresh._find_recent_q5_entry_index(computed, None), 2)

    def test_most_recent_entry_chosen_when_multiple_entries_exist(self):
        computed = [
            _entry("d1", "Q3", 1), _entry("d2", "Q5", 1),  # 最初のエントリー(index1)
            _entry("d3", "Q4", 1),
            _entry("d4", "Q3", 1), _entry("d5", "Q5", 1),  # 2回目のエントリー(index4、こちらを採用)
        ]
        self.assertEqual(refresh._find_recent_q5_entry_index(computed, None), 4)

    def test_no_entry_returns_none(self):
        computed = [_entry("d1", "Q3", 1), _entry("d2", "Q4", 1), _entry("d3", "Q2", 1)]
        self.assertIsNone(refresh._find_recent_q5_entry_index(computed, None))

    def test_oldest_day_q5_with_unknown_predecessor_is_treated_as_entry(self):
        # entry_before_window=None(それ以前が計算できない)の場合、既存の
        # prev_q未取得時の扱い(Q5でない扱い)と同じ慣習に従い、エントリー扱いにする。
        computed = [_entry("d1", "Q5", 1), _entry("d2", "Q5", 1)]
        self.assertEqual(refresh._find_recent_q5_entry_index(computed, None), 0)

    def test_oldest_day_q5_with_known_non_q5_predecessor_is_entry(self):
        computed = [_entry("d1", "Q5", 1)]
        entry_before_window = _entry("d0", "Q3", 1)
        self.assertEqual(refresh._find_recent_q5_entry_index(computed, entry_before_window), 0)

    def test_oldest_day_q5_with_known_q5_predecessor_is_not_entry(self):
        computed = [_entry("d1", "Q5", 1), _entry("d2", "Q4", 1)]
        entry_before_window = _entry("d0", "Q5", 1)
        self.assertIsNone(refresh._find_recent_q5_entry_index(computed, entry_before_window))

    def test_empty_computed_returns_none(self):
        self.assertIsNone(refresh._find_recent_q5_entry_index([], None))


if __name__ == "__main__":
    unittest.main()
