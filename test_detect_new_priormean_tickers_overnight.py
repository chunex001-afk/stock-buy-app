# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-28c、design確認済み)Phase 3:
detect_new_priormean_tickers_overnight.py(24:00 JST締切の深夜バックフィル
専用、新規銘柄検出だけを行う軽量スクリプト)の結合テスト。

Twelve Data・Upstashへは一切アクセスしない(store側の該当関数のみmockで
差し替える)。既存のrefresh_priormean_incremental.py/quintile_logic.py/
q5_universe_logic.py自体はこのテストでは一切変更・mockしない(importも
しない、このスクリプトはそれらに触れない設計のため)。
"""
import unittest
from unittest import mock

import detect_new_priormean_tickers_overnight as job
import redis_store as store


CUTOFF = "2026-10-28T00:00:00+09:00"
STATIC_TICKERS = ["AAPL", "MSFT"]  # 既存56銘柄の代わりの仮の静的銘柄(テスト専用)


class OvernightDetectTestCase(unittest.TestCase):
    def setUp(self):
        self._watchlist = []
        self._backfill_status = {}
        self._pending_backfill = []
        self._added_at = {}

        def fake_load_reference_data():
            return {"events_by_ticker": {t: [] for t in STATIC_TICKERS}}

        def fake_get_watchlist():
            return list(self._watchlist)

        def fake_get_backfill_status(ticker):
            return self._backfill_status.get(ticker)

        def fake_set_backfill_status(ticker, status):
            self._backfill_status[ticker] = status
            return True

        def fake_get_pending_backfill():
            return list(self._pending_backfill)

        def fake_set_pending_backfill(tickers):
            self._pending_backfill = list(tickers)
            return True

        def fake_get_added_at():
            return dict(self._added_at)

        patches = [
            mock.patch.object(job.pml, "load_reference_data", side_effect=fake_load_reference_data),
            mock.patch.object(job.store, "get_watchlist", side_effect=fake_get_watchlist),
            mock.patch.object(job.store, "get_priormean_backfill_status", side_effect=fake_get_backfill_status),
            mock.patch.object(job.store, "set_priormean_backfill_status", side_effect=fake_set_backfill_status),
            mock.patch.object(job.store, "get_priormean_pending_backfill", side_effect=fake_get_pending_backfill),
            mock.patch.object(job.store, "set_priormean_pending_backfill", side_effect=fake_set_pending_backfill),
            mock.patch.object(job.store, "get_priormean_watchlist_added_at", side_effect=fake_get_added_at),
            mock.patch.object(job, "_today_midnight_jst_iso", return_value=CUTOFF),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class EmptyWatchlistTests(OvernightDetectTestCase):
    def test_empty_watchlist_skips_without_touching_redis_writes(self):
        self._watchlist = []
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending_backfill, [])
        self.assertEqual(self._backfill_status, {})


class SingleTickerDetectionTests(OvernightDetectTestCase):
    def test_new_ticker_added_before_cutoff_is_queued(self):
        self._watchlist = ["AAPL", "FORM"]
        self._added_at = {"FORM": "2026-10-27T23:50:00+09:00"}
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending_backfill, ["FORM"])
        self.assertEqual(self._backfill_status["FORM"], "pending")
        self.assertNotIn("AAPL", self._backfill_status)  # 既存56銘柄は対象外

    def test_new_ticker_added_after_cutoff_is_not_queued_yet(self):
        """24:00 JST(=今日0:00 JST)以降に追加された銘柄は今回は検出対象外。"""
        self._watchlist = ["LATECO"]
        self._added_at = {"LATECO": "2026-10-28T00:05:00+09:00"}
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending_backfill, [])
        self.assertNotIn("LATECO", self._backfill_status)

    def test_ticker_without_added_at_record_is_queued(self):
        """この機能導入前からwatchlistに存在していた銘柄(例: 実際のFORM)は
        added_atの記録が無く、締切の対象外として常に検出される。"""
        self._watchlist = ["FORM"]
        self._added_at = {}
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending_backfill, ["FORM"])
        self.assertEqual(self._backfill_status["FORM"], "pending")

    def test_already_done_ticker_is_not_requeued(self):
        self._watchlist = ["FORM"]
        self._added_at = {"FORM": "2026-10-27T10:00:00+09:00"}
        self._backfill_status["FORM"] = "done"
        job.main()
        self.assertEqual(self._pending_backfill, [])

    def test_already_pending_ticker_is_not_duplicated(self):
        self._watchlist = ["FORM"]
        self._added_at = {"FORM": "2026-10-27T10:00:00+09:00"}
        self._backfill_status["FORM"] = "pending"
        self._pending_backfill = ["FORM"]
        job.main()
        self.assertEqual(self._pending_backfill, ["FORM"])  # 重複しない

    def test_failed_ticker_before_cutoff_is_retried(self):
        self._watchlist = ["FORM"]
        self._added_at = {"FORM": "2026-10-27T10:00:00+09:00"}
        self._backfill_status["FORM"] = "failed"
        job.main()
        self.assertEqual(self._pending_backfill, ["FORM"])
        self.assertEqual(self._backfill_status["FORM"], "pending")


class MultipleTickersTests(OvernightDetectTestCase):
    """新規銘柄検出・pending登録自体は1回の実行で複数件処理してよい
    (実際のバックフィル実行run_pending_priormean_backfill.py側の
    「1回1銘柄」制限とは別の話、ユーザー確定仕様)。"""

    def test_multiple_new_tickers_all_queued_in_one_run(self):
        self._watchlist = ["A", "B", "C"]
        self._added_at = {
            "A": "2026-10-27T20:00:00+09:00",
            "B": "2026-10-27T21:00:00+09:00",
            "C": "2026-10-27T22:00:00+09:00",
        }
        rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending_backfill, ["A", "B", "C"])
        for t in ("A", "B", "C"):
            self.assertEqual(self._backfill_status[t], "pending")

    def test_mix_of_before_and_after_cutoff_only_queues_eligible_ones(self):
        self._watchlist = ["EARLY", "LATE"]
        self._added_at = {
            "EARLY": "2026-10-27T23:50:00+09:00",
            "LATE": "2026-10-28T00:10:00+09:00",
        }
        job.main()
        self.assertEqual(self._pending_backfill, ["EARLY"])
        self.assertNotIn("LATE", self._backfill_status)

    def test_existing_pending_queue_is_preserved_and_extended(self):
        self._watchlist = ["NEWCO"]
        self._added_at = {"NEWCO": "2026-10-27T10:00:00+09:00"}
        self._pending_backfill = ["ALREADY_QUEUED"]
        job.main()
        self.assertEqual(self._pending_backfill, ["ALREADY_QUEUED", "NEWCO"])


class DoesNotTouchDay0OrEventResolutionTests(OvernightDetectTestCase):
    """既存のDay0検出・20営業日経過イベント確定(quintile:pool_history/
    quintile:state/priormean:incremental_state)には一切アクセスしない
    ことを確認する(このスクリプトの対象外、priormean-incremental.yml
    〈既存、無変更、08:40 JST実行〉に残す設計)。"""

    def test_does_not_import_or_touch_day0_resolution_state(self):
        import redis_store as store
        self._watchlist = ["FORM"]
        self._added_at = {}
        with mock.patch.object(store, "get_pool_history") as m_pool, \
             mock.patch.object(store, "get_quintile_state") as m_qstate, \
             mock.patch.object(store, "get_priormean_incremental_state") as m_incstate, \
             mock.patch.object(store, "set_priormean_incremental_state") as m_set_incstate:
            rc = job.main()
        self.assertEqual(rc, 0)
        m_pool.assert_not_called()
        m_qstate.assert_not_called()
        m_incstate.assert_not_called()
        m_set_incstate.assert_not_called()


class RedisStoreAddedAtMergeTests(unittest.TestCase):
    """redis_store.set_priormean_watchlist_added_atが読み取り→マージ→
    書き込みで、他の銘柄の記録を上書きしないことの確認(実際のUpstashには
    一切アクセスしない、get_json/set_jsonだけをフェイクに差し替える)。"""

    def setUp(self):
        self.db = {}

        def fake_get_json(key, default=None):
            return self.db.get(key, default)

        def fake_set_json(key, value, ex=None):
            self.db[key] = value
            return True

        patches = [
            mock.patch.object(store, "get_json", side_effect=fake_get_json),
            mock.patch.object(store, "set_json", side_effect=fake_set_json),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_set_merges_without_overwriting_other_tickers(self):
        store.set_priormean_watchlist_added_at("FORM", "2026-10-27T23:50:00+09:00")
        store.set_priormean_watchlist_added_at("NEWCO", "2026-10-28T09:00:00+09:00")
        result = store.get_priormean_watchlist_added_at()
        self.assertEqual(result, {
            "FORM": "2026-10-27T23:50:00+09:00",
            "NEWCO": "2026-10-28T09:00:00+09:00",
        })

    def test_get_returns_empty_dict_when_unset(self):
        self.assertEqual(store.get_priormean_watchlist_added_at(), {})

    def test_ticker_is_uppercased(self):
        store.set_priormean_watchlist_added_at("form", "2026-10-27T23:50:00+09:00")
        result = store.get_priormean_watchlist_added_at()
        self.assertEqual(result, {"FORM": "2026-10-27T23:50:00+09:00"})


if __name__ == "__main__":
    unittest.main()
