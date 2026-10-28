# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-28d、design確認済み)Phase 4:
即時dispatchバックフィル(backfill_dispatched_ticker.py)と、
backfill_one_ticker冒頭に追加したRedis原子的ロック(二重起動防止)の
結合テスト。

Twelve Data・Upstashへは一切アクセスしない。重い実計算(KNN scoring)は
compute_ticker_scores/load_price_seriesをモックで差し替えて高速化する
(ロック機構自体の検証が主眼であり、Day0計算の正しさはtest_priormean_
incremental_logic.py/test_backfill_new_ticker_prior_mean.py側で別途
FORM実データで検証済み)。
"""
import threading
import time
import unittest
from unittest import mock

import backfill_dispatched_ticker as driver
import backfill_new_ticker_prior_mean as backfill_script
import redis_store as store


class _FakeRedisBackend:
    """get_json/set_jsonだけを差し替えるインメモリのフェイクバックエンド
    (他の既存テストと同じ手法)。set_nxは別途スレッドセーフに実装する。"""

    def __init__(self, bounds_history=None):
        self.db = {"priormean:bounds_history": bounds_history or {"2024-01-01": [0.0, 0.0, 0.0, 0.0]}}
        self._mutex = threading.Lock()

    def get_json(self, key, default=None):
        with self._mutex:
            return self.db.get(key, default)

    def set_json(self, key, value, ex=None):
        with self._mutex:
            self.db[key] = value
            return True

    def set_nx(self, key, value, ex=None):
        """Upstashの実際のSET NXと同じ契約(未設定時のみ成功)を、
        threading.Lockで原子的に再現する。"""
        with self._mutex:
            if key in self.db:
                return False
            self.db[key] = value
            return True


def _patched(fake, extra=()):
    """backfill_one_ticker呼び出しに必要な一連のmock.patchを返す。"""
    patches = [
        mock.patch.object(store, "get_json", side_effect=fake.get_json),
        mock.patch.object(store, "set_json", side_effect=fake.set_json),
        mock.patch.object(store, "set_nx", side_effect=fake.set_nx),
        mock.patch.object(backfill_script, "load_price_series", return_value=(["2024-01-01"], [10.0], [100])),
    ]
    return list(patches) + list(extra)


class BackfillLockIntegrationTests(unittest.TestCase):
    """backfill_one_ticker冒頭のRedis原子的ロック(design 2026-10-28d)の
    結合テスト。実際のbackfill_one_ticker(無変更の外側ロジック+追加した
    ロックだけ)を、高速なフェイクデータで動かして検証する。"""

    def _counter(self):
        return {"n": 0, "lock": threading.Lock()}

    def _scoring_side_effect(self, counter, delay=0.0):
        def _side_effect(*args, **kwargs):
            with counter["lock"]:
                counter["n"] += 1
            if delay:
                time.sleep(delay)
            return {}
        return _side_effect

    # --- 1回dispatch ---
    def test_single_call_runs_once(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO")
        self.assertFalse(outcome["skipped"])
        self.assertEqual(counter["n"], 1)
        self.assertEqual(fake.get_json("priormean:backfill_status:FOO"), "done")

    # --- 連続2回(1回目完了後) ---
    def test_sequential_duplicate_after_completion_skips(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            r1 = backfill_script.backfill_one_ticker("FOO")
            r2 = backfill_script.backfill_one_ticker("FOO")
        self.assertFalse(r1["skipped"])
        self.assertTrue(r2["skipped"])
        self.assertEqual(r2["reason"], "already_done")
        self.assertEqual(counter["n"], 1, "Twelve Data取得/スコア計算は1回だけのはず")

    # --- ほぼ同時に2回(スレッド競合) ---
    def test_near_simultaneous_calls_only_one_runs(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        results = []
        results_lock = threading.Lock()

        def worker():
            r = backfill_script.backfill_one_ticker("FOO")
            with results_lock:
                results.append(r)

        # mock.patchはモジュール属性をグローバルに差し替えるため、1つの
        # with(メインスレッド)で一度だけ適用し、その内側で複数スレッドを
        # 起動する(スレッドごとに別々のpatcherを開始すると競合して壊れる)。
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter, delay=0.05)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(counter["n"], 1, "同じtickerのTwelve Data取得/スコア計算が2重に走ってはいけない")
        skipped = [r for r in results if r["skipped"]]
        ran = [r for r in results if not r["skipped"]]
        self.assertEqual(len(ran), 1)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "locked")

    # --- 10並列(cron+dispatchの同時実行を想定した、より厳しい競合) ---
    def test_ten_way_parallel_calls_only_one_runs(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        results = []
        results_lock = threading.Lock()

        def worker():
            r = backfill_script.backfill_one_ticker("FOO")
            with results_lock:
                results.append(r)

        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter, delay=0.05)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(counter["n"], 1, "10並列(cron+dispatch競合を想定)でもTwelve Data取得は1回だけのはず")
        self.assertEqual(sum(1 for r in results if not r["skipped"]), 1)
        self.assertEqual(sum(1 for r in results if r.get("reason") == "locked"), 9)

    # --- pending状態でdispatch: 通常通り実行されるべき ---
    def test_pending_status_proceeds_normally(self):
        fake = _FakeRedisBackend()
        fake.set_json("priormean:backfill_status:FOO", "pending")
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO")
        self.assertFalse(outcome["skipped"])
        self.assertEqual(counter["n"], 1)

    # --- in_progress状態+ロック保持中でdispatch: スキップされるべき ---
    def test_in_progress_with_held_lock_is_skipped(self):
        fake = _FakeRedisBackend()
        fake.set_json("priormean:backfill_status:FOO", "in_progress")
        fake.set_nx("priormean:backfill_lock:FOO", "1")  # 別プロセスが既に保持中を再現
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO")
        self.assertTrue(outcome["skipped"])
        self.assertEqual(outcome["reason"], "locked")
        self.assertEqual(counter["n"], 0, "ロック保持中はTwelve Data取得に到達してはいけない")

    # --- done状態でdispatch: ロック取得すら試みずスキップ ---
    def test_done_status_skips_before_lock_attempt(self):
        fake = _FakeRedisBackend()
        fake.set_json("priormean:backfill_status:FOO", "done")
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO")
        self.assertTrue(outcome["skipped"])
        self.assertEqual(outcome["reason"], "already_done")
        self.assertEqual(counter["n"], 0)
        self.assertNotIn("priormean:backfill_lock:FOO", fake.db, "doneの場合、ロックすら取得しないはず")

    # --- failed状態でdispatch: 再試行として実行されるべき ---
    def test_failed_status_retries_normally(self):
        fake = _FakeRedisBackend()
        fake.set_json("priormean:backfill_status:FOO", "failed")
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO")
        self.assertFalse(outcome["skipped"])
        self.assertEqual(counter["n"], 1)
        self.assertEqual(fake.get_json("priormean:backfill_status:FOO"), "done")

    # --- 別tickerは互いにブロックしない ---
    def test_different_tickers_do_not_block_each_other(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            r_a = backfill_script.backfill_one_ticker("AAA")
            r_b = backfill_script.backfill_one_ticker("BBB")
        self.assertFalse(r_a["skipped"])
        self.assertFalse(r_b["skipped"])
        self.assertEqual(counter["n"], 2)

    # --- dry_run時はロック取得自体を行わない(既存方針の維持確認) ---
    def test_dry_run_does_not_attempt_lock(self):
        fake = _FakeRedisBackend()
        counter = self._counter()
        patches = _patched(fake, [
            mock.patch.object(backfill_script, "compute_ticker_scores",
                               side_effect=self._scoring_side_effect(counter)),
        ])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            outcome = backfill_script.backfill_one_ticker("FOO", dry_run=True)
        self.assertFalse(outcome["skipped"])
        self.assertNotIn("priormean:backfill_lock:FOO", fake.db)
        self.assertNotIn("priormean:backfill_status:FOO", fake.db, "dry_run時はstatusも一切書き込まないはず(既存方針)")


class DispatchedTickerDriverTests(unittest.TestCase):
    """backfill_dispatched_ticker.main()(新規の薄いドライバー)のテスト。"""

    def test_missing_ticker_argument_returns_error_code(self):
        with mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py"]):
            rc = driver.main()
        self.assertEqual(rc, 2)

    def test_successful_outcome_returns_zero(self):
        fake_outcome = {
            "skipped": False, "ticker": "FOO", "elapsed": 1.2,
            "result": {"day0_count": 3, "resolved_events": [1, 2], "unresolved_day0_dates": [3]},
        }
        with mock.patch.object(driver.backfill_module, "backfill_one_ticker", return_value=fake_outcome), \
             mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py", "foo"]):
            rc = driver.main()
        self.assertEqual(rc, 0)

    def test_skipped_outcome_returns_zero(self):
        fake_outcome = {"skipped": True, "reason": "locked", "ticker": "FOO"}
        with mock.patch.object(driver.backfill_module, "backfill_one_ticker", return_value=fake_outcome), \
             mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py", "FOO"]):
            rc = driver.main()
        self.assertEqual(rc, 0)

    def test_unexpected_exception_sets_failed_status_and_returns_one(self):
        with mock.patch.object(driver.backfill_module, "backfill_one_ticker", side_effect=RuntimeError("boom")), \
             mock.patch.object(driver.store, "set_priormean_backfill_status") as m_set, \
             mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py", "FOO"]):
            rc = driver.main()
        self.assertEqual(rc, 1)
        m_set.assert_called_once_with("FOO", "failed")

    def test_file_not_found_sets_failed_status_and_returns_one(self):
        with mock.patch.object(driver.backfill_module, "backfill_one_ticker",
                                side_effect=FileNotFoundError("no price cache")), \
             mock.patch.object(driver.store, "set_priormean_backfill_status") as m_set, \
             mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py", "FOO"]):
            rc = driver.main()
        self.assertEqual(rc, 1)
        m_set.assert_called_once_with("FOO", "failed")

    def test_ticker_is_uppercased(self):
        fake_outcome = {"skipped": True, "reason": "already_done", "ticker": "FOO"}
        with mock.patch.object(driver.backfill_module, "backfill_one_ticker",
                                return_value=fake_outcome) as m_backfill, \
             mock.patch.object(driver.sys, "argv", ["backfill_dispatched_ticker.py", "foo"]):
            driver.main()
        m_backfill.assert_called_once_with("FOO")


if __name__ == "__main__":
    unittest.main()
