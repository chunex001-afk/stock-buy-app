# -*- coding: utf-8 -*-
"""実運用の売買成績蓄積(design 2026-10-21、フェーズ1)のテスト。

confirm_sellが売却時に売却日・売却価格を受け取り、positions削除前にtrade_history
(ticker/entry_date/entry_price/exit_date/exit_price/return_pct)へ記録することを
確認する。株数(shares)はこの履歴に含まれない(design確定仕様)。

既存のQ5判定・BUY判定・20営業日SELL判定・+50%利確・Risk-off・既存の取得日/
取得価格編集機能(edit_entry)には一切触れていないことも、trade_historyが
confirm_buy/confirm_profit/edit_entry経由でも保持され続けることを含めて確認する
(いずれもstate全体を上書き保存するため、引き継ぎ漏れがあれば履歴が消える)。

Upstash・Twelve Dataへは一切アクセスしない(store側の該当関数のみmockで
差し替える)。
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import app


class TradeHistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._records = {}
        self._watchlist = ["AAA", "BBB", "CCC"]
        self._calendar = [f"2026-01-{d:02d}" for d in range(1, 29) if d % 7 not in (0,)]
        self._today_jst = self._calendar[-1]

        def fake_get_portfolio_state():
            return self._portfolio_state

        def fake_set_portfolio_state(data):
            self._portfolio_state = data
            return True

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=fake_get_portfolio_state),
            mock.patch.object(app.store, "set_portfolio_state", side_effect=fake_set_portfolio_state),
            mock.patch.object(app.store, "get_watchlist", side_effect=lambda: list(self._watchlist)),
            mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self._records.get(t)),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: self._calendar),
            mock.patch.object(app, "_today_jst_str", side_effect=lambda: self._today_jst),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, path, body):
        resp = self.client.post(path, data=json.dumps(body), content_type="application/json")
        return resp, resp.get_json()

    def _positions(self):
        return (self._portfolio_state or {}).get("positions") or {}

    def _trade_history(self):
        return (self._portfolio_state or {}).get("trade_history") or []

    def _buy(self, ticker, entry_price, entry_date=None):
        body = {"ticker": ticker, "entry_price": entry_price}
        if entry_date:
            body["entry_date"] = entry_date
        return self._post("/api/portfolio/confirm_buy", body)


class SellWithExplicitPriceAndDateTests(TradeHistoryTestCase):
    def test_sell_with_exit_price_and_date_records_trade_history(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 120.0, "exit_date": self._calendar[5]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())  # positionsは従来どおり削除

        history = self._trade_history()
        self.assertEqual(len(history), 1)
        rec = history[0]
        self.assertEqual(rec["ticker"], "AAA")
        self.assertEqual(rec["entry_date"], self._calendar[0])
        self.assertEqual(rec["entry_price"], 100.0)
        self.assertEqual(rec["exit_date"], self._calendar[5])
        self.assertEqual(rec["exit_price"], 120.0)
        self.assertAlmostEqual(rec["return_pct"], 20.0)
        self.assertNotIn("shares", rec)  # 株数は含めない

    def test_sell_with_loss_computes_negative_return_pct(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 80.0})
        rec = self._trade_history()[0]
        self.assertAlmostEqual(rec["return_pct"], -20.0)


class ExitDateDefaultAndValidationTests(TradeHistoryTestCase):
    def test_exit_date_omitted_defaults_to_today(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})
        rec = self._trade_history()[0]
        self.assertEqual(rec["exit_date"], self._calendar[-1])

    def test_past_exit_date_is_accepted(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        past = self._calendar[3]
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 110.0, "exit_date": past})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self._trade_history()[0]["exit_date"], past)

    def test_future_exit_date_is_rejected_and_position_not_deleted(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        future = (date.fromisoformat(self._today_jst) + timedelta(days=1)).isoformat()
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 110.0, "exit_date": future})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("AAA", self._positions())  # 拒否時はpositionsも変更されない
        self.assertEqual(self._trade_history(), [])

    def test_malformed_exit_date_is_rejected(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 110.0, "exit_date": "not-a-date"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("AAA", self._positions())


class ExitDateBeforeEntryDateTests(TradeHistoryTestCase):
    """design 2026-10-21修正: 「買う前に売った」という矛盾したtrade_historyが
    記録される不具合の修正確認(exit_date < entry_dateの拒否)。"""

    def test_exit_date_before_entry_date_is_rejected(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[10])
        before = self._calendar[5]
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 120.0, "exit_date": before})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("AAA", self._positions())  # positionsは削除されない
        self.assertEqual(self._trade_history(), [])  # trade_historyにも記録されない

    def test_exit_date_equal_to_entry_date_is_accepted(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[10])
        same = self._calendar[10]
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 120.0, "exit_date": same})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())
        rec = self._trade_history()[0]
        self.assertEqual(rec["entry_date"], same)
        self.assertEqual(rec["exit_date"], same)
        self.assertAlmostEqual(rec["return_pct"], 20.0)

    def test_exit_date_after_entry_date_is_accepted(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[10])
        after = self._calendar[15]
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 120.0, "exit_date": after})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())
        self.assertEqual(self._trade_history()[0]["exit_date"], after)

    def test_future_exit_date_still_rejected_after_this_fix(self):
        """回帰確認: entry_dateより前の拒否を追加しても、既存の未来日拒否は
        引き続き動作する(両方のチェックが独立して効くことの確認)。"""
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        future = (date.fromisoformat(self._today_jst) + timedelta(days=1)).isoformat()
        resp, data = self._post("/api/portfolio/confirm_sell",
                                 {"ticker": "AAA", "exit_price": 120.0, "exit_date": future})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("AAA", self._positions())
        self.assertEqual(self._trade_history(), [])


class ExitPriceValidationTests(TradeHistoryTestCase):
    def test_non_numeric_exit_price_is_rejected_and_position_not_deleted(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("AAA", self._positions())
        self.assertEqual(self._trade_history(), [])

    def test_non_positive_exit_price_is_rejected(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        resp2, data2 = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": -5})
        self.assertEqual(resp2.status_code, 400)
        self.assertFalse(data2["ok"])
        self.assertIn("AAA", self._positions())


class BackwardCompatibilityNoPriceTests(TradeHistoryTestCase):
    """既存の「価格を入れずに売却確認するだけ」の挙動が壊れていないことの確認
    (test_portfolio_api.pyのConfirmSellTestsと同じ呼び出し方)。"""

    def test_sell_without_price_falls_back_to_current_price(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._records["AAA"] = {"price": 130.0}
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())
        rec = self._trade_history()[0]
        self.assertEqual(rec["exit_price"], 130.0)
        self.assertAlmostEqual(rec["return_pct"], 30.0)

    def test_sell_without_price_and_without_current_price_skips_history_but_still_sells(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        # self._records に "AAA" を設定しない = 現在価格も取得不能
        resp, data = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("AAA", self._positions())  # 売却自体は従来どおり成功
        self.assertEqual(self._trade_history(), [])  # 履歴には記録されない

    def test_idempotent_resell_of_already_absent_ticker_unaffected(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})
        resp2, data2 = self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 999.0})
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(data2.get("already_absent"))
        self.assertEqual(len(self._trade_history()), 1)  # 二重記録されない
        self.assertEqual(self._trade_history()[0]["exit_price"], 110.0)


class TradeHistorySurvivesOtherWritesTests(TradeHistoryTestCase):
    """confirm_buy/confirm_profit/edit_entryがstate全体を上書きしても、
    既存のtrade_historyが引き継がれ続けることの確認(引き継ぎ漏れの回帰防止)。"""

    def test_trade_history_survives_subsequent_confirm_buy(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})
        self.assertEqual(len(self._trade_history()), 1)

        self._buy("BBB", 50.0, entry_date=self._calendar[0])
        self.assertEqual(len(self._trade_history()), 1)  # 消えていない
        self.assertIn("BBB", self._positions())

    def test_trade_history_survives_subsequent_confirm_profit(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})

        self._buy("BBB", 50.0, entry_date=self._calendar[0])
        self._records["BBB"] = {"price": 100.0}  # +50%以上でPROFIT対象
        self._post("/api/portfolio/confirm_profit", {"ticker": "BBB"})
        self.assertEqual(len(self._trade_history()), 1)  # 消えていない

    def test_trade_history_survives_subsequent_edit_entry(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})

        self._buy("BBB", 50.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/edit_entry", {"ticker": "BBB", "entry_price": 48.0})
        self.assertEqual(len(self._trade_history()), 1)  # 消えていない
        self.assertEqual(self._positions()["BBB"]["entry_price"], 48.0)

    def test_multiple_sells_accumulate_in_order(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 110.0})
        self._buy("BBB", 50.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/confirm_sell", {"ticker": "BBB", "exit_price": 55.0})

        history = self._trade_history()
        self.assertEqual([h["ticker"] for h in history], ["AAA", "BBB"])


class ExistingEditEntryUnaffectedTests(TradeHistoryTestCase):
    """既存の取得日・取得価格編集機能(edit_entry)との整合性: 売却前の訂正が
    trade_historyに正しく反映される(訂正後の値が記録される)ことの確認。"""

    def test_entry_correction_before_sell_is_reflected_in_trade_history(self):
        self._buy("AAA", 100.0, entry_date=self._calendar[0])
        self._post("/api/portfolio/edit_entry", {"ticker": "AAA", "entry_price": 90.0,
                                                   "entry_date": self._calendar[1]})
        self._post("/api/portfolio/confirm_sell", {"ticker": "AAA", "exit_price": 108.0})

        rec = self._trade_history()[0]
        self.assertEqual(rec["entry_price"], 90.0)
        self.assertEqual(rec["entry_date"], self._calendar[1])
        self.assertAlmostEqual(rec["return_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
