# -*- coding: utf-8 -*-
"""ポートフォリオ記録(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
フェーズ1)のredis_store.get_portfolio_state/set_portfolio_stateのテスト。

既存のget_riskoff_state/set_riskoff_stateと同じ薄いラッパーであることを確認する。
Upstashへは一切アクセスしない(redis_store._requestをmockで差し替える、
実際のネットワーク境界だけを差し替えて、get_json/set_json/get/set_の実コードは
そのまま通す)。
"""
import json
import unittest
from unittest import mock

import redis_store as store


class PortfolioStateTests(unittest.TestCase):
    def test_get_portfolio_state_returns_none_when_unset(self):
        with mock.patch.object(store, "_request", return_value={"result": None}) as m:
            result = store.get_portfolio_state()
        self.assertIsNone(result)
        # 正しいキー(portfolio:state)を参照していることを確認
        args, kwargs = m.call_args
        self.assertIn("portfolio:state", args[1])

    def test_set_then_get_round_trip(self):
        """set_portfolio_stateが書き込んだJSONを、get_portfolio_stateが
        正しく読み戻せることを確認する(実際のUpstashの代わりに、
        set時に渡された値をそのまま保持する簡易fakeで検証)。"""
        stored = {}

        def fake_request(method, path, **kwargs):
            if method == "POST" and path.startswith("set/"):
                stored["value"] = kwargs["data"].decode("utf-8")
                return {"result": "OK"}
            if method == "GET" and path.startswith("get/"):
                return {"result": stored.get("value")}
            raise AssertionError(f"unexpected call: {method} {path}")

        data = {
            "positions": {
                "SNDK": {"entry_date": "2026-10-10", "entry_price": 142.5,
                         "profit_taken": False, "profit_taken_date": None}
            },
            "last_updated": "2026-10-16",
        }

        with mock.patch.object(store, "_request", side_effect=fake_request):
            ok = store.set_portfolio_state(data)
            self.assertTrue(ok)
            result = store.get_portfolio_state()

        self.assertEqual(result, data)

    def test_set_portfolio_state_uses_correct_key(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            store.set_portfolio_state({"positions": {}, "last_updated": "2026-10-16"})
        args, kwargs = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].startswith("set/portfolio:state"))

    def test_independent_from_other_state_keys(self):
        """portfolio:stateがriskoff:state/ai_market_env:state等と別キーである
        ことを確認する(既存データに一切触れないことの最低限の保証)。"""
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(path)
            return {"result": "OK"}

        with mock.patch.object(store, "_request", side_effect=fake_request):
            store.set_portfolio_state({"positions": {}, "last_updated": "2026-10-16"})
            store.get_riskoff_state()
            store.get_ai_market_env_state()

        self.assertTrue(any(c.startswith("set/portfolio:state") for c in calls))
        self.assertTrue(any(c.startswith("get/riskoff:state") for c in calls))
        self.assertTrue(any(c.startswith("get/ai_market_env:state") for c in calls))
        # それぞれ別キーであること(文字列として重複がない)
        self.assertEqual(len({"portfolio:state", "riskoff:state", "ai_market_env:state"}), 3)


if __name__ == "__main__":
    unittest.main()
