# -*- coding: utf-8 -*-
"""既存保有ポジションの取得情報(取得単価・取得日)訂正機能(design 2026-10-20)の
テスト。COHRの取得日が誤って10/16で登録され、正しくは10/19だった実際の
事例を受けて追加した/api/portfolio/edit_entryのテスト。

検証ポイント:
1. entry_price/entry_dateをそれぞれ単独・同時に訂正できる
2. entry_dateを訂正すると、既存の_trading_day_indexベースの保有期間計算・
   20営業日SELL判定が新しいentry_date起点で再計算される(ロジック自体は
   無変更、entry_dateを書き換えるだけで既存計算が追従することの確認)
3. entry_priceだけの訂正ではentry_dateが変わらず、entry_dateだけの訂正では
   entry_priceが変わらない
4. 不正な入力(0以下の価格・不正な日付形式・未来日・未保有銘柄・フィールド
   未指定)を拒否する
5. PROFIT履歴(profit_taken/profit_taken_date/profit_take_sold_shares/
   profit_take_sold_value/profit_take_price)が訂正によってリセットされない
6. 他の保有銘柄・既存のBUY/SELL/PROFIT確認APIの挙動には影響しない

Twelve Data・Redisへは一切アクセスしない(store側の該当関数のみmockで
差し替える)。
"""
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import app


def _business_days(n, start="2026-01-01"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


CAL = _business_days(60)


class EditEntryTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._portfolio_state = None
        self._records = {}

        def fake_get_portfolio_state():
            return self._portfolio_state

        def fake_set_portfolio_state(data):
            self._portfolio_state = data
            return True

        patches = [
            mock.patch.object(app.store, "get_portfolio_state", side_effect=fake_get_portfolio_state),
            mock.patch.object(app.store, "set_portfolio_state", side_effect=fake_set_portfolio_state),
            mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self._records.get(t)),
            mock.patch.object(app, "_trading_days_calendar", side_effect=lambda: CAL),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, body):
        resp = self.client.post("/api/portfolio/edit_entry", data=json.dumps(body),
                                 content_type="application/json")
        return resp, resp.get_json()

    def _positions(self):
        return (self._portfolio_state or {}).get("positions") or {}

    def _set_position(self, ticker, **fields):
        base = {"entry_date": CAL[-1], "entry_price": 100.0,
                "profit_taken": False, "profit_taken_date": None}
        base.update(fields)
        positions = dict(self._positions())
        positions[ticker] = base
        self._portfolio_state = {"positions": positions, "last_updated": CAL[-1]}


class EntryDateCorrectionTests(EditEntryTestCase):
    """COHRの実際の事例(10/16登録→正しくは10/19)を模した、取得日訂正が
    保有期間・20営業日SELL判定の起算日に反映されることの確認。"""

    def test_1_2_entry_date_can_be_corrected_and_entry_date_updates(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)

        resp, data = self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], CAL[new_idx])

    def test_3_holding_period_recalculated_from_new_entry_date(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)
        self._records["COHR"] = {"price": 130.0}

        # 訂正前: 25営業日経過
        view_before = app._build_portfolio_view()
        self.assertEqual(view_before["holdings"][0]["days_held"], 25)

        self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})

        # 訂正後: 新しいentry_date(10営業日前)起点で再計算される
        view_after = app._build_portfolio_view()
        self.assertEqual(view_after["holdings"][0]["entry_date"], CAL[new_idx])
        self.assertEqual(view_after["holdings"][0]["days_held"], 10)

    def test_4_sell_judgement_anchor_follows_new_entry_date(self):
        old_idx = len(CAL) - 1 - 25  # 25営業日 -> SELL対象(>=20)
        new_idx = len(CAL) - 1 - 10  # 10営業日 -> SELL対象外
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)
        self._records["COHR"] = {"price": 130.0}

        state_before = self._portfolio_state
        result_before = app._compute_daily_actions([], state_before, None, CAL[-1], CAL)
        self.assertEqual([s["ticker"] for s in result_before["sell"]], ["COHR"])
        self.assertEqual(result_before["sell"][0]["days_held"], 25)

        self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})

        state_after = self._portfolio_state
        result_after = app._compute_daily_actions([], state_after, None, CAL[-1], CAL)
        self.assertEqual(result_after["sell"], [])  # 新しい起点では20営業日未満

    def test_4b_sell_judgement_can_also_turn_on_after_correction(self):
        """反対方向(訂正後に取得日が古くなる)でも20営業日SELL判定の起算日が
        新entry_date追従することの確認(逆方向でも一貫していることの保証)。"""
        old_idx = len(CAL) - 1 - 10  # 10営業日 -> SELL対象外
        new_idx = len(CAL) - 1 - 25  # 25営業日 -> SELL対象
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)
        self._records["COHR"] = {"price": 130.0}

        result_before = app._compute_daily_actions([], self._portfolio_state, None, CAL[-1], CAL)
        self.assertEqual(result_before["sell"], [])

        self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})

        result_after = app._compute_daily_actions([], self._portfolio_state, None, CAL[-1], CAL)
        self.assertEqual([s["ticker"] for s in result_after["sell"]], ["COHR"])
        self.assertEqual(result_after["sell"][0]["days_held"], 25)


class CombinedAndIndependentFieldTests(EditEntryTestCase):
    def test_5_price_and_date_can_be_changed_together(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)

        resp, data = self._post({"ticker": "COHR", "entry_price": 110.0, "entry_date": CAL[new_idx]})
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 110.0)
        self.assertEqual(pos["entry_date"], CAL[new_idx])

    def test_6_price_only_change_leaves_entry_date_unchanged(self):
        old_idx = len(CAL) - 1 - 25
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)

        resp, data = self._post({"ticker": "COHR", "entry_price": 110.0})
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 110.0)
        self.assertEqual(pos["entry_date"], CAL[old_idx])

    def test_7_date_only_change_leaves_entry_price_unchanged(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)

        resp, data = self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 120.0)
        self.assertEqual(pos["entry_date"], CAL[new_idx])


class ValidationRejectionTests(EditEntryTestCase):
    def test_8_rejects_non_positive_price(self):
        self._set_position("COHR")
        resp, data = self._post({"ticker": "COHR", "entry_price": 0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        resp2, data2 = self._post({"ticker": "COHR", "entry_price": -5})
        self.assertEqual(resp2.status_code, 400)
        self.assertFalse(data2["ok"])
        # 値が書き換えられていないこと
        self.assertEqual(self._positions()["COHR"]["entry_price"], 100.0)

    def test_8b_rejects_non_numeric_price(self):
        self._set_position("COHR")
        resp, data = self._post({"ticker": "COHR", "entry_price": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_9_rejects_malformed_entry_date(self):
        self._set_position("COHR")
        resp, data = self._post({"ticker": "COHR", "entry_date": "not-a-date"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], CAL[-1])

    def test_10_rejects_future_entry_date(self):
        self._set_position("COHR")
        future = (date.fromisoformat(CAL[-1]) + timedelta(days=5)).isoformat()
        resp, data = self._post({"ticker": "COHR", "entry_date": future})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._positions()["COHR"]["entry_date"], CAL[-1])

    def test_rejects_unheld_ticker(self):
        resp, data = self._post({"ticker": "ZZZZ", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_rejects_empty_ticker(self):
        resp, data = self._post({"ticker": "", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_rejects_when_no_field_given(self):
        self._set_position("COHR")
        resp, data = self._post({"ticker": "COHR"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])

    def test_empty_string_fields_are_treated_as_not_given(self):
        self._set_position("COHR")
        resp, data = self._post({"ticker": "COHR", "entry_price": "", "entry_date": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])


class ProfitHistoryPreservedTests(EditEntryTestCase):
    def test_11_profit_history_survives_entry_date_correction(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position(
            "COHR", entry_date=CAL[old_idx], entry_price=100.0,
            profit_taken=True, profit_taken_date=CAL[old_idx + 5],
            profit_take_sold_shares=1.234, profit_take_sold_value=321.0,
            profit_take_price=150.0, shares=4.321,
        )

        resp, data = self._post({"ticker": "COHR", "entry_date": CAL[new_idx]})
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_date"], CAL[new_idx])
        self.assertTrue(pos["profit_taken"])
        self.assertEqual(pos["profit_taken_date"], CAL[old_idx + 5])
        self.assertEqual(pos["profit_take_sold_shares"], 1.234)
        self.assertEqual(pos["profit_take_sold_value"], 321.0)
        self.assertEqual(pos["profit_take_price"], 150.0)
        self.assertEqual(pos["shares"], 4.321)

    def test_11b_profit_history_survives_price_correction(self):
        old_idx = len(CAL) - 1 - 25
        self._set_position(
            "COHR", entry_date=CAL[old_idx], entry_price=100.0,
            profit_taken=True, profit_taken_date=CAL[old_idx + 5],
            profit_take_sold_shares=1.234, profit_take_sold_value=321.0,
            profit_take_price=150.0, shares=4.321,
        )

        resp, data = self._post({"ticker": "COHR", "entry_price": 95.0})
        self.assertTrue(data["ok"])
        pos = self._positions()["COHR"]
        self.assertEqual(pos["entry_price"], 95.0)
        self.assertTrue(pos["profit_taken"])
        self.assertEqual(pos["profit_taken_date"], CAL[old_idx + 5])
        self.assertEqual(pos["shares"], 4.321)


class OtherPositionsAndExistingFlowUnaffectedTests(EditEntryTestCase):
    def test_12_other_positions_untouched(self):
        old_idx = len(CAL) - 1 - 25
        new_idx = len(CAL) - 1 - 10
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=120.0)
        self._set_position("AAPL", entry_date=CAL[old_idx], entry_price=200.0)

        self._post({"ticker": "COHR", "entry_date": CAL[new_idx], "entry_price": 110.0})

        aapl = self._positions()["AAPL"]
        self.assertEqual(aapl["entry_date"], CAL[old_idx])
        self.assertEqual(aapl["entry_price"], 200.0)

    def test_12b_held_count_unchanged_by_edit(self):
        self._set_position("COHR")
        self._set_position("AAPL")
        self._post({"ticker": "COHR", "entry_price": 1.0})
        self.assertEqual(len(self._positions()), 2)

    def test_13_confirm_buy_sell_profit_still_work_after_edit(self):
        """今回追加したedit_entryが既存のconfirm_buy/confirm_profit/
        confirm_sellの挙動に影響していないことの回帰確認。"""
        old_idx = len(CAL) - 1 - 25
        self._set_position("COHR", entry_date=CAL[old_idx], entry_price=100.0)
        self._records["COHR"] = {"price": 160.0}  # +50%超のためPROFIT対象

        self._post({"ticker": "COHR", "entry_price": 90.0})  # 先に訂正しておく

        r1 = self.client.post("/api/portfolio/confirm_profit", data=json.dumps({"ticker": "COHR"}),
                               content_type="application/json")
        d1 = r1.get_json()
        self.assertTrue(d1["ok"])
        self.assertTrue(self._positions()["COHR"]["profit_taken"])
        # confirm_profitはentry_price/entry_dateを書き換えない(既存仕様)
        self.assertEqual(self._positions()["COHR"]["entry_price"], 90.0)

        r2 = self.client.post("/api/portfolio/confirm_sell", data=json.dumps({"ticker": "COHR"}),
                               content_type="application/json")
        d2 = r2.get_json()
        self.assertTrue(d2["ok"])
        self.assertNotIn("COHR", self._positions())


if __name__ == "__main__":
    unittest.main()
