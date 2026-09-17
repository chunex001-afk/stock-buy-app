# -*- coding: utf-8 -*-
"""refresh.backfill_quintile_history_for_new_ticker の状態遷移テスト
(2026-09-18、delete→再add時にQ1〜5履歴が原因で失敗する不具合の修正確認)。

Twelve Data・Redisへは一切アクセスしない(全てmockで代替)。実際のAPI呼び出し
回数は`_td_fetch_one`のcall_countで検証する(要件: 「必ず1回だけ」の確認)。
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import refresh


def _fake_series(n=260, end="2026-09-18", start_price=100.0):
    """テスト用の合成株価データ(平日のみの日付・単純な周期変動の終値)。"""
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


def _fake_pool_history(dates, n_tail=10):
    """quintile:pool_history相当のダミーデータ(直近n_tail日分、5値のダミープール)。"""
    tail = dates[-n_tail:]
    scores_by_date = {d: [10.0, 20.0, 30.0, 40.0, 50.0] for d in tail}
    return {"dates": tail, "scores_by_date": scores_by_date}


class BackfillNewTickerTests(unittest.TestCase):
    def setUp(self):
        self.dates, self.closes, self.volumes = _fake_series()
        self.pool_history = _fake_pool_history(self.dates)

        budget_patcher = mock.patch.object(refresh, "remaining_td_budget", return_value=(700, 0))
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)

    def _patch_store(self, quintile_state=None, ticker_record=None, pool_history="__default__"):
        if pool_history == "__default__":
            pool_history = self.pool_history
        patchers = [
            mock.patch.object(refresh.store, "get_quintile_state", return_value=quintile_state),
            mock.patch.object(refresh.store, "get_ticker_record", return_value=ticker_record),
            mock.patch.object(refresh.store, "get_pool_history", return_value=pool_history),
            mock.patch.object(refresh.store, "set_quintile_state"),
            mock.patch.object(refresh.store, "set_ticker_record"),
        ]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        # get_state, get_record, get_pool, set_state, set_record
        return mocks

    def _patch_fetch_success(self):
        return mock.patch.object(
            refresh, "_td_fetch_one",
            return_value=((self.dates, self.closes, self.volumes), False, None, None),
        )

    def _patch_fetch_failure(self, error_type="INVALID_SYMBOL", message="not found"):
        return mock.patch.object(
            refresh, "_td_fetch_one", return_value=(None, False, error_type, message),
        )

    # ------------------------------------------------------------------
    # A. 新規追加(旧指標・Q1〜5とも未存在)
    # ------------------------------------------------------------------
    def test_A_brand_new_ticker(self):
        _, _, _, set_state, set_record = self._patch_store(quintile_state=None, ticker_record=None)
        with self._patch_fetch_success() as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("NEWCO", "key")

        self.assertEqual(fetch.call_count, 1, "新規追加はTwelve Dataを1回だけ呼ぶ")
        self.assertTrue(result["ok"])
        set_record.assert_called_once()
        set_state.assert_called_once()

    # ------------------------------------------------------------------
    # B. 追加→削除→再追加(delete_watchlistはRedis状態を消さないため、
    #    再追加時には旧指標・Q1〜5とも既存データが残っている想定)
    # ------------------------------------------------------------------
    def test_B_readd_after_delete_both_already_valid(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q3"}], "current_q": "Q3"}
        existing_record = {"last_trade_date": self.dates[-10]}  # 削除前の少し古いデータ
        _, _, _, set_state, set_record = self._patch_store(existing_state, existing_record)

        with self._patch_fetch_success() as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("SKHY", "key")

        self.assertEqual(fetch.call_count, 1, "要件1: 再追加時は必ず1回だけTwelve Dataを取得する")
        self.assertTrue(result["ok"])
        set_record.assert_called_once()  # legacyは最新化される
        set_state.assert_not_called()    # 要件2: 既存のQ1〜5historyは上書きしない

    # ------------------------------------------------------------------
    # C. legacyのみ存在(Q1〜5 stateが無い)
    # ------------------------------------------------------------------
    def test_C_legacy_only(self):
        existing_record = {"last_trade_date": self.dates[-1]}
        _, _, _, set_state, set_record = self._patch_store(quintile_state=None, ticker_record=existing_record)

        with self._patch_fetch_success() as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("TICK", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(result["ok"])
        set_record.assert_called_once()
        set_state.assert_called_once()  # 要件4: Q1〜5を新規構築する

    # ------------------------------------------------------------------
    # D. Q1〜5のみ存在(legacyが無い) = 実際のSKHY障害と同じ状態
    # ------------------------------------------------------------------
    def test_D_quintile_only(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q4"}], "current_q": "Q4"}
        _, _, _, set_state, set_record = self._patch_store(quintile_state=existing_state, ticker_record=None)

        with self._patch_fetch_success() as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("SKHY", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(result["ok"])
        set_record.assert_called_once()  # 要件3: legacyだけ修復する
        set_state.assert_not_called()    # 要件3: Q1〜5は再計算しない

    # ------------------------------------------------------------------
    # E. 両方存在(正常)
    # ------------------------------------------------------------------
    def test_E_both_exist(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q2"}], "current_q": "Q2"}
        existing_record = {"last_trade_date": self.dates[-1]}
        _, _, _, set_state, set_record = self._patch_store(existing_state, existing_record)

        with self._patch_fetch_success() as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("AXTI", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(result["ok"])
        set_record.assert_called_once()  # legacyは最新化される
        set_state.assert_not_called()    # Q1〜5は不要に上書きしない

    # ------------------------------------------------------------------
    # F. 両方存在するが、legacy再構築が(何らかの理由で)失敗する
    # ------------------------------------------------------------------
    def test_F_legacy_repair_fails_quintile_untouched(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q5"}], "current_q": "Q5"}
        _, _, _, set_state, set_record = self._patch_store(quintile_state=existing_state, ticker_record=None)

        with self._patch_fetch_success() as fetch, \
             mock.patch.object(refresh.logic, "build_result", side_effect=RuntimeError("boom")), \
             mock.patch.object(refresh, "_mark_stale") as mark_stale:
            result = refresh.backfill_quintile_history_for_new_ticker("SKHY", "key")

        self.assertEqual(fetch.call_count, 1)
        mark_stale.assert_called_once()
        set_record.assert_not_called()   # 壊れた内容では保存しない
        set_state.assert_not_called()    # Q1〜5は無関係、一切触れない
        # legacy修復に失敗してもQ1〜5が表示可能なため、呼び出し全体はok
        self.assertTrue(result["ok"])

    # ------------------------------------------------------------------
    # G. Twelve Data取得失敗
    # ------------------------------------------------------------------
    def test_G_fetch_fails_nothing_existing(self):
        _, _, _, set_state, set_record = self._patch_store(quintile_state=None, ticker_record=None)

        with self._patch_fetch_failure(error_type="RATE_LIMIT") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("NEWCO", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "RATE_LIMIT")
        set_record.assert_not_called()
        set_state.assert_not_called()

    def test_G_fetch_fails_but_existing_data_shown(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q4"}], "current_q": "Q4"}
        existing_record = {"last_trade_date": self.dates[-1]}
        _, _, _, set_state, set_record = self._patch_store(existing_state, existing_record)

        with self._patch_fetch_failure(error_type="RATE_LIMIT") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("AXTI", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(result["ok"])  # 既存データがあるので「失敗」とはしない
        self.assertIsNotNone(result["message"])  # 「最新データを取得しました」と誤表示しないための説明文
        set_record.assert_not_called()
        set_state.assert_not_called()

    # ------------------------------------------------------------------
    # H. 無効ticker(Twelve DataがINVALID_SYMBOLを返す)
    # ------------------------------------------------------------------
    def test_H_invalid_symbol(self):
        _, _, _, set_state, set_record = self._patch_store(quintile_state=None, ticker_record=None)

        with self._patch_fetch_failure(error_type="INVALID_SYMBOL", message="Symbol not found") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("ZZZZZZZ", "key")

        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "INVALID_SYMBOL")
        set_record.assert_not_called()
        set_state.assert_not_called()

    # ------------------------------------------------------------------
    # 追加確認: APIキー未設定・予算切れではTwelve Dataを一切呼ばない
    # ------------------------------------------------------------------
    def test_no_api_key_never_calls_twelvedata(self):
        self._patch_store(quintile_state=None, ticker_record=None)
        with mock.patch.object(refresh, "_td_fetch_one") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("NEWCO", "")
        fetch.assert_not_called()
        self.assertFalse(result["ok"])

    def test_budget_exhausted_never_calls_twelvedata(self):
        self._patch_store(quintile_state=None, ticker_record=None)
        with mock.patch.object(refresh, "remaining_td_budget", return_value=(0, 700)), \
             mock.patch.object(refresh, "_td_fetch_one") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("NEWCO", "key")
        fetch.assert_not_called()
        self.assertFalse(result["ok"])

    def test_budget_exhausted_but_existing_data_shown(self):
        existing_state = {"history": [{"date": self.dates[-2], "q": "Q4"}], "current_q": "Q4"}
        existing_record = {"last_trade_date": self.dates[-1]}
        self._patch_store(existing_state, existing_record)
        with mock.patch.object(refresh, "remaining_td_budget", return_value=(0, 700)), \
             mock.patch.object(refresh, "_td_fetch_one") as fetch:
            result = refresh.backfill_quintile_history_for_new_ticker("AXTI", "key")
        fetch.assert_not_called()
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
