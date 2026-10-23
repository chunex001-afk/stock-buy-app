# -*- coding: utf-8 -*-
"""refresh.py(日次株価更新)のmain()が、株価更新完了通知
(push_notify.notify_refresh_success)を正しい引数で呼び出すことだけを確認する
配線テスト(design 2026-10-22)。Twelve Data/Upstash Redisへの実アクセスは
一切行わない(run_quintile_refresh・redis_store双方をmockで置き換える)。

Q1〜Q5判定ロジック(run_quintile_refresh内部)・既存のdaily-refresh処理の
計算結果自体はこのテストの対象外(test_quintile_logic.py等、既存のテストで
別途検証済み)。ここではpush_notify呼び出しの配線(trading_dateの受け渡し・
td_result=Noneの場合の扱い・例外時にmain()をクラッシュさせないこと)のみを
確認する。push_notify.notify_refresh_success自体のロジック(重複送信防止・
再送可否等)はtest_push_notify.pyのRefreshSuccessNotificationTestsで検証済み。
"""
import os
import unittest
from unittest import mock

import refresh


def _env():
    return {"TWELVEDATA_API_KEY": "dummy-key"}


class RefreshNotifyWiringTests(unittest.TestCase):
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
        # AI市場下落状況パネル(design 2026-10-23)はmain()内でrun_quintile_refresh
        # とは別に呼ばれる独立した追加処理のため、このテストの対象外として
        # mockで無効化する(実Twelve Data呼び出しを避けるだけで、push_notify
        # 配線の検証対象には一切含めない)。
        p4 = mock.patch("refresh._update_ai_market_decline_state", return_value=None)
        p4.start()
        self.addCleanup(p4.stop)

    def test_notifies_with_trading_date_when_quintile_updated(self):
        td_result = {
            "fetched": {}, "failed": [], "rate_limited": False,
            "trading_date": "2026-10-22", "quintile_updated": True,
            "raw_watchlist_data": {},
        }
        with mock.patch.dict(os.environ, _env(), clear=True), \
             mock.patch("refresh.run_quintile_refresh", return_value=td_result), \
             mock.patch.object(refresh.push_notify, "notify_refresh_success") as m:
            rc = refresh.main()
        self.assertEqual(rc, 0)
        m.assert_called_once_with(refresh.store, "2026-10-22")

    def test_does_not_notify_when_trading_date_missing(self):
        """SPY取得失敗等でtrading_date=None(=全体失敗)の場合、Noneを渡して
        呼ぶ(実際に通知するかどうかの判断はpush_notify側、Noneなら何もしない)。"""
        td_result = {
            "fetched": {}, "failed": [], "rate_limited": False,
            "trading_date": None, "quintile_updated": False,
            "raw_watchlist_data": {},
        }
        with mock.patch.dict(os.environ, _env(), clear=True), \
             mock.patch("refresh.run_quintile_refresh", return_value=td_result), \
             mock.patch.object(refresh.push_notify, "notify_refresh_success") as m:
            rc = refresh.main()
        self.assertEqual(rc, 0)
        m.assert_called_once_with(refresh.store, None)

    def test_does_not_crash_when_quintile_refresh_raises(self):
        """Q1〜Q5処理自体が予期しない例外を出した場合(td_resultが未代入のまま)
        でも、通知呼び出しはNoneで安全に行われ、main()はクラッシュしない。"""
        with mock.patch.dict(os.environ, _env(), clear=True), \
             mock.patch("refresh.run_quintile_refresh", side_effect=RuntimeError("boom")), \
             mock.patch.object(refresh.push_notify, "notify_refresh_success") as m:
            rc = refresh.main()
        self.assertEqual(rc, 0)
        m.assert_called_once_with(refresh.store, None)

    def test_push_notify_exception_does_not_fail_main(self):
        """notify_refresh_success自体が(本来は内部で握っているはずだが)万一
        例外を出しても、main()の終了コード・既存処理には影響しない。"""
        td_result = {
            "fetched": {}, "failed": [], "rate_limited": False,
            "trading_date": "2026-10-22", "quintile_updated": True,
            "raw_watchlist_data": {},
        }
        with mock.patch.dict(os.environ, _env(), clear=True), \
             mock.patch("refresh.run_quintile_refresh", return_value=td_result), \
             mock.patch.object(refresh.push_notify, "notify_refresh_success", side_effect=RuntimeError("down")):
            rc = refresh.main()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
