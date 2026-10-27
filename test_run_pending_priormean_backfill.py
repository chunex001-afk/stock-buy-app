# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 2:
run_pending_priormean_backfill.py(priormean:pending_backfillキューから
新規銘柄を1件だけ取り出してバックフィルするワークフロー専用スクリプト)の
結合テスト。

backfill_new_ticker_prior_mean.backfill_one_ticker自体(Phase 1、無変更)は
重い処理(実際の価格データ読み込み・スコア計算)を含むため、ここではmockで
差し替える。このテストの目的はbackfill_one_ticker自身の正しさではなく、
「キューから何件取り出すか」「失敗時にキューからどう扱うか」という
run_pending_priormean_backfill.py自身のオーケストレーション仕様を確認する
こと。
"""
import unittest
from unittest import mock

import run_pending_priormean_backfill as job


class RunPendingBackfillTestCase(unittest.TestCase):
    def setUp(self):
        self._pending = []
        self._status = {}

        def fake_get_pending():
            return list(self._pending)

        def fake_set_pending(tickers):
            self._pending = list(tickers)
            return True

        def fake_set_status(ticker, status):
            self._status[ticker] = status
            return True

        patches = [
            mock.patch.object(job.store, "get_priormean_pending_backfill", side_effect=fake_get_pending),
            mock.patch.object(job.store, "set_priormean_pending_backfill", side_effect=fake_set_pending),
            mock.patch.object(job.store, "set_priormean_backfill_status", side_effect=fake_set_status),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class EmptyQueueTests(RunPendingBackfillTestCase):
    def test_empty_queue_skips_without_calling_backfill(self):
        self._pending = []
        with mock.patch.object(job.backfill_module, "backfill_one_ticker") as m:
            rc = job.main()
        self.assertEqual(rc, 0)
        m.assert_not_called()


class SingleTickerPerRunTests(RunPendingBackfillTestCase):
    """1回の実行で処理するのは最大1銘柄のみ(ユーザー確定仕様)。"""

    def test_only_first_ticker_in_queue_is_processed(self):
        self._pending = ["NEWCO", "OTHER"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            return_value={
                "skipped": False,
                "result": {"day0_count": 3, "resolved_events": [1, 2], "unresolved_day0_dates": [3]},
                "elapsed": 1.2,
            },
        ) as m:
            rc = job.main()
        self.assertEqual(rc, 0)
        m.assert_called_once_with("NEWCO")
        self.assertEqual(self._pending, ["OTHER"])  # 処理済みの銘柄だけキューから除外、他は残る

    def test_already_done_ticker_outcome_skipped_is_removed_from_queue(self):
        self._pending = ["NEWCO"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            return_value={"skipped": True, "reason": "already_done", "ticker": "NEWCO"},
        ):
            rc = job.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._pending, [])


class FailureHandlingTests(RunPendingBackfillTestCase):
    """2026-10-28修正(Head-of-Line Blocking解消): 失敗時はstatus=failedを
    記録した上で、pending_backfillキューからそのtickerを削除する(以前は
    キューに残り続け、後続の新規銘柄が永久にブロックされる問題があった)。
    再試行の仕組み自体(status="failed"のtickerを翌日のrefresh_priormean_
    incremental.pyが再度キューへ追加する既存仕様)は維持する——
    DetectNewTickersTests.test_failed_tickers_are_retried(priormean_
    incremental_logic側、無変更)で既に確認済み。"""

    def test_file_not_found_marks_failed_and_removes_ticker_from_queue(self):
        self._pending = ["NEWCO"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            side_effect=FileNotFoundError("no price cache"),
        ):
            rc = job.main()
        self.assertEqual(rc, 1)
        self.assertEqual(self._status["NEWCO"], "failed")
        self.assertEqual(self._pending, [])  # キューから削除される(以前は残っていた)

    def test_unexpected_exception_marks_failed_and_removes_ticker_from_queue(self):
        self._pending = ["NEWCO"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            side_effect=RuntimeError("boom"),
        ):
            rc = job.main()
        self.assertEqual(rc, 1)
        self.assertEqual(self._status["NEWCO"], "failed")
        self.assertEqual(self._pending, [])  # キューから削除される(以前は残っていた)

    def test_failure_does_not_block_next_ticker_in_queue(self):
        """Head-of-Line Blocking解消の直接確認: A失敗→Aがキューから削除され、
        次回実行時にBが処理可能になる(A失敗後もBが先頭に来る)。"""
        self._pending = ["A", "B"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            side_effect=RuntimeError("Aは常に失敗する想定"),
        ):
            rc1 = job.main()  # 1回目: Aを処理→失敗
        self.assertEqual(rc1, 1)
        self.assertEqual(self._status["A"], "failed")
        self.assertEqual(self._pending, ["B"])  # Aが消え、Bが先頭に来る

        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            return_value={
                "skipped": False,
                "result": {"day0_count": 1, "resolved_events": [1], "unresolved_day0_dates": []},
                "elapsed": 0.5,
            },
        ) as m:
            rc2 = job.main()  # 2回目: 今度はBが処理される(Aにブロックされない)
        self.assertEqual(rc2, 0)
        m.assert_called_once_with("B")
        self.assertEqual(self._pending, [])

    def test_success_path_behavior_unchanged(self):
        """A成功時の既存処理(キューからの除外・戻り値)が変更されていない
        ことの確認(失敗系の修正が成功系に影響していないか)。"""
        self._pending = ["NEWCO", "OTHER"]
        with mock.patch.object(
            job.backfill_module, "backfill_one_ticker",
            return_value={
                "skipped": False,
                "result": {"day0_count": 2, "resolved_events": [1, 2], "unresolved_day0_dates": []},
                "elapsed": 3.4,
            },
        ) as m:
            rc = job.main()
        self.assertEqual(rc, 0)
        m.assert_called_once_with("NEWCO")
        self.assertEqual(self._pending, ["OTHER"])
        self.assertNotIn("NEWCO", self._status)  # 成功時、このスクリプト自身はstatusを変更しない(backfill_one_ticker側が"done"を設定する、無変更)


if __name__ == "__main__":
    unittest.main()
