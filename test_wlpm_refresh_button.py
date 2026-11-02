# -*- coding: utf-8 -*-
"""「監視銘柄のQ5過去平均(登録段階チェック用)」パネル専用の手動更新ボタン
(design 2026-11-02ユーザー確定仕様)のテスト。

新しいバックフィルロジックは一切作らず、既存のapp._dispatch_priormean_backfill
(workflow_dispatch経由でpriormean-backfill-dispatch.yml→backfill_dispatched_
ticker.py→backfill_new_ticker_prior_mean.backfill_one_ticker、いずれも無変更)
と既存のapp._build_watchlist_prior_means(無変更)を、1銘柄だけに絞って呼ぶ
新しい2つのエンドポイントを検証する。

Twelve Data・GitHub Actions・Upstashへは一切アクセスしない(app.store/
app._dispatch_priormean_backfillをmockで差し替える)。
"""
import json
import unittest
from unittest import mock

import app


class WlpmRefreshButtonEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._watchlist = ["CRWD", "AAPL"]  # AAPLは静的56銘柄扱いのダミー
        self._backfill_status = {}
        self._calendar = ["2026-10-28", "2026-10-29", "2026-10-30"]

        def fake_get_watchlist():
            return list(self._watchlist)

        def fake_get_backfill_status(ticker):
            return self._backfill_status.get(ticker.upper())

        patches = [
            mock.patch.object(app.store, "get_watchlist", side_effect=fake_get_watchlist),
            mock.patch.object(app.store, "get_priormean_backfill_status", side_effect=fake_get_backfill_status),
            mock.patch.object(
                app.q5_prior_mean_logic, "load_reference_data",
                return_value={"events_by_ticker": {"AAPL": [{"date": "2020-01-01", "ret20": 1.0, "max_dd20": -1.0}]}},
            ),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
            mock.patch.object(app, "GITHUB_DISPATCH_TOKEN", "dummy-token"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post_refresh(self, ticker):
        resp = self.client.post(f"/api/watchlist/{ticker}/refresh_prior_mean")
        return resp, resp.get_json()

    def _get_status(self, ticker):
        resp = self.client.get(f"/api/watchlist/{ticker}/prior_mean_status")
        return resp, resp.get_json()

    # ------------------------------------------------------------------
    # POST /api/watchlist/<ticker>/refresh_prior_mean
    # ------------------------------------------------------------------
    def test_ticker_not_in_watchlist_is_rejected(self):
        with mock.patch.object(app, "_dispatch_priormean_backfill") as dispatch:
            resp, body = self._post_refresh("NOTWATCHED")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(body["ok"])
        dispatch.assert_not_called()

    def test_static_reference_ticker_is_rejected_without_dispatch(self):
        # AAPLはload_reference_dataのevents_by_tickerに含まれる=既存56銘柄
        # 相当の静的データ。更新ボタンを押しても意味が無いため拒否する。
        with mock.patch.object(app, "_dispatch_priormean_backfill") as dispatch:
            resp, body = self._post_refresh("AAPL")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(body["ok"])
        dispatch.assert_not_called()

    def test_already_done_does_not_dispatch(self):
        self._backfill_status["CRWD"] = "done"
        with mock.patch.object(app, "_dispatch_priormean_backfill") as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["dispatched"])
        dispatch.assert_not_called()

    def test_already_in_progress_does_not_dispatch_again(self):
        # 二重実行防止(design 2026-11-02): backfill_one_ticker自体のRedis
        # ロック・GitHub Actions concurrencyグループに加え、ここでも無駄な
        # dispatch呼び出しを事前に弾く。
        self._backfill_status["CRWD"] = "in_progress"
        with mock.patch.object(app, "_dispatch_priormean_backfill") as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["dispatched"])
        dispatch.assert_not_called()

    def test_pending_status_triggers_dispatch(self):
        self._backfill_status["CRWD"] = "pending"
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=True) as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["dispatched"])
        dispatch.assert_called_once_with("CRWD")

    def test_failed_status_can_be_retried_and_triggers_dispatch(self):
        # 「失敗した場合はCRWDだけ再試行できる」というユーザー要件の確認。
        self._backfill_status["CRWD"] = "failed"
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=True) as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["dispatched"])
        dispatch.assert_called_once_with("CRWD")

    def test_none_status_triggers_dispatch(self):
        # backfill_statusがまだ一度も記録されていない(新規追加直後)場合も
        # 手動更新を試行できる。
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=True) as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["dispatched"])
        dispatch.assert_called_once_with("CRWD")

    def test_dispatch_failure_is_reported_as_error(self):
        self._backfill_status["CRWD"] = "pending"
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=False) as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(body["ok"])
        dispatch.assert_called_once_with("CRWD")

    def test_no_dispatch_token_configured_returns_error_without_calling_dispatch(self):
        self._backfill_status["CRWD"] = "pending"
        with mock.patch.object(app, "GITHUB_DISPATCH_TOKEN", ""), \
             mock.patch.object(app, "_dispatch_priormean_backfill") as dispatch:
            resp, body = self._post_refresh("CRWD")
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(body["ok"])
        dispatch.assert_not_called()

    def test_ticker_is_sanitized_and_case_insensitive(self):
        self._backfill_status["CRWD"] = "pending"
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=True) as dispatch:
            resp, body = self._post_refresh("crwd")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        dispatch.assert_called_once_with("CRWD")

    # ------------------------------------------------------------------
    # GET /api/watchlist/<ticker>/prior_mean_status
    # ------------------------------------------------------------------
    def test_status_endpoint_rejects_ticker_not_in_watchlist(self):
        resp, body = self._get_status("NOTWATCHED")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(body["ok"])

    def test_status_endpoint_reuses_build_watchlist_prior_means(self):
        # 新しい計算ロジックを作らず、既存のapp._build_watchlist_prior_means
        # (無変更)を1銘柄だけのリストで呼んだ結果と完全に一致すること。
        expected = app._build_watchlist_prior_means(["CRWD"], self._calendar[-1])[0]
        resp, body = self._get_status("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["item"], expected)

    def test_status_endpoint_returns_none_item_when_no_trading_calendar(self):
        with mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: []):
            resp, body = self._get_status("CRWD")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["item"])

    def test_status_endpoint_does_not_touch_other_tickers(self):
        # 他の監視銘柄(AAPL)のバックフィル状態・prior_meanには一切触れない
        # (_build_watchlist_prior_meansに渡すリストが[ticker]単体であること)。
        with mock.patch.object(
            app, "_build_watchlist_prior_means", wraps=app._build_watchlist_prior_means,
        ) as wrapped:
            self._get_status("CRWD")
        wrapped.assert_called_once_with(["CRWD"], self._calendar[-1])


if __name__ == "__main__":
    unittest.main()
