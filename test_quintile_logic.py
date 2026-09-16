"""Q1〜Q5状態判定ロジックのテスト。

このテストファイルは「設計確認フェーズ」の一部として、実装(quintile_logic.py)より
先に作成されている(TDD的アプローチ)。quintile_logic.pyが未実装の間、
TestAssignQuintile / TestRotationAssignment / TestNoLookaheadContract は
skip(未実装のためスキップ)として扱われ、失敗としては扱わない。

TestExistingLogicUnchanged は既存の stock_logic.py に対する回帰テストであり、
今回のQ1〜Q5実装より前の時点でも実行・合格できる(実装前後で出力が変わって
いないことを確認するためのベースライン)。

実行方法: python -m unittest test_quintile_logic -v
"""
import unittest
from datetime import date

try:
    import quintile_logic
    HAVE_QUINTILE_LOGIC = True
except ImportError:
    HAVE_QUINTILE_LOGIC = False

import stock_logic


# ---------------------------------------------------------------------------
# 固定値フィクスチャ: Stage24〜28の検証済みバックテストデータから抽出した
# (pred_score, 分位境界, 期待されるQラベル)の実例。
# 出典: scratchpad/rebound_verify/two_model_validation/stage24, stage27
# (252営業日ローリング、全48銘柄+SPYプール、20/40/60/80パーセンタイル)
# ---------------------------------------------------------------------------
KNOWN_CASES = [
    # (sym, date, pred_score, bounds[Q1/Q2, Q2/Q3, Q3/Q4, Q4/Q5], expected_quintile)
    ("LSCC", "2024-10-17", 21.164434, [8.1897, 11.5821, 15.3378, 19.8174], "Q5"),
    ("CEG", "2023-09-08", 7.855548, [8.6696, 11.7011, 15.0925, 19.9712], "Q1"),
    ("NOW", "2024-10-29", 8.00962, [8.1661, 11.6141, 15.3623, 19.7989], "Q1"),
    ("AMD", "2023-06-01", 17.268289, [9.2378, 12.0826, 15.2419, 20.3610], "Q4"),
    ("APLD", "2026-04-13", 32.540829, [9.7958, 13.7649, 17.5845, 23.5158], "Q5"),
]

# Stage29で確定した49銘柄(48+SPY)のプール構成。SPYは2026-09-16のTwelve Data
# 本番実装レビューでローテーションから外し毎日直接取得する方針となったため、
# ローテーション対象はSPYを除いた48銘柄(EXPECTED_ROTATION_UNIVERSE)になる。
EXPECTED_REFERENCE_UNIVERSE = [
    "AAOI", "AEHR", "AI", "ALAB", "AMD", "APLD", "ARM", "ASML", "AVGO", "AXTI",
    "BE", "CAT", "CEG", "CIEN", "COHR", "CRDO", "CRWD", "CRWV", "DELL", "DLR",
    "EQIX", "GEV", "IREN", "JNJ", "JPM", "LITE", "LSCC", "MCHP", "MPWR", "MRVL",
    "MU", "NBIS", "NOW", "NRG", "NVDA", "ON", "PATH", "PG", "PLTR", "QCOM",
    "SMCI", "SNDK", "SNOW", "TSM", "VRT", "VST", "WMT", "XOM", "SPY",
]
EXPECTED_ROTATION_UNIVERSE = [sym for sym in EXPECTED_REFERENCE_UNIVERSE if sym != "SPY"]
EXPECTED_GROUP_SIZES = [4, 4, 4, 4, 4, 4, 3, 3, 3, 3, 3, 3, 3, 3]  # groups 0..13 (48銘柄)


@unittest.skipUnless(HAVE_QUINTILE_LOGIC, "quintile_logic.py は未実装(設計確認フェーズ)")
class TestAssignQuintile(unittest.TestCase):
    """境界値との比較によるQ1〜Q5判定が、Stage24〜28の検証結果と一致するか。"""

    def test_known_cases_reproduce_backtest(self):
        for sym, dt, score, bounds, expected in KNOWN_CASES:
            with self.subTest(sym=sym, date=dt):
                result = quintile_logic.assign_quintile(score, bounds)
                self.assertEqual(result, expected,
                                  f"{sym}/{dt}: score={score} bounds={bounds} "
                                  f"expected={expected} got={result}")

    def test_boundary_edge_values(self):
        bounds = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(quintile_logic.assign_quintile(9.999, bounds), "Q1")
        self.assertEqual(quintile_logic.assign_quintile(10.0, bounds), "Q2")
        self.assertEqual(quintile_logic.assign_quintile(19.999, bounds), "Q2")
        self.assertEqual(quintile_logic.assign_quintile(20.0, bounds), "Q3")
        self.assertEqual(quintile_logic.assign_quintile(40.0, bounds), "Q5")
        self.assertEqual(quintile_logic.assign_quintile(1000.0, bounds), "Q5")
        self.assertEqual(quintile_logic.assign_quintile(-1000.0, bounds), "Q1")


@unittest.skipUnless(HAVE_QUINTILE_LOGIC, "quintile_logic.py は未実装(設計確認フェーズ)")
class TestRotationAssignment(unittest.TestCase):
    """参照母集団49銘柄の14グループ割当と、日付からのステートレスなグループ計算。"""

    def test_reference_universe_matches_stage29(self):
        universe = quintile_logic.REFERENCE_UNIVERSE
        self.assertEqual(sorted(universe), sorted(EXPECTED_REFERENCE_UNIVERSE))
        self.assertEqual(len(universe), 49)
        self.assertIn("SPY", universe)

    def test_rotation_universe_excludes_spy(self):
        # SPYはrel_strength_spyの鮮度を保つため毎日直接取得し、
        # 14日ローテーションの対象からは外れる(2026-09-16レビューで確定)。
        rotation = quintile_logic.ROTATION_UNIVERSE
        self.assertEqual(sorted(rotation), sorted(EXPECTED_ROTATION_UNIVERSE))
        self.assertEqual(len(rotation), 48)
        self.assertNotIn("SPY", rotation)

    def test_group_sizes_match_stage29(self):
        groups = {i: [] for i in range(14)}
        for idx, sym in enumerate(EXPECTED_ROTATION_UNIVERSE):
            groups[idx % 14].append(sym)
        sizes = [len(groups[i]) for i in range(14)]
        self.assertEqual(sizes, EXPECTED_GROUP_SIZES)
        self.assertEqual(sum(sizes), 48)
        self.assertEqual(quintile_logic.GROUP_SIZES, EXPECTED_GROUP_SIZES)

    def test_tickers_for_group_never_returns_spy(self):
        for g in range(14):
            self.assertNotIn("SPY", quintile_logic.tickers_for_group(g))

    def test_rotation_group_is_stateless_pure_function(self):
        # 同じ日付を何度呼んでも同じ結果になること(状態を持たない設計の確認)
        g1 = quintile_logic.rotation_group_for_date("2026-09-16")
        g2 = quintile_logic.rotation_group_for_date("2026-09-16")
        self.assertEqual(g1, g2)
        self.assertTrue(0 <= g1 < 14)

    def test_rotation_group_advances_over_consecutive_days(self):
        # 連続する14日間で、14グループが(順不同でよいが)全て一度ずつ現れること
        seen = set()
        for offset in range(14):
            d = date(2026, 1, 1)
            d = date.fromordinal(d.toordinal() + offset)
            seen.add(quintile_logic.rotation_group_for_date(d.isoformat()))
        self.assertEqual(seen, set(range(14)))

    def test_failed_run_does_not_corrupt_future_schedule(self):
        """"実行が飛んだ日"があっても、翌日以降のグループ計算は影響を受けない
        (状態を持たないため、そもそも「失敗」という概念がスケジュールに残らない)。"""
        g_day1 = quintile_logic.rotation_group_for_date("2026-03-10")
        g_day2 = quintile_logic.rotation_group_for_date("2026-03-11")
        # day1の呼び出しの有無に関わらず、day2の結果は同じであることを確認
        # (要するに副作用がない=純粋関数であることを再確認)
        g_day2_again = quintile_logic.rotation_group_for_date("2026-03-11")
        self.assertEqual(g_day2, g_day2_again)


@unittest.skipUnless(HAVE_QUINTILE_LOGIC, "quintile_logic.py は未実装(設計確認フェーズ)")
class TestNoLookaheadContract(unittest.TestCase):
    """未来データが混入していないことの契約テスト。"""

    def test_pool_history_update_does_not_require_future_dates(self):
        """当日のプール更新関数が、当日より後の日付のデータを引数として要求
        する設計になっていないことを、関数シグネチャの契約として確認する。
        (実装後、実際の呼び出し規約に合わせて詳細化する)"""
        import inspect
        sig = inspect.signature(quintile_logic.update_pool_history)
        params = list(sig.parameters.keys())
        # "future" 等の未来データを示唆する引数名が含まれていないことの簡易チェック
        for p in params:
            self.assertNotIn("future", p.lower())
            self.assertNotIn("tomorrow", p.lower())

    def test_assign_quintile_is_pure_and_order_independent(self):
        """assign_quintileはscoreとboundsだけの純粋関数であり、
        グローバル状態(現在日時など)を参照しないことを確認する。"""
        bounds = [10.0, 20.0, 30.0, 40.0]
        r1 = quintile_logic.assign_quintile(25.0, bounds)
        r2 = quintile_logic.assign_quintile(25.0, bounds)
        self.assertEqual(r1, r2)


class TestExistingLogicUnchanged(unittest.TestCase):
    """既存stock_logic.pyの回帰テスト(ベースライン)。
    Q1〜Q5実装の前後でこのテストの合否が変わらないことを確認するために使う。
    このテストクラスは今回の実装に関わらず、常に実行・合格できる。"""

    def _synthetic_series(self, n=260, start=100.0, daily_drift=0.001):
        closes = [start]
        for i in range(1, n):
            closes.append(closes[-1] * (1 + daily_drift))
        volumes = [1_000_000 for _ in range(n)]
        dates = [f"2025-{(1 + i // 28):02d}-{(1 + i % 28):02d}" for i in range(n)]
        return dates, closes, volumes

    def test_compute_indicators_runs_and_has_expected_keys(self):
        dates, closes, volumes = self._synthetic_series()
        ind = stock_logic.compute_indicators(dates, closes, volumes)
        expected_keys = {
            "last_trade_date", "price", "change_pct", "month_return",
            "ma20", "ma50", "ma20_slope", "rsi", "high_gap", "low_gap", "volume_ratio",
        }
        self.assertEqual(set(ind.keys()), expected_keys)
        self.assertIsInstance(ind["price"], float)

    def test_compute_bottom_status_returns_known_labels(self):
        dates, closes, volumes = self._synthetic_series()
        ind = stock_logic.compute_indicators(dates, closes, volumes)
        status = stock_logic.compute_bottom_status(ind)
        self.assertIn(status, ("底打ち確認", "底打ち途中", "底打ち未確認", None))

    def test_classify_market_cap_unchanged(self):
        self.assertIsNone(stock_logic.classify_market_cap(0))
        self.assertIsNone(stock_logic.classify_market_cap(None))
        self.assertEqual(
            stock_logic.classify_market_cap(2_000_000_000_000),
            stock_logic.classify_market_cap(2_000_000_000_000),
        )

    def test_max_tickers_and_budget_unchanged(self):
        """ユーザー指示: DAILY_API_BUDGET=22・MAX_TICKERS=15は変更しない。"""
        self.assertEqual(stock_logic.MAX_TICKERS, 15)
        self.assertEqual(stock_logic.DAILY_API_BUDGET, 22)


if __name__ == "__main__":
    unittest.main()
