# -*- coding: utf-8 -*-
"""β(ベータ)表示専用ロジックのテスト(2026-09-25追加)。

- beta_logic.compute_beta: 252営業日ローリングβの計算(先読みなし、
  データ不足時はNone、既知の共分散/分散比になることの確認)
- beta_logic.classify_beta: ユーザー確定仕様の4区分・境界値8ケース

Q1〜Q5判定(quintile_logic.py)には一切触れない、beta_logic.py単体のテスト。
"""
import unittest

import beta_logic


def _make_series(n, stock_rets, spy_rets):
    """始値100からstock_rets/spy_retsを順に適用した日足終値シリーズを作る。
    dates/closesはtwelvedata_client.fetch_daily_seriesと同じ形式(古い→新しい順)。"""
    dates = [f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
    stock_closes = [100.0]
    spy_closes = [100.0]
    for r in stock_rets:
        stock_closes.append(stock_closes[-1] * (1 + r))
    for r in spy_rets:
        spy_closes.append(spy_closes[-1] * (1 + r))
    return dates[: len(stock_closes)], stock_closes, dates[: len(spy_closes)], spy_closes


class ComputeBetaTests(unittest.TestCase):
    def test_beta_2x_leveraged_series(self):
        # 対象銘柄の日次リターンが常にSPYのちょうど2倍なら、
        # cov(2x, x)/var(x) = 2*var(x)/var(x) = 2.0 になるはず。
        window = beta_logic.BETA_WINDOW
        spy_rets = [0.001 * (1 if i % 2 == 0 else -1) for i in range(window)]
        stock_rets = [r * 2.0 for r in spy_rets]
        dates, closes, spy_dates, spy_closes = _make_series(window + 1, stock_rets, spy_rets)
        beta = beta_logic.compute_beta(dates, closes, spy_dates, spy_closes)
        self.assertIsNotNone(beta)
        self.assertAlmostEqual(beta, 2.0, places=6)

    def test_insufficient_history_returns_none(self):
        window = beta_logic.BETA_WINDOW
        # window未満(251件)のペアしか作れない場合はNone。
        spy_rets = [0.001] * (window - 1)
        stock_rets = [0.002] * (window - 1)
        dates, closes, spy_dates, spy_closes = _make_series(window, stock_rets, spy_rets)
        self.assertIsNone(beta_logic.compute_beta(dates, closes, spy_dates, spy_closes))

    def test_missing_or_empty_inputs_return_none(self):
        self.assertIsNone(beta_logic.compute_beta([], [], [], []))
        self.assertIsNone(beta_logic.compute_beta(None, None, None, None))
        self.assertIsNone(beta_logic.compute_beta(["2020-01-01"], [100.0], ["2020-01-01"], [100.0]))

    def test_only_uses_data_up_to_and_including_given_dates_no_lookahead(self):
        """呼び出し側が「今日までの」dates/closesだけを渡す前提を裏付けるテスト。
        直近1日だけ大きく動く外れ値を系列の末尾に置き、その日を含む/含まない
        で結果が変わることを確認する(=関数が将来日を先読みするのではなく、
        渡された系列の末尾までをそのまま使っていることの確認)。window+バッファ
        日分を用意し、末尾1件を落としてもwindow件のペアが確保できるようにする。"""
        window = beta_logic.BETA_WINDOW
        buffer_days = 5
        spy_rets = [0.001 if i % 2 == 0 else -0.001 for i in range(window + buffer_days)]
        stock_rets = list(spy_rets)
        stock_rets[-1] = 0.05  # 直近1日だけ大きく動く外れ値
        dates, closes, spy_dates, spy_closes = _make_series(window + buffer_days + 1, stock_rets, spy_rets)

        beta_full = beta_logic.compute_beta(dates, closes, spy_dates, spy_closes)
        beta_without_last_day = beta_logic.compute_beta(dates[:-1], closes[:-1], spy_dates, spy_closes)
        self.assertIsNotNone(beta_full)
        self.assertIsNotNone(beta_without_last_day)
        self.assertNotAlmostEqual(beta_full, beta_without_last_day, places=3)

    def test_zero_variance_spy_returns_none(self):
        window = beta_logic.BETA_WINDOW
        spy_rets = [0.0] * window
        stock_rets = [0.001] * window
        dates, closes, spy_dates, spy_closes = _make_series(window + 1, stock_rets, spy_rets)
        self.assertIsNone(beta_logic.compute_beta(dates, closes, spy_dates, spy_closes))


class ClassifyBetaTests(unittest.TestCase):
    """【テスト】に明記された8ケースをそのまま検証する。"""

    def test_case1_low(self):
        key, label = beta_logic.classify_beta(0.79)
        self.assertEqual(key, "low")
        self.assertEqual(label, "低ベータ")

    def test_case2_normal_lower_boundary(self):
        key, label = beta_logic.classify_beta(0.80)
        self.assertEqual(key, "normal")
        self.assertEqual(label, "標準ベータ")

    def test_case3_normal_upper_edge(self):
        key, label = beta_logic.classify_beta(1.29)
        self.assertEqual(key, "normal")
        self.assertEqual(label, "標準ベータ")

    def test_case4_high_lower_boundary(self):
        key, label = beta_logic.classify_beta(1.30)
        self.assertEqual(key, "high")
        self.assertEqual(label, "高ベータ")

    def test_case5_high_upper_edge(self):
        key, label = beta_logic.classify_beta(2.49)
        self.assertEqual(key, "high")
        self.assertEqual(label, "高ベータ")

    def test_case6_extreme_lower_boundary(self):
        key, label = beta_logic.classify_beta(2.50)
        self.assertEqual(key, "extreme")
        self.assertEqual(label, "超高ベータ")

    def test_case7_extreme_higher_value(self):
        key, label = beta_logic.classify_beta(3.00)
        self.assertEqual(key, "extreme")
        self.assertEqual(label, "超高ベータ")

    def test_case8_none_beta(self):
        key, label = beta_logic.classify_beta(None)
        self.assertIsNone(key)
        self.assertIsNone(label)

    def test_extreme_note_constant_is_not_no_difference(self):
        # 「Q1〜Q5の差なし」とは表示しない仕様の確認(定数そのものの文言確認)。
        self.assertEqual(beta_logic.EXTREME_BETA_NOTE, "Q1〜Q5の段階差は小さめ")
        self.assertNotIn("差なし", beta_logic.EXTREME_BETA_NOTE)


if __name__ == "__main__":
    unittest.main()
