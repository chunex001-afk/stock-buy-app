# -*- coding: utf-8 -*-
"""ウォッチリスト再登録時のQ判定ギャップ補完(design 2026-11-02ユーザー確定仕様)
のテスト。CIENで実際に確認された「ウォッチリストから外れていた5取引日分、
quintile:stateが凍結したまま再登録時にも補完されない」挙動の修正を検証する。

Twelve Data・Redisへは一切アクセスしない。refresh._compute_backfill_dayを
target_idxキーの辞書でmockし(test_backfill_q5_entry_recovery.pyと同じ方針)、
9特徴量・KNN・分位境界の実計算を経由せずに「各日のQ/score/bounds/priceが
何であるか」を直接指定する。_backfill_quintile_gap・_update_quintile_state・
_reconcile_price_path・_advance_q5_warning・_update_q5_midline_halt・
app._compute_q5_signal/_compute_q5_progressは本物のコードをそのまま通す。

検証項目(ユーザー確定の7項目に対応):
1. CIENの10/23〜10/29相当の欠落期間を正しく復元できること
2. 削除前のQ5 Day0(10/20)が維持されること
3. Q5 Day・前回Q5からの経過日数・シグナル有効期限が正しく復元されること
4. Q5起点価格と現在価格から計算する騰落率が正しいこと
5. 再登録を複数回行っても履歴が重複しないこと
6. prior_mean関連のRedisキーには一切触れないこと
7. 削除していなかった場合(毎日処理)と同じ最新状態になること(差分テスト)
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import app
import refresh


def _weekday_dates(n, end):
    """end(YYYY-MM-DD)までのn営業日分の日付リスト(土日のみ除外、祝日は
    考慮しない。test_backfill_new_ticker.py等と同じ生成方式)。"""
    end_d = date.fromisoformat(end)
    dates = []
    d = end_d
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d -= timedelta(days=1)
    dates.reverse()
    return dates


# 本テスト全体で使う日付・価格のシナリオ(CIENの実際の本番ログ調査結果と
# 整合する構造: 2026-10-20にQ5突入〈Day0〉、2026-10-21も継続、2026-10-22に
# Q3へ離脱。ここでウォッチリストから削除されたと仮定し、2026-10-23〜10-29の
# 間は一切処理されない。2026-10-30に再登録され、ギャップが補完される)。
# 2026-10-23(金)の次の取引日は週末を挟んで2026-10-26(月)になる。
SCENARIO = [
    ("2026-10-16", "Q3", 10.0, 97.0),
    ("2026-10-19", "Q4", 35.0, 98.0),
    ("2026-10-20", "Q5", 45.0, 100.0),   # Day0
    ("2026-10-21", "Q5", 46.0, 102.0),
    ("2026-10-22", "Q3", 22.0, 95.0),    # Q5離脱、ここでウォッチリストから削除
    ("2026-10-23", "Q3", 23.0, 96.0),    # --- ここから先はウォッチリスト削除中 ---
    ("2026-10-26", "Q4", 32.0, 90.0),
    ("2026-10-27", "Q4", 33.0, 93.0),    # 取引日ベースでDay0から数えてDay5
    ("2026-10-28", "Q3", 24.0, 91.0),
    ("2026-10-29", "Q3", 25.0, 89.0),
    ("2026-10-30", "Q4", 34.0, 94.0),    # 再登録・最新取引日
]
BOUNDS = [10.0, 20.0, 30.0, 40.0]  # Q1/Q2, Q2/Q3, Q3/Q4, Q4/Q5
LAST_DATE = SCENARIO[-1][0]
GAP_START_IDX = 5   # SCENARIO内で2026-10-23に対応するindex(削除期間の開始)
DEPART_DATE = "2026-10-22"


def _build_price_series():
    """SCENARIOの日付・価格を末尾に据え、その前に十分な(200日超の)ダミー
    営業日を付けた日付/終値/出来高シリーズを作る(_compute_backfill_dayは
    mockするため中身は使われないが、closesのsma(20)計算〈q5_midline_halt用〉
    はこの関数のロジックを本物のまま通すため、実数である必要がある)。"""
    scenario_dates = [row[0] for row in SCENARIO]
    pad_n = 220
    pad_dates = _weekday_dates(pad_n, end="2026-10-14")
    dates = pad_dates + scenario_dates
    closes = [100.0 + (i % 11) * 0.1 for i in range(len(pad_dates))] + [row[3] for row in SCENARIO]
    volumes = [1_000_000] * len(dates)
    return dates, closes, volumes


def _entries_by_idx(dates):
    """SCENARIOの各日について、_compute_backfill_dayがそのdates上のindexで
    呼ばれたときに返すべきentry(date/q/score/price/bounds)を、date文字列から
    実際のindexを引いて組み立てる。"""
    date_to_idx = {d: i for i, d in enumerate(dates)}
    by_idx = {}
    for d, q, score, price in SCENARIO:
        idx = date_to_idx[d]
        by_idx[idx] = {"date": d, "q": q, "score": score, "price": price, "bounds": list(BOUNDS)}
    return by_idx


class FakeRedis:
    """quintile:state:<TICKER>だけを保持する最小限のインメモリRedis代替。
    _update_quintile_stateがget_quintile_state→set_quintile_stateで
    読み書きする呼び出しパターンをそのまま再現する(複数回の呼び出し間で
    状態が正しく引き継がれることを検証するため、mock.MagicMockの固定戻り値
    ではなく実際に状態を保持する必要がある)。"""

    def __init__(self):
        self._state = {}

    def get_quintile_state(self, ticker):
        v = self._state.get(ticker.upper())
        return dict(v) if v is not None else None

    def set_quintile_state(self, ticker, data):
        self._state[ticker.upper()] = dict(data)
        return True


class GapBackfillTestBase(unittest.TestCase):
    TICKER = "CIEN"

    def setUp(self):
        self.dates, self.closes, self.volumes = _build_price_series()
        self.trading_calendar = list(self.dates)
        self.by_idx = _entries_by_idx(self.dates)

        self.fake_store = FakeRedis()
        store_patchers = [
            mock.patch.object(refresh.store, "get_quintile_state", side_effect=self.fake_store.get_quintile_state),
            mock.patch.object(refresh.store, "set_quintile_state", side_effect=self.fake_store.set_quintile_state),
        ]
        for p in store_patchers:
            p.start()
            self.addCleanup(p.stop)

        backfill_patcher = mock.patch.object(
            refresh, "_compute_backfill_day",
            side_effect=lambda ticker, dates, closes, volumes, idx, pool_history: self.by_idx.get(idx),
        )
        backfill_patcher.start()
        self.addCleanup(backfill_patcher.stop)

    def _call_update(self, date_str):
        """SCENARIOのdate_strに対応するentryで_update_quintile_stateを1回
        呼ぶ(日次本番バッチが1日分処理するのと同じ呼び出し方)。"""
        idx = self.dates.index(date_str)
        entry = self.by_idx[idx]
        return refresh._update_quintile_state(
            self.TICKER, entry["score"], entry["bounds"], entry["date"],
            price=entry["price"], beta=None,
            trading_calendar=self.trading_calendar,
            dates=self.dates[: idx + 1], closes=self.closes[: idx + 1],
        )

    def _run_sequentially_through(self, date_str):
        """削除が一度も起きなかった場合を再現する: SCENARIOの先頭から
        date_strまで、1日ずつ順番に_update_quintile_stateを呼ぶ。"""
        for d, _q, _score, _price in SCENARIO:
            self._call_update(d)
            if d == date_str:
                break


class Test1GapRestoration(GapBackfillTestBase):
    """検証1: 10/23〜10/29相当の欠落期間を正しく復元できること。"""

    def test_gap_restored_up_to_latest_date(self):
        # 削除前日(10/22)までは通常通り日次処理されていたとする。
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)
        self.assertEqual(pre_state["last_updated"], DEPART_DATE)
        self.assertEqual(pre_state["current_q"], "Q3")

        # ウォッチリストから削除されていた間は何も呼ばれない(=ここでは何もしない)。
        # 再登録時、ギャップ補完を実行する。
        filled, had_issue = refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}},  # _compute_backfill_dayはmock済みのため未使用
            trading_calendar=self.trading_calendar,
        )

        self.assertEqual(filled, 6, "10/23,26,27,28,29,30の6取引日分が補完される")
        self.assertFalse(had_issue)

        final_state = self.fake_store.get_quintile_state(self.TICKER)
        self.assertEqual(final_state["last_updated"], LAST_DATE)
        self.assertEqual(final_state["current_q"], "Q4")
        # 欠落期間中の実際のQ変化(Q3→Q4→Q3→Q4)がhistoryに反映されている。
        tail_dates_q = [(h["date"], h["q"]) for h in final_state["history"][-4:]]
        self.assertEqual(tail_dates_q, [
            ("2026-10-22", "Q3"),
            ("2026-10-26", "Q4"),
            ("2026-10-28", "Q3"),
            ("2026-10-30", "Q4"),
        ])


class Test2Day0Preserved(GapBackfillTestBase):
    """検証2: 削除前のQ5 Day0(2026-10-20)が維持されること。"""

    def test_q5_day0_unchanged_after_gap_fill(self):
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)

        refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}},
            trading_calendar=self.trading_calendar,
        )
        final_state = self.fake_store.get_quintile_state(self.TICKER)

        true_day0 = refresh._true_q5_day0(final_state["history"])
        self.assertEqual(true_day0, "2026-10-20")
        self.assertEqual(final_state["q5_price_path"][0]["date"], "2026-10-20")
        self.assertEqual(final_state["q5_price_path"][0]["price"], 100.0)


class Test3SignalAndDayCount(GapBackfillTestBase):
    """検証3: Q5 Day・前回Q5からの経過日数・シグナル有効期限が正しく復元
    されること(app.py側の表示用関数にそのまま通して検証する)。"""

    def test_signal_days_elapsed_and_day_n(self):
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)
        refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}},
            trading_calendar=self.trading_calendar,
        )
        final_state = self.fake_store.get_quintile_state(self.TICKER)

        signal = app._compute_q5_signal(
            final_state["current_q"], final_state["history"], final_state["last_updated"],
            self.trading_calendar,
        )
        # depart_date=2026-10-22からanchor_date=2026-10-30までの取引日数
        # (10/22,23,26,27,28,29,30)= 7取引日。8取引日未満のためまだ有効。
        self.assertEqual(signal["status"], "active")
        self.assertEqual(signal["days_elapsed"], 7)
        self.assertEqual(signal["day0_date"], "2026-10-21")

        progress = app._compute_q5_progress(final_state["q5_price_path"], current_price=94.0)
        # Day0(10/20)〜Day5到達(10/27)でq5_price_pathが凍結される
        # (MAX_Q5_PRICE_PATH_DAYS=6件: Day0〜Day5)。
        self.assertEqual(progress["days_elapsed"], 5)
        self.assertEqual(progress["path"][-1]["date"] if False else final_state["q5_price_path"][-1]["date"], "2026-10-27")


class Test4ReturnPct(GapBackfillTestBase):
    """検証4: Q5起点価格(Day0)と現在価格から計算する騰落率が正しいこと。"""

    def test_current_return_pct_from_day0(self):
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)
        refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}},
            trading_calendar=self.trading_calendar,
        )
        final_state = self.fake_store.get_quintile_state(self.TICKER)

        day0_price = final_state["q5_price_path"][0]["price"]  # 100.0
        current_price = 94.0
        progress = app._compute_q5_progress(final_state["q5_price_path"], current_price=current_price)
        expected = round((current_price / day0_price - 1) * 100, 2)
        self.assertEqual(progress["current_return_pct"], expected)
        self.assertEqual(progress["current_return_pct"], -6.0)


class Test5NoDuplicationOnRepeatedRequeue(GapBackfillTestBase):
    """検証5: 再登録を複数回行っても履歴が重複しないこと。"""

    def test_two_step_requeue_matches_single_pass(self):
        # 比較対象: 1回の再登録で10/23〜10/30を一括補完した場合。
        self._run_sequentially_through(DEPART_DATE)
        single_pass_store = FakeRedis()
        single_pass_store._state = dict(self.fake_store._state)
        with mock.patch.object(refresh.store, "get_quintile_state", side_effect=single_pass_store.get_quintile_state), \
             mock.patch.object(refresh.store, "set_quintile_state", side_effect=single_pass_store.set_quintile_state):
            refresh._backfill_quintile_gap(
                self.TICKER, DEPART_DATE, self.dates, self.closes, self.volumes,
                pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar,
            )
        single_pass_final = single_pass_store.get_quintile_state(self.TICKER)

        # 実際の対象: 10/23〜10/26で1回目の再登録、10/27〜10/30でさらに
        # 削除→2回目の再登録、という2段階補完。
        filled1, _ = refresh._backfill_quintile_gap(
            self.TICKER, DEPART_DATE, self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar[: self.dates.index("2026-10-26") + 1],
        )
        mid_state = self.fake_store.get_quintile_state(self.TICKER)
        self.assertEqual(mid_state["last_updated"], "2026-10-26")

        filled2, _ = refresh._backfill_quintile_gap(
            self.TICKER, mid_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar,
        )
        two_step_final = self.fake_store.get_quintile_state(self.TICKER)

        self.assertEqual(filled1 + filled2, 6)
        self.assertEqual(two_step_final, single_pass_final, "2段階の再登録でも1回一括補完と同じ最終状態になる")

        # history内に同じ日付のエントリが重複していない。
        seen_dates = [h["date"] for h in two_step_final["history"]]
        self.assertEqual(len(seen_dates), len(set(seen_dates)), "historyに同じ日付の重複エントリが無い")


class Test6PriorMeanUntouched(GapBackfillTestBase):
    """検証6: 再登録のギャップ補完がprior_mean関連のRedisキーに一切触れないこと。"""

    def test_no_priormean_store_calls(self):
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)

        priormean_fns = [
            "get_priormean_events", "set_priormean_events",
            "get_priormean_backfill_status", "set_priormean_backfill_status",
            "get_priormean_pending_backfill", "set_priormean_pending_backfill",
            "get_priormean_bounds_history", "set_priormean_bounds_history",
            "get_priormean_incremental_state", "set_priormean_incremental_state",
        ]
        patchers = [mock.patch.object(refresh.store, name) for name in priormean_fns]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

        refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar,
        )

        for m in mocks:
            m.assert_not_called()


class Test7MatchesNeverRemoved(GapBackfillTestBase):
    """検証7(最重要): 削除していなかった場合(毎日逐次処理)と、削除→再登録時
    一括ギャップ補完した場合で、最終的なquintile:stateが完全に一致すること。
    過去日のQ判定が当時の分位境界(ここではmockで固定したbounds)だけを使い、
    未来のデータを一切参照していないことの裏付けでもある。"""

    def test_final_state_identical_to_never_removed(self):
        # A: 一度も削除されなかった場合(毎日逐次処理)。
        never_removed_store = FakeRedis()
        with mock.patch.object(refresh.store, "get_quintile_state", side_effect=never_removed_store.get_quintile_state), \
             mock.patch.object(refresh.store, "set_quintile_state", side_effect=never_removed_store.set_quintile_state):
            for d, _q, _score, _price in SCENARIO:
                self._call_update(d)
        never_removed_final = never_removed_store.get_quintile_state(self.TICKER)

        # B: 10/22まで処理→削除→10/30に再登録してギャップを一括補完。
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)
        refresh._backfill_quintile_gap(
            self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
            pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar,
        )
        requeued_final = self.fake_store.get_quintile_state(self.TICKER)

        self.assertEqual(requeued_final, never_removed_final)


class Test8NoFutureDataLeak(GapBackfillTestBase):
    """追加検証: _backfill_quintile_gapが各取引日の_update_quintile_state呼び出し
    に渡すdates/closesが、その日までにスライスされている(未来の終値が
    混入していない)ことを直接確認する。"""

    def test_dates_closes_sliced_per_day(self):
        self._run_sequentially_through(DEPART_DATE)
        pre_state = self.fake_store.get_quintile_state(self.TICKER)

        calls = []
        real_update = refresh._update_quintile_state

        def _spy(ticker, score, bounds, date_key, price=None, beta=None,
                 trading_calendar=None, dates=None, closes=None):
            calls.append((date_key, dates[-1] if dates else None, len(dates) if dates else 0))
            return real_update(ticker, score, bounds, date_key, price=price, beta=beta,
                                trading_calendar=trading_calendar, dates=dates, closes=closes)

        with mock.patch.object(refresh, "_update_quintile_state", side_effect=_spy):
            refresh._backfill_quintile_gap(
                self.TICKER, pre_state["last_updated"], self.dates, self.closes, self.volumes,
                pool_history={"dates": [], "scores_by_date": {}}, trading_calendar=self.trading_calendar,
            )

        for date_key, last_date_in_slice, _n in calls:
            self.assertEqual(
                last_date_in_slice, date_key,
                f"{date_key}処理時にdatesの末尾がその日自身ではない(未来データ混入の疑い): {last_date_in_slice}",
            )


class Test9PublicEntryPointWiring(unittest.TestCase):
    """統合テスト: app.add_watchlistが実際に呼ぶ公開関数
    refresh.backfill_quintile_history_for_new_tickerの入口から、
    ギャップ補完が正しく発火することを確認する(_backfill_quintile_gapを
    直接呼ぶ他のテストとは別に、実際の呼び出し経路そのものを検証する)。
    既存テスト(test_backfill_new_ticker.py)のexisting_stateフィクスチャには
    last_updatedが無く新ロジックが発火しないため、ここでは本番と同じ形
    (last_updated付き)のexisting_stateを使う。"""

    def setUp(self):
        self.dates, self.closes, self.volumes = _build_price_series()
        self.trading_calendar = list(self.dates)
        self.by_idx = _entries_by_idx(self.dates)

        budget_patcher = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)

        backfill_patcher = mock.patch.object(
            refresh, "_compute_backfill_day",
            side_effect=lambda ticker, dates, closes, volumes, idx, pool_history: self.by_idx.get(idx),
        )
        backfill_patcher.start()
        self.addCleanup(backfill_patcher.stop)

        fetch_patcher = mock.patch.object(
            refresh, "_td_fetch_one",
            return_value=((self.dates, self.closes, self.volumes), False, None, None, None),
        )
        fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

        # 削除前(2026-10-22)まで正常に処理されていた、本番と同じ形のstate
        # (last_updated付き)。q5_price_pathもDay0〜Day2まで実在する想定。
        self.existing_state = {
            "current_q": "Q3",
            "previous_q": "Q5",
            "pred_score": 22.0,
            "history": [
                {"date": "2026-10-20", "q": "Q5"},
                {"date": "2026-10-22", "q": "Q3"},
            ],
            "q5_price_path": [
                {"date": "2026-10-20", "price": 100.0},
                {"date": "2026-10-21", "price": 102.0},
                {"date": "2026-10-22", "price": 95.0},
            ],
            "q5_warning": None,
            "q5_midline_halt": None,
            "last_updated": DEPART_DATE,
        }

        self.fake_store = FakeRedis()
        self.fake_store._state["CIEN"] = dict(self.existing_state)
        patchers = [
            mock.patch.object(refresh.store, "get_quintile_state", side_effect=self.fake_store.get_quintile_state),
            mock.patch.object(refresh.store, "set_quintile_state", side_effect=self.fake_store.set_quintile_state),
            mock.patch.object(refresh.store, "get_ticker_record", return_value={"last_trade_date": "2026-10-22"}),
            mock.patch.object(refresh.store, "set_ticker_record"),
            mock.patch.object(
                refresh.store, "get_pool_history",
                return_value={
                    "dates": self.trading_calendar,
                    "scores_by_date": {d: [10.0, 20.0, 30.0, 40.0, 50.0] for d in self.trading_calendar},
                },
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_gap_is_backfilled_through_public_entry_point(self):
        result = refresh.backfill_quintile_history_for_new_ticker("CIEN", "dummy-api-key")

        self.assertTrue(result["ok"])
        self.assertIsNone(result["message"], "全日補完できた場合はmessage=None(既存の成功時表示ロジックに委ねる)")

        final_state = self.fake_store.get_quintile_state("CIEN")
        self.assertEqual(final_state["last_updated"], LAST_DATE)
        self.assertEqual(final_state["current_q"], "Q4")
        self.assertEqual(
            [h["date"] for h in final_state["history"]][-4:],
            ["2026-10-22", "2026-10-26", "2026-10-28", "2026-10-30"],
        )

        signal = app._compute_q5_signal(
            final_state["current_q"], final_state["history"], final_state["last_updated"],
            self.trading_calendar,
        )
        self.assertEqual(signal["status"], "active")
        self.assertEqual(signal["days_elapsed"], 7)

    def test_data_insufficient_gap_is_reported_not_guessed(self):
        """ギャップの一部で_compute_backfill_dayがNone(データ不足)を返す場合、
        その日は推測で埋めず、呼び出し元に「補完できなかった」ことを伝える。"""
        incomplete_by_idx = dict(self.by_idx)
        missing_idx = self.dates.index("2026-10-26")
        del incomplete_by_idx[missing_idx]
        with mock.patch.object(
            refresh, "_compute_backfill_day",
            side_effect=lambda ticker, dates, closes, volumes, idx, pool_history: incomplete_by_idx.get(idx),
        ):
            result = refresh.backfill_quintile_history_for_new_ticker("CIEN", "dummy-api-key")

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["message"])
        self.assertIn("補完できませんでした", result["message"])

        final_state = self.fake_store.get_quintile_state("CIEN")
        # 10/26は推測されず、historyに架空のエントリは増えていない
        # (10/22のQ3のまま10/27のQ4エントリへ直接つながる = 10/26単独のエントリは無い)。
        self.assertNotIn("2026-10-26", [h["date"] for h in final_state["history"]])


if __name__ == "__main__":
    unittest.main()
