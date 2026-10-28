# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-28d、design確認済み)Phase 4:
app.py側の即時dispatch呼び出し(_dispatch_priormean_backfill)と、
/api/watchlist(add_watchlist)への配線のテスト。

GitHub API・Twelve Data・Upstashへは一切アクセスしない(requests.post/
redis_store側の該当関数をmockで差し替える)。
"""
import json
import unittest
from unittest import mock

import requests

import app


class DispatchFunctionTests(unittest.TestCase):
    """_dispatch_priormean_backfill単体のテスト(HTTP呼び出しをmock)。"""

    def setUp(self):
        patches = [
            mock.patch.object(app, "GITHUB_DISPATCH_TOKEN", "fake-token"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_success_returns_true(self):
        fake_resp = mock.Mock(status_code=204)
        with mock.patch.object(app.requests, "post", return_value=fake_resp) as m_post:
            result = app._dispatch_priormean_backfill("FOO")
        self.assertTrue(result)
        m_post.assert_called_once()
        args, kwargs = m_post.call_args
        self.assertIn("priormean-backfill-dispatch.yml", args[0])
        self.assertEqual(kwargs["json"], {"ref": "main", "inputs": {"ticker": "FOO"}})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-token")
        self.assertIn("timeout", kwargs)

    def test_github_api_error_status_returns_false_without_raising(self):
        fake_resp = mock.Mock(status_code=422)
        with mock.patch.object(app.requests, "post", return_value=fake_resp):
            result = app._dispatch_priormean_backfill("FOO")
        self.assertFalse(result)

    def test_timeout_returns_false_without_raising(self):
        with mock.patch.object(app.requests, "post", side_effect=requests.Timeout("timed out")):
            result = app._dispatch_priormean_backfill("FOO")
        self.assertFalse(result)

    def test_connection_error_returns_false_without_raising(self):
        with mock.patch.object(app.requests, "post", side_effect=requests.ConnectionError("boom")):
            result = app._dispatch_priormean_backfill("FOO")
        self.assertFalse(result)

    def test_missing_token_skips_http_call_entirely(self):
        with mock.patch.object(app, "GITHUB_DISPATCH_TOKEN", ""), \
             mock.patch.object(app.requests, "post") as m_post:
            result = app._dispatch_priormean_backfill("FOO")
        self.assertFalse(result)
        m_post.assert_not_called()


class AddWatchlistDispatchWiringTests(unittest.TestCase):
    """/api/watchlist(POST)からの配線テスト。既存のwatchlist追加成否には
    一切影響しないことを確認する(design 2026-10-28d、ユーザー確定仕様)。"""

    def setUp(self):
        self.client = app.app.test_client()
        self._watchlist = []

        def fake_get_watchlist():
            return list(self._watchlist)

        def fake_set_watchlist(tickers):
            self._watchlist = list(tickers)
            return True

        patches = [
            mock.patch.object(app.store, "get_watchlist", side_effect=fake_get_watchlist),
            mock.patch.object(app.store, "set_watchlist", side_effect=fake_set_watchlist),
            mock.patch.object(app.store, "set_priormean_watchlist_added_at", return_value=True),
            mock.patch.object(app, "TD_API_KEY", ""),  # 既存の即時Q1-5取得はこのテストの対象外
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _add(self, ticker):
        resp = self.client.post("/api/watchlist", data=json.dumps({"ticker": ticker}),
                                 content_type="application/json")
        return resp, resp.get_json()

    def test_watchlist_add_succeeds_even_when_dispatch_raises(self):
        with mock.patch.object(app, "_dispatch_priormean_backfill", side_effect=RuntimeError("boom")):
            resp, data = self._add("NEWCO")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertIn("NEWCO", self._watchlist)

    def test_watchlist_add_succeeds_even_when_dispatch_returns_false(self):
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=False) as m_dispatch:
            resp, data = self._add("NEWCO")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        m_dispatch.assert_called_once_with("NEWCO")

    def test_dispatch_is_called_for_genuinely_new_ticker(self):
        with mock.patch.object(app, "_dispatch_priormean_backfill", return_value=True) as m_dispatch:
            self._add("NEWCO")
        m_dispatch.assert_called_once_with("NEWCO")

    def test_dispatch_is_not_called_for_existing_56_ticker(self):
        """既存56銘柄(例: NVDA)はprior_meanが静的JSONから計算されるため、
        Redis backfillの対象ではない。不要なTwelve Data呼び出しを避けるため
        dispatchしないことを確認する。"""
        with mock.patch.object(app, "_dispatch_priormean_backfill") as m_dispatch:
            resp, data = self._add("NVDA")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        m_dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
