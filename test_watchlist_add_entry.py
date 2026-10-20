# -*- coding: utf-8 -*-
"""「＋追加（即時取得）」で取得日・取得価格をまとめて登録できるようにする
変更(design 2026-10-21)のテスト。

実際の変更はフロントエンド(addTicker())がPOST /api/watchlist(既存・無変更)の
成功後、取得価格が入力されている場合だけ続けてPOST /api/portfolio/confirm_buy
(既存API、今回はentry_dateの未来日チェックのみ追加)を呼ぶという2段呼び出し
なので、ここでは両エンドポイントをFlaskテストクライアントで実際にその順で
呼び、結果を検証する。Upstash・Twelve Dataへは一切アクセスしない(store側・
refresh側の該当関数のみmockで差し替える)。
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import app


class WatchlistAddEntryTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._watchlist = []
        self._portfolio_state = None
        self._calendar = [f"2026-01-{d:02d}" for d in range(1, 29) if d % 7 not in (0,)]
        self._today_jst = "2026-10-20"

        def fake_get_watchlist():
            return list(self._watchlist)

        def fake_set_watchlist(tickers):
            self._watchlist = list(tickers)
            return True

        def fake_get_portfolio_state():
            return self._portfolio_state

        def fake_set_portfolio_state(data):
            self._portfolio_state = data
            return True

        patches = [
            mock.patch.object(app.store, "get_watchlist", side_effect=fake_get_watchlist),
            mock.patch.object(app.store, "set_watchlist", side_effect=fake_set_watchlist),
            mock.patch.object(app.store, "get_portfolio_state", side_effect=fake_get_portfolio_state),
            mock.patch.object(app.store, "set_portfolio_state", side_effect=fake_set_portfolio_state),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
            mock.patch.object(app, "_today_jst_str", side_effect=lambda: self._today_jst),
            # Twelve Dataへの即時取得は今回の変更と無関係(既存のbackfill_new_ticker
            # テストで別途カバー済み)なので、APIキー未設定扱いにしてスキップさせる。
            mock.patch.object(app, "TD_API_KEY", ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _add_watchlist(self, ticker):
        resp = self.client.post("/api/watchlist", data=json.dumps({"ticker": ticker}),
                                 content_type="application/json")
        return resp, resp.get_json()

    def _confirm_buy(self, body):
        resp = self.client.post("/api/portfolio/confirm_buy", data=json.dumps(body),
                                 content_type="application/json")
        return resp, resp.get_json()

    def _positions(self):
        return (self._portfolio_state or {}).get("positions") or {}


class PriceOnlyAddTests(WatchlistAddEntryTestCase):
    """1. 銘柄コード＋取得価格だけで追加 → ポジション登録される。"""

    def test_price_only_creates_position_with_default_entry_date(self):
        r1, d1 = self._add_watchlist("COHR")
        self.assertTrue(d1["ok"])
        self.assertIn("COHR", self._watchlist)
        self.assertNotIn("COHR", self._positions())  # watchlist追加だけではポジションは作られない

        r2, d2 = self._confirm_buy({"ticker": "COHR", "entry_price": 232.90})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(d2["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 232.90)
        self.assertEqual(pos["entry_date"], self._calendar[-1])  # 省略時デフォルト


class DateAndPriceAddTests(WatchlistAddEntryTestCase):
    """2. 銘柄コード＋取得日＋取得価格 → 指定したentry_date/entry_priceで登録される。"""

    def test_explicit_entry_date_and_price_are_saved_as_given(self):
        self._add_watchlist("COHR")
        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 232.90, "entry_date": "2026-10-19"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 232.90)
        self.assertEqual(pos["entry_date"], "2026-10-19")


class WatchlistOnlyAddTests(WatchlistAddEntryTestCase):
    """3. 取得価格なし → 従来どおりウォッチリストだけに追加され、ポジションは作られない。"""

    def test_watchlist_add_without_price_never_touches_positions(self):
        r, d = self._add_watchlist("COHR")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertIn("COHR", self._watchlist)
        self.assertIsNone(self._portfolio_state)  # confirm_buyを呼んでいないので未初期化のまま
        self.assertNotIn("COHR", self._positions())


class EntryDateValidationTests(WatchlistAddEntryTestCase):
    """4〜6. 取得日の過去日/未来日/データ最終処理日より新しい過去日の扱い。"""

    def test_4_past_entry_date_is_accepted(self):
        self._add_watchlist("COHR")
        past = "2026-01-05"
        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 100.0, "entry_date": past})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], past)

    def test_5_future_entry_date_is_rejected(self):
        self._add_watchlist("COHR")
        future = (date.fromisoformat(self._today_jst) + timedelta(days=1)).isoformat()
        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 100.0, "entry_date": future})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(d["ok"])
        self.assertNotIn("COHR", self._positions())

    def test_5b_entry_date_equal_to_today_jst_is_accepted(self):
        self._add_watchlist("COHR")
        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 100.0, "entry_date": self._today_jst})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], self._today_jst)

    def test_6_entry_date_after_stale_trading_calendar_but_not_future_is_accepted(self):
        """株価データの最終処理日(trading_calendar[-1])より後の過去日でも、
        実際の今日(JST)より後でなければ登録できる(edit_entryと同じ仕様)。"""
        self._calendar = ["2026-10-16"]  # 株価データはここで止まっている想定
        self._today_jst = "2026-10-20"   # 実際の今日はこちら
        self._add_watchlist("COHR")

        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 232.90, "entry_date": "2026-10-19"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], "2026-10-19")


class DuplicateTickerTests(WatchlistAddEntryTestCase):
    """7. 既存銘柄の重複登録 → 現在の重複防止仕様を維持。"""

    def test_duplicate_watchlist_add_is_rejected(self):
        self._add_watchlist("COHR")
        r, d = self._add_watchlist("COHR")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(d["ok"])
        self.assertEqual(self._watchlist.count("COHR"), 1)

    def test_duplicate_position_buy_is_rejected(self):
        self._add_watchlist("COHR")
        self._confirm_buy({"ticker": "COHR", "entry_price": 100.0})
        r, d = self._confirm_buy({"ticker": "COHR", "entry_price": 999.0})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(d["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_price"], 100.0)  # 上書きされない


class ExistingEditEntryApiUntouchedTests(WatchlistAddEntryTestCase):
    """既存の取得情報編集API(edit_entry)が今回の変更後も従来どおり動くことの確認。"""

    def test_edit_entry_still_works_after_combined_add(self):
        self._add_watchlist("COHR")
        self._confirm_buy({"ticker": "COHR", "entry_price": 232.90, "entry_date": "2026-10-19"})

        resp = self.client.post("/api/portfolio/edit_entry",
                                 data=json.dumps({"ticker": "COHR", "entry_price": 230.0}),
                                 content_type="application/json")
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 230.0)
        self.assertEqual(pos["entry_date"], "2026-10-19")  # entry_priceのみ変更、entry_dateは不変


if __name__ == "__main__":
    unittest.main()
