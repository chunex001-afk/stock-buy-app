# -*- coding: utf-8 -*-
"""Q5 48銘柄の将来成績をforward-onlyで蓄積する専用バッチ(design 2026-10-21、
refresh_q5_universe.py)の結合テスト。

Twelve Data・Upstashへは一切アクセスしない(store/refresh側の該当関数のみ
mockで差し替える)。既存のrefresh.py・app.pyのロジック自体はこのテストでは
一切変更・呼び出ししない(refresh.remaining_td_budget/_td_fetch_oneのみ
mock経由で参照する)。

day5/20/60の遡及補完(design 2026-10-21修正: 「今日の価格で代用」せず、
各tickerの当日取得分に含まれる日付→終値から本来のチェックポイント日
そのものの終値を探す)を検証するため、fake_td_fetch_oneは実際のCAL
(共通の取引日カレンダー)に正確に揃った日付配列を返す(過去のテストに
あった「別起点のダミー日付で末尾だけ差し替える」簡易実装は、日付単位の
厳密な遡及検索と整合しないため廃止した)。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import quintile_logic
import refresh_q5_universe as job


def _business_days(n, start="2025-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(400)
UNIVERSE = quintile_logic.ROTATION_UNIVERSE
BOUNDS_SCORE = 999.0  # bounds=[10,20,30,40]に対し確実にQ5側になる値

# MIN_HISTORY_FOR_FULL_FEATURES(=200)を必ず満たすよう、テストの「今日」は
# このOFFSET以降のCALインデックスを使う(CAL[:OFFSET]の長さ=220>=200)。
OFFSET = 220


class Q5UniverseRefreshTestCase(unittest.TestCase):
    def setUp(self):
        self._pool_history = {"dates": list(CAL), "scores_by_date": {d: [10.0, 20.0, 30.0, 40.0, 50.0] for d in CAL}}
        self._state = None
        self._fetch_calls = []
        self._score_by_ticker = {t: 15.0 for t in UNIVERSE}  # デフォルトQ2相当
        self._price_by_ticker = {t: 100.0 for t in UNIVERSE}  # 「今日」の終値
        # ticker別に、特定の過去日付の終値だけを個別指定したい場合に使う
        # ({ticker: {"YYYY-MM-DD": price}})。遡及補完テスト専用。
        self._historical_overrides = {}
        # ticker別に、当日取得分の日付配列から特定の日付を欠落させたい場合
        # に使う({ticker: {"YYYY-MM-DD",...}})。「対象日が履歴に存在しない」
        # ケースの再現専用。
        self._missing_dates = {}
        self._fail_tickers = set()

        def fake_get_pool_history():
            return self._pool_history

        def fake_get_state():
            return self._state

        def fake_set_state(data):
            self._state = data
            return True

        def fake_remaining_td_budget():
            return 700, 0

        def fake_td_fetch_one(ticker, api_key, date_key):
            self._fetch_calls.append(ticker)
            if ticker in self._fail_tickers:
                return None, False, "NO_DATA", "テスト用の疑似失敗", None

            last_date = self._pool_history["dates"][-1]
            idx = CAL.index(last_date)
            dates = list(CAL[: idx + 1])
            closes = [100.0] * (len(dates) - 1) + [self._price_by_ticker[ticker]]

            for d, price in self._historical_overrides.get(ticker, {}).items():
                if d in dates:
                    closes[dates.index(d)] = price

            drop = self._missing_dates.get(ticker, set())
            if drop:
                kept = [(d, c) for d, c in zip(dates, closes) if d not in drop]
                dates = [d for d, _ in kept]
                closes = [c for _, c in kept]

            volumes = [1_000_000] * len(dates)
            return (dates, closes, volumes), False, None, None, None

        def fake_compute_features(dates, closes, volumes, spy_dates=None, spy_closes=None):
            return {"_last_close": closes[-1]}

        def fake_knn_predict_score(raw_features, reference=None, k=100):
            # fake_td_fetch_oneが呼ばれた直後、直列にcompute_features→
            # knn_predict_scoreが呼ばれる(スクリプトはforループで1銘柄ずつ
            # 処理するため、_fetch_calls[-1]が「今まさに処理中のticker」)。
            ticker = self._fetch_calls[-1]
            return self._score_by_ticker[ticker]

        patches = [
            mock.patch.object(job.store, "get_pool_history", side_effect=fake_get_pool_history),
            mock.patch.object(job.store, "get_q5universe_state", side_effect=fake_get_state),
            mock.patch.object(job.store, "set_q5universe_state", side_effect=fake_set_state),
            mock.patch.object(job.refresh, "remaining_td_budget", side_effect=fake_remaining_td_budget),
            mock.patch.object(job.refresh, "_td_fetch_one", side_effect=fake_td_fetch_one),
            mock.patch.object(job.quintile_logic, "compute_features", side_effect=fake_compute_features),
            mock.patch.object(job.quintile_logic, "knn_predict_score", side_effect=fake_knn_predict_score),
            mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "test-key"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _entries(self):
        return (self._state or {}).get("entries") or []

    def _prev_q(self):
        return (self._state or {}).get("prev_q") or {}

    def _amd_entry(self):
        return [e for e in self._entries() if e["ticker"] == "AMD"][0]

    def _set_today(self, idx):
        self._pool_history["dates"] = CAL[: idx + 1]

    def _latest_price(self):
        return (self._state or {}).get("latest_price") or {}

    def _latest_price_date(self):
        return (self._state or {}).get("latest_price_date")


class SkipWithoutApiCallTests(Q5UniverseRefreshTestCase):
    """10: pool_historyが今日まで更新されていなければAPIを呼ばず終了。"""

    def test_10a_missing_pool_history_skips_without_any_api_call(self):
        self._pool_history = None
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fetch_calls, [])
        self.assertIsNone(self._state)

    def test_10b_already_processed_date_skips_without_any_api_call(self):
        self._state = {"prev_q": {t: "Q2" for t in UNIVERSE}, "entries": [],
                        "last_processed_date": self._pool_history["dates"][-1]}
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fetch_calls, [])  # Twelve Dataを一切呼んでいない


class DuplicateRunPreventionTests(Q5UniverseRefreshTestCase):
    """11: 同じ日に2回実行しても重複Day0を作らない。"""

    def test_11_running_main_twice_same_day_does_not_duplicate_day0(self):
        self._set_today(OFFSET)
        job.main()  # 事前にprev_qを確立(1回目はDay0を作らない仕様のため)

        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        rc1 = job.main()
        self.assertEqual(rc1, 0)
        calls_after_first = len(self._fetch_calls)
        self.assertEqual(len([e for e in self._entries() if e["ticker"] == "AMD"]), 1)

        rc2 = job.main()  # 同じpool_historyでもう一度実行(cron取りこぼし等の想定)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self._fetch_calls), calls_after_first)  # 2回目はAPIを呼んでいない
        self.assertEqual(len([e for e in self._entries() if e["ticker"] == "AMD"]), 1)  # 重複していない


class CompletenessGateTests(Q5UniverseRefreshTestCase):
    """9: 一部銘柄取得失敗時は確定更新しない。"""

    def test_9_single_ticker_failure_aborts_without_any_write(self):
        self._set_today(OFFSET)
        self._fail_tickers = {UNIVERSE[5]}
        rc = job.main()
        self.assertEqual(rc, 1)
        self.assertIsNone(self._state)  # set_q5universe_stateは一度も呼ばれていない

    def test_9b_failure_does_not_partially_record_successful_tickers(self):
        self._set_today(OFFSET)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        self._fail_tickers = {UNIVERSE[-1]}
        rc = job.main()
        self.assertEqual(rc, 1)
        self.assertIsNone(self._state)
        self.assertEqual(self._entries(), [])

    def test_13_partial_failure_does_not_write_latest_price(self):
        """13: 失敗時に部分的なlatest_priceが保存されないことの確認。
        1回目は正常に成功させてlatest_priceを確定し、2回目に1銘柄だけ
        失敗させても、1回目のlatest_priceが変化しないことを確認する。"""
        self._set_today(OFFSET)
        rc1 = job.main()
        self.assertEqual(rc1, 0)
        latest_price_after_success = dict(self._latest_price())
        self.assertEqual(len(latest_price_after_success), len(UNIVERSE))

        self._set_today(OFFSET + 1)
        self._price_by_ticker["AMD"] = 999.0  # 失敗時にこの値が紛れ込まないか確認用
        self._fail_tickers = {UNIVERSE[3]}
        rc2 = job.main()
        self.assertEqual(rc2, 1)
        self.assertEqual(self._latest_price(), latest_price_after_success)  # 変化していない


class LatestPriceForMomentumTests(Q5UniverseRefreshTestCase):
    """Q5直近勢い(design 2026-10-21)用のlatest_price/latest_price_dateが、
    既存のprices_by_ticker(day0価格計算用)をそのまま再利用して保存される
    ことの確認。"""

    def test_latest_price_contains_all_48_tickers_with_closing_prices(self):
        self._set_today(OFFSET)
        self._price_by_ticker["AMD"] = 123.45
        rc = job.main()
        self.assertEqual(rc, 0)
        latest_price = self._latest_price()
        self.assertEqual(len(latest_price), len(UNIVERSE))
        self.assertEqual(latest_price["AMD"], 123.45)

    def test_latest_price_date_matches_processed_date(self):
        self._set_today(OFFSET)
        job.main()
        self.assertEqual(self._latest_price_date(), CAL[OFFSET])

    def test_latest_price_updates_on_subsequent_successful_runs(self):
        self._set_today(OFFSET)
        job.main()
        self._set_today(OFFSET + 1)
        self._price_by_ticker["AMD"] = 200.0
        job.main()
        self.assertEqual(self._latest_price()["AMD"], 200.0)
        self.assertEqual(self._latest_price_date(), CAL[OFFSET + 1])

    def test_latest_price_does_not_add_fields_to_entries(self):
        """entries自体には新しいフィールドを追加しない(既存の確定成績
        ロジック・既存entriesの構造は無変更)ことの確認。"""
        self._set_today(OFFSET)
        job.main()
        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        job.main()
        entry = self._amd_entry()
        self.assertEqual(
            set(entry.keys()),
            {"ticker", "day0_date", "day0_price", "day5_date", "day5_price",
             "day20_date", "day20_price", "day60_date", "day60_price"},
        )


class Day0EndToEndTests(Q5UniverseRefreshTestCase):
    def test_first_successful_run_never_creates_day0(self):
        """1: 初回実行(prev_qが保存されていない)ではDay0を作らない。"""
        self._set_today(OFFSET)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE  # 今日Q5だが、前回Qが無い
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._entries(), [])
        self.assertEqual(self._prev_q()["AMD"], "Q5")  # prev_qは記録される(次回以降の起点)

    def test_second_run_q4_to_q5_creates_day0(self):
        """2: 前回Q4→今日Q5でDay0を作る。"""
        self._set_today(OFFSET)
        job.main()  # 1回目: prev_qを確定(AMD=Q2)

        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE  # 2回目: Q2->Q5
        rc = job.main()
        self.assertEqual(rc, 0)
        amd_entries = [e for e in self._entries() if e["ticker"] == "AMD"]
        self.assertEqual(len(amd_entries), 1)
        self.assertEqual(amd_entries[0]["day0_date"], CAL[OFFSET + 1])


class CheckpointOnTimeTests(Q5UniverseRefreshTestCase):
    """5〜7 + 新規1: day5/20/60が、ジョブがちょうどその営業日に実行できた
    場合に正しく記録されること(基本ケース、「当日取得できた場合」)。"""

    def _establish_day0(self):
        self._set_today(OFFSET)
        job.main()  # prev_q確定(AMD=Q2)
        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        self._price_by_ticker["AMD"] = 100.0
        job.main()  # Day0
        return OFFSET + 1

    def test_5_day5_recorded_on_the_exact_day(self):
        day0_idx = self._establish_day0()
        self._set_today(day0_idx + 5)
        self._price_by_ticker["AMD"] = 110.0
        job.main()
        entry = self._amd_entry()
        self.assertEqual(entry["day5_date"], CAL[day0_idx + 5])
        self.assertEqual(entry["day5_price"], 110.0)

    def test_6_day20_recorded_on_the_exact_day(self):
        day0_idx = self._establish_day0()
        self._set_today(day0_idx + 20)
        self._price_by_ticker["AMD"] = 130.0
        job.main()
        entry = self._amd_entry()
        self.assertEqual(entry["day20_date"], CAL[day0_idx + 20])
        self.assertEqual(entry["day20_price"], 130.0)

    def test_7_day60_recorded_on_the_exact_day(self):
        day0_idx = self._establish_day0()
        self._set_today(day0_idx + 60)
        self._price_by_ticker["AMD"] = 150.0
        job.main()
        entry = self._amd_entry()
        self.assertEqual(entry["day60_date"], CAL[day0_idx + 60])
        self.assertEqual(entry["day60_price"], 150.0)

    def test_8_q5_departure_does_not_stop_checkpoint_recording(self):
        day0_idx = self._establish_day0()
        self._set_today(day0_idx + 20)
        self._score_by_ticker["AMD"] = 15.0  # Q5から離脱済み
        self._price_by_ticker["AMD"] = 70.0
        job.main()
        self.assertEqual(self._amd_entry()["day20_price"], 70.0)  # 離脱後も記録される


class CheckpointCatchUpTests(Q5UniverseRefreshTestCase):
    """新規2〜4: ジョブがチェックポイント日にちょうど実行できず、後日に
    復旧した場合の遡及補完(design 2026-10-21修正の核心部分)。"""

    def _establish_day0(self):
        self._set_today(OFFSET)
        job.main()
        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        self._price_by_ticker["AMD"] = 100.0
        job.main()
        return OFFSET + 1

    def test_2_job_fails_exactly_on_checkpoint_day_recovers_next_day(self):
        """day0+20営業日目にジョブが失敗(=その日は実行されない)し、
        day0+21営業日目に復旧した場合でも、day20には「day0+21の価格」
        ではなく「day0+20の実際の終値」が記録されること。"""
        day0_idx = self._establish_day0()
        target_date = CAL[day0_idx + 20]
        later_date = CAL[day0_idx + 21]

        # day0+20の実際の終値を130.0、復旧日(day0+21)自身の終値は999.0とし、
        # 「代用」と「遡及補完」が混同されていないことを明確に区別する。
        self._historical_overrides["AMD"] = {target_date: 130.0}
        self._price_by_ticker["AMD"] = 999.0  # day0+21自身(=closes[-1])の終値
        self._score_by_ticker["AMD"] = 15.0

        self._set_today(day0_idx + 21)  # day0+20はスキップされ、+21で初回復旧
        job.main()

        entry = self._amd_entry()
        self.assertEqual(entry["day20_date"], target_date)  # 本来のチェックポイント日
        self.assertEqual(entry["day20_price"], 130.0)  # 「今日の価格」999.0ではない

    def test_3_target_date_found_in_fetched_history_is_used_as_is(self):
        """過去OHLCに対象日(day0+5)が存在する場合、その終値がそのまま
        使われること(基本的な遡及検索の成功ケース)。"""
        day0_idx = self._establish_day0()
        target_date = CAL[day0_idx + 5]
        self._historical_overrides["AMD"] = {target_date: 111.11}
        self._price_by_ticker["AMD"] = 222.22
        self._set_today(day0_idx + 6)  # day0+5をスキップし、+6で初回復旧
        job.main()
        entry = self._amd_entry()
        self.assertEqual(entry["day5_date"], target_date)
        self.assertEqual(entry["day5_price"], 111.11)

    def test_4_target_date_missing_from_fetched_history_stays_null(self):
        """対象日(day0+20)が取得した過去OHLCの範囲にも存在しない場合は、
        無理に埋めずnullのままにする(「取れない場合だけnullのまま」)。"""
        day0_idx = self._establish_day0()
        target_date = CAL[day0_idx + 20]
        self._missing_dates["AMD"] = {target_date}  # 当日の取得結果からこの日だけ欠落させる
        self._price_by_ticker["AMD"] = 999.0
        self._set_today(day0_idx + 21)
        rc = job.main()
        self.assertEqual(rc, 0)  # 48銘柄全体の取得自体は成功(このticker自体のfetchは失敗していない)
        entry = self._amd_entry()
        self.assertIsNone(entry["day20_date"])
        self.assertIsNone(entry["day20_price"])

        # 次回、対象日が見えるようになれば(通常はdata gapが解消される想定)、
        # 正しく埋まることの確認(再試行できることの確認)。
        self._missing_dates["AMD"] = set()
        self._set_today(day0_idx + 22)
        job.main()
        entry = self._amd_entry()
        self.assertEqual(entry["day20_date"], target_date)
        # target_dateはもう「今日」ではなく履歴中の1点になっており、
        # override未設定のため汎用の過去値(100.0)がそのまま入っている。
        self.assertEqual(entry["day20_price"], 100.0)


class Day0PriceImmutabilityTests(Q5UniverseRefreshTestCase):
    """新規5 + 13: Day0価格が後から変更されないこと(look-ahead防止の
    核心、複数回のmain()呼び出しをまたいで確認する)。"""

    def test_5_day0_price_never_changes_across_later_runs(self):
        self._set_today(OFFSET)
        job.main()

        self._set_today(OFFSET + 1)
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        self._price_by_ticker["AMD"] = 200.0
        job.main()
        day0_price_snapshot = self._amd_entry()["day0_price"]
        self.assertEqual(day0_price_snapshot, 200.0)

        for extra_days, fake_price in [(5, 9999.0), (20, 1.0), (60, 555.0)]:
            self._set_today(OFFSET + 1 + extra_days)
            self._price_by_ticker["AMD"] = fake_price
            self._score_by_ticker["AMD"] = 15.0
            job.main()
            self.assertEqual(self._amd_entry()["day0_price"], day0_price_snapshot)


class OtherEntriesUntouchedTests(Q5UniverseRefreshTestCase):
    def test_entries_for_other_tickers_unaffected_by_unrelated_day0(self):
        self._set_today(OFFSET)
        job.main()
        self._score_by_ticker["AMD"] = BOUNDS_SCORE
        self._score_by_ticker["NVDA"] = BOUNDS_SCORE
        self._set_today(OFFSET + 1)
        job.main()
        tickers_with_entries = sorted({e["ticker"] for e in self._entries()})
        self.assertEqual(tickers_with_entries, ["AMD", "NVDA"])


if __name__ == "__main__":
    unittest.main()
