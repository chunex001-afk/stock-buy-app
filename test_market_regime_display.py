# -*- coding: utf-8 -*-
"""market_regime_display.py(design 2026-10-27、Candidate C研究の表示専用
実装)のテスト。

Redis/Twelve Dataへは一切アクセスしない純粋関数なので、スタブ値を直接渡して
検証する(他の*_logic.pyテストと同じ方針)。

Test1〜3: is_market_overheated()の閾値境界(10%)が正しいこと。
Test4〜7: 市場過熱状態を判定しても、既存のQ5 Day0判定・Q5ランキング(Q1〜Q5
判定)・prior_mean・watchlist/Redis結合のいずれにも影響が出ないこと
(=market_regime_display.pyが本当に「表示専用で独立している」ことの直接的な
証明。is_market_overheated()を呼ぶ前後で既存ロジックの出力が完全に同一である
ことを確認する)。
"""
import unittest

import market_regime_display as mrd
import q5_prior_mean_logic as pm
import q5_universe_logic
import quintile_logic


class IsMarketOverheatedThresholdTests(unittest.TestCase):
    """閾値10%の境界値テスト(Test1〜3、ユーザー指定の仕様通り)。"""

    def test_ratio_1_09_is_not_overheated(self):
        # spy_close/sma200 = 109/100 = 1.09 (+9%) -> False
        self.assertFalse(mrd.is_market_overheated(109.0, 100.0))

    def test_ratio_1_10_is_overheated(self):
        # spy_close/sma200 = 110/100 = 1.10 (+10%) -> True (境界含む)
        self.assertTrue(mrd.is_market_overheated(110.0, 100.0))

    def test_ratio_1_15_is_overheated(self):
        # spy_close/sma200 = 115/100 = 1.15 (+15%) -> True
        self.assertTrue(mrd.is_market_overheated(115.0, 100.0))

    def test_missing_data_returns_none_not_false(self):
        """データ不足を「過熱していない」と偽装しない(架空の値で補完しない)。"""
        self.assertIsNone(mrd.is_market_overheated(None, 100.0))
        self.assertIsNone(mrd.is_market_overheated(110.0, None))
        self.assertIsNone(mrd.is_market_overheated(110.0, 0.0))


class ClassifyOverheatLevelTests(unittest.TestCase):
    def test_three_tier_boundaries(self):
        self.assertEqual(mrd.classify_overheat_level(4.9), "normal")
        self.assertEqual(mrd.classify_overheat_level(5.0), "elevated")
        self.assertEqual(mrd.classify_overheat_level(9.9), "elevated")
        self.assertEqual(mrd.classify_overheat_level(10.0), "overheated")
        self.assertEqual(mrd.classify_overheat_level(-3.0), "normal")
        self.assertIsNone(mrd.classify_overheat_level(None))


class BuildMarketRegimeViewTests(unittest.TestCase):
    def test_overheated_view_has_disclaimer_and_no_forbidden_wording(self):
        view = mrd.build_market_regime_view(112.0, 100.0)
        self.assertEqual(view["level"], "overheated")
        self.assertTrue(view["is_overheated"])
        self.assertIn("影響しません", view["disclaimer"])
        forbidden = ["購入禁止", "購入停止", "買わない", "危険", "投資比率を下げ"]
        full_text = view["label"] + view["note"] + view["disclaimer"]
        for word in forbidden:
            self.assertNotIn(word, full_text, f"禁止表現 '{word}' が含まれている")

    def test_normal_view(self):
        view = mrd.build_market_regime_view(103.0, 100.0)
        self.assertEqual(view["level"], "normal")
        self.assertFalse(view["is_overheated"])
        self.assertEqual(view["emoji"], "🟢")

    def test_missing_data_returns_none(self):
        self.assertIsNone(mrd.build_market_regime_view(None, None))


class NoCouplingWithExistingLogicTests(unittest.TestCase):
    """Test4〜7: 市場過熱判定を呼んでも既存ロジックの挙動が一切変わらないこと。"""

    def test_4_day0_detection_unaffected_by_overheat_check(self):
        prev_q = {"AAA": "Q4", "BBB": "Q5"}
        today_q = {"AAA": "Q5", "BBB": "Q5"}
        before = q5_universe_logic.detect_day0_tickers(prev_q, today_q)

        # 市場過熱判定を呼ぶ(表示専用、Day0判定には絶対に関与しない)
        mrd.is_market_overheated(115.0, 100.0)
        mrd.build_market_regime_view(115.0, 100.0)

        after = q5_universe_logic.detect_day0_tickers(prev_q, today_q)
        self.assertEqual(before, after)
        self.assertEqual(before, ["AAA"])

    def test_5_quintile_ranking_unaffected_by_overheat_check(self):
        bounds = [10, 20, 30, 40]
        scores = [5, 15, 25, 35, 45]
        before = [quintile_logic.assign_quintile(s, bounds) for s in scores]

        mrd.is_market_overheated(120.0, 100.0)  # 強い過熱状態を判定させてみる

        after = [quintile_logic.assign_quintile(s, bounds) for s in scores]
        self.assertEqual(before, after)
        self.assertEqual(before, ["Q1", "Q2", "Q3", "Q4", "Q5"])

    def test_6_prior_mean_unaffected_by_overheat_check(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0} for i in range(1, 11)]
        data = {"min_prior_n": 10, "events_by_ticker": {"AAA": events}}
        before = pm.get_prior_mean("AAA", "2020-02-01", data=data)

        mrd.build_market_regime_view(130.0, 100.0)  # かなり強い過熱状態

        after = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(before, after)

    def test_7_module_has_no_redis_or_watchlist_coupling(self):
        """market_regime_display.pyはRedis/Twelve Data/watchlist並び順の
        どのロジックにも結合していないこと(静的なimportチェック)。これにより
        watchlistの並び順(Redisに保存された既存の順序そのもの)が、この
        モジュールの呼び出しによって変化し得ないことを保証する。"""
        import inspect

        src = inspect.getsource(mrd)
        for forbidden_import in ("redis_store", "twelvedata_client", "import app"):
            self.assertNotIn(forbidden_import, src)


if __name__ == "__main__":
    unittest.main()
