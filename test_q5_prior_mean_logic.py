# -*- coding: utf-8 -*-
"""q5_prior_mean_logic.get_prior_mean のテスト(design 2026-10-23)。

Q5 Day0銘柄に追加表示する「Q5過去平均リターン」は、ticker-reliability研究
Phase1〜3(validation_ticker_reliability_q5メモリ)で検証済みのprior_mean定義
をそのまま使う、表示専用の参考情報。Redis/Twelve Dataへは一切アクセスしない
純粋関数なので、テスト用のダミーreference dataを直接渡して検証する。

特に重要: 未来情報(day0_date自身、またそれより後の同一銘柄イベント)が
絶対に混入しないこと、データ不足銘柄が0%等に補完されないことを検証する。
"""
import unittest

import q5_prior_mean_logic as pm


def _data(events_by_ticker, min_prior_n=10):
    return {"min_prior_n": min_prior_n, "events_by_ticker": events_by_ticker}


class GetPriorMeanTests(unittest.TestCase):
    def test_sufficient_history_returns_mean_and_n(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0} for i in range(1, 11)]
        data = _data({"AAA": events})
        result = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(result, {"status": "ok", "n": 10, "mean": 10.0})

    def test_fewer_than_min_prior_n_is_insufficient(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0} for i in range(1, 10)]  # 9件
        data = _data({"AAA": events})
        result = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 9)
        self.assertIsNone(result["mean"])

    def test_zero_history_is_insufficient_not_zero_percent(self):
        data = _data({"AAA": []})
        result = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(result, {"status": "insufficient", "n": 0, "mean": None})

    def test_unknown_ticker_is_insufficient(self):
        data = _data({"AAA": [{"date": "2020-01-01", "ret20": 10.0}]})
        result = pm.get_prior_mean("ZZZZ", "2020-02-01", data=data)
        self.assertEqual(result, {"status": "insufficient", "n": 0, "mean": None})

    def test_future_leakage_excludes_events_on_or_after_day0_date(self):
        """day0_dateそのもの、またそれより後の同一銘柄イベントは絶対に
        prior_meanの計算に混入させない(design絶対条件)。"""
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0} for i in range(1, 11)]  # 2020-01-01..10, 10件
        events_with_self_and_future = events + [
            {"date": "2020-01-10", "ret20": 10.0},   # day0_date自身(同日の別イベント想定)
            {"date": "2020-01-11", "ret20": -999.0},  # 未来
        ]
        data = _data({"AAA": events_with_self_and_future})
        result = pm.get_prior_mean("AAA", "2020-01-10", data=data)
        # 2020-01-10より厳密に過去のイベントは2020-01-01〜09の9件のみ
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 9)

    def test_future_crash_event_never_pulls_mean_down(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0} for i in range(1, 11)]
        events_with_future_crash = events + [{"date": "2020-02-01", "ret20": -999.0}]
        data = _data({"AAA": events_with_future_crash})
        result = pm.get_prior_mean("AAA", "2020-01-20", data=data)
        self.assertEqual(result, {"status": "ok", "n": 10, "mean": 10.0})

    def test_mean_is_rounded_to_1_decimal(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.33]
        events = [{"date": f"2020-01-{i:02d}", "ret20": v} for i, v in enumerate(vals, start=1)]
        data = _data({"AAA": events})
        result = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(result["mean"], 5.5)

    def test_negative_mean_displayed_as_is(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": -5.0} for i in range(1, 11)]
        data = _data({"AAA": events})
        result = pm.get_prior_mean("AAA", "2020-02-01", data=data)
        self.assertEqual(result, {"status": "ok", "n": 10, "mean": -5.0})

    def test_exactly_min_prior_n_is_ok_one_less_is_insufficient(self):
        events10 = [{"date": f"2020-01-{i:02d}", "ret20": 1.0} for i in range(1, 11)]
        data10 = _data({"AAA": events10})
        self.assertEqual(pm.get_prior_mean("AAA", "2020-02-01", data=data10)["status"], "ok")

        events9 = events10[:9]
        data9 = _data({"AAA": events9})
        self.assertEqual(pm.get_prior_mean("AAA", "2020-02-01", data=data9)["status"], "insufficient")


class LoadRealReferenceDataTests(unittest.TestCase):
    """実際に生成したq5_prior_mean_reference.jsonを使ったエンドツーエンドの
    健全性チェック。"""

    def test_reference_file_loads_and_has_expected_shape(self):
        data = pm.load_reference_data()
        self.assertIn("events_by_ticker", data)
        self.assertEqual(data.get("min_prior_n"), 10)
        self.assertGreaterEqual(len(data["events_by_ticker"]), 40)

    def test_first_ever_day0_for_a_ticker_has_zero_prior_history(self):
        data = pm.load_reference_data()
        sndk_dates = sorted(e["date"] for e in data["events_by_ticker"]["SNDK"])
        first_date = sndk_dates[0]
        result = pm.get_prior_mean("SNDK", first_date, data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 0)

    def test_unknown_ticker_not_in_universe_is_insufficient(self):
        result = pm.get_prior_mean("THIS_TICKER_DOES_NOT_EXIST", "2026-01-01")
        self.assertEqual(result, {"status": "insufficient", "n": 0, "mean": None})


# ---------------------------------------------------------------------------
# q5_prior_mean_logic.get_prior_stats のテスト(design 2026-10-25)。
# 中央値・勝率・平均最大DD・中央値最大DDを追加する、完全に独立した新規関数。
# get_prior_mean自体は上のテストクラスの通り無変更(このファイルの新規関数も
# 既存関数を一切呼ばない・変更しない)。
# ---------------------------------------------------------------------------

def _stats_data(events_by_ticker, min_prior_n=10):
    """get_prior_stats用のダミーreference data。各イベントはret20に加えて
    max_dd20を持つ(build_q5_prior_mean_reference.pyが生成時に計算済みの値を
    想定、ここでは任意の値を与える)。"""
    return {"min_prior_n": min_prior_n, "events_by_ticker": events_by_ticker}


class GetPriorStatsTests(unittest.TestCase):
    # 1. 正常系(n>=10、全フィールドが揃う)
    def test_sufficient_history_returns_all_fields(self):
        events = [
            {"date": f"2020-01-{i:02d}", "ret20": v, "max_dd20": d}
            for i, (v, d) in enumerate(
                zip([10.0] * 10, [-5.0] * 10), start=1
            )
        ]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["n"], 10)
        self.assertEqual(result["mean"], 10.0)
        self.assertEqual(result["median"], 10.0)
        self.assertEqual(result["win_rate"], 100.0)
        self.assertEqual(result["max_dd_mean"], -5.0)
        self.assertEqual(result["max_dd_median"], -5.0)

    # 2. median計算(偶数件・奇数件・外れ値に強いことを確認)
    def test_median_calculation(self):
        rets = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 1000.0]  # 外れ値1件
        events = [{"date": f"2020-01-{i:02d}", "ret20": r, "max_dd20": -1.0} for i, r in enumerate(rets, start=1)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        # 偶数件(10件)の中央値は中央2件(5.0, 6.0)の平均
        self.assertEqual(result["median"], 5.5)
        # 外れ値1件に引っ張られない(meanは引っ張られる対比を取る)
        self.assertGreater(result["mean"], result["median"])

    # 3. win_rate計算(ret20 > 0 の割合、0は勝ちに含めない)
    def test_win_rate_calculation(self):
        rets = [1.0, 2.0, 3.0, -1.0, -2.0, -3.0, -4.0, 0.0, 5.0, 6.0]  # 5勝(>0)/10件
        events = [{"date": f"2020-01-{i:02d}", "ret20": r, "max_dd20": -1.0} for i, r in enumerate(rets, start=1)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["win_rate"], 50.0)

    def test_win_rate_zero_return_does_not_count_as_win(self):
        rets = [0.0] * 10
        events = [{"date": f"2020-01-{i:02d}", "ret20": r, "max_dd20": -1.0} for i, r in enumerate(rets, start=1)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["win_rate"], 0.0)

    # 4. max_dd20自体の計算はbuild_q5_prior_mean_reference.py側(本ファイルでは
    # 保存済みのmax_dd20フィールドをそのまま使うだけ)。ここではDay0自身が
    # 判定対象に含まれていないこと(=保存値をそのまま信頼し、get_prior_stats
    # 側で変に補正しないこと)を、ゼロ値を含むケースで確認する。
    def test_max_dd20_values_are_used_as_stored(self):
        events = [
            {"date": "2020-01-01", "ret20": 10.0, "max_dd20": 0.0},   # 一度も下落しなかったケース
            {"date": "2020-01-02", "ret20": -30.0, "max_dd20": -40.0},
        ] + [{"date": f"2020-01-{i:02d}", "ret20": 5.0, "max_dd20": -2.0} for i in range(3, 11)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["n"], 10)
        expected_mean = round((0.0 + -40.0 + -2.0 * 8) / 10, 1)
        self.assertEqual(result["max_dd_mean"], expected_mean)

    # 5. max_dd_mean計算
    def test_max_dd_mean_calculation(self):
        dds = [-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0, -8.0, -9.0, -10.0]
        events = [{"date": f"2020-01-{i:02d}", "ret20": 1.0, "max_dd20": d} for i, d in enumerate(dds, start=1)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["max_dd_mean"], -5.5)

    # 6. max_dd_median計算
    def test_max_dd_median_calculation(self):
        dds = [-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0, -8.0, -9.0, -100.0]  # 外れ値1件
        events = [{"date": f"2020-01-{i:02d}", "ret20": 1.0, "max_dd20": d} for i, d in enumerate(dds, start=1)]
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["max_dd_median"], -5.5)
        self.assertLess(result["max_dd_mean"], result["max_dd_median"])  # 外れ値にmeanだけ引っ張られる

    # 7. date < day0_dateによる未来情報混入防止(get_prior_meanと同じ仕様)
    def test_future_leakage_excludes_events_on_or_after_day0_date(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0, "max_dd20": -1.0} for i in range(1, 11)]
        events_with_self_and_future = events + [
            {"date": "2020-01-10", "ret20": 10.0, "max_dd20": -1.0},     # day0_date自身
            {"date": "2020-01-11", "ret20": -999.0, "max_dd20": -999.0},  # 未来
        ]
        data = _stats_data({"AAA": events_with_self_and_future})
        result = pm.get_prior_stats("AAA", "2020-01-10", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 9)

    def test_future_crash_event_never_pulls_stats_down(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0, "max_dd20": -1.0} for i in range(1, 11)]
        events_with_future_crash = events + [{"date": "2020-02-01", "ret20": -999.0, "max_dd20": -999.0}]
        data = _stats_data({"AAA": events_with_future_crash})
        result = pm.get_prior_stats("AAA", "2020-01-20", data=data)
        self.assertEqual(result["mean"], 10.0)
        self.assertEqual(result["max_dd_mean"], -1.0)

    # 8. prior_n < 10(get_prior_meanと同じinsufficientの考え方)
    def test_fewer_than_min_prior_n_is_insufficient(self):
        events = [{"date": f"2020-01-{i:02d}", "ret20": 10.0, "max_dd20": -1.0} for i in range(1, 10)]  # 9件
        data = _stats_data({"AAA": events})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 9)
        self.assertIsNone(result["mean"])
        self.assertIsNone(result["median"])
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["max_dd_mean"])
        self.assertIsNone(result["max_dd_median"])

    def test_zero_history_is_insufficient(self):
        data = _stats_data({"AAA": []})
        result = pm.get_prior_stats("AAA", "2020-02-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 0)

    def test_unknown_ticker_is_insufficient(self):
        data = _stats_data({"AAA": [{"date": "2020-01-01", "ret20": 10.0, "max_dd20": -1.0}]})
        result = pm.get_prior_stats("ZZZZ", "2020-02-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 0)

    # get_prior_meanと完全に同じ母集団(prior)から算出されること
    def test_same_population_as_get_prior_mean(self):
        import random
        random.seed(1)
        events = [
            {"date": f"2020-{m:02d}-{d:02d}", "ret20": random.uniform(-20, 20), "max_dd20": -random.uniform(0, 30)}
            for m in range(1, 11) for d in (1, 15)
        ]
        data = _stats_data({"AAA": events})
        mean_result = pm.get_prior_mean("AAA", "2020-11-01", data=data)
        stats_result = pm.get_prior_stats("AAA", "2020-11-01", data=data)
        self.assertEqual(mean_result["n"], stats_result["n"])
        self.assertEqual(mean_result["mean"], stats_result["mean"])

    # 実データ(本番で使うq5_prior_mean_reference.json)でのエンドツーエンド確認。
    # 既存のget_prior_mean読み込みテスト(LoadRealReferenceDataTests)と同様、
    # 実ファイルにmax_dd20フィールドが存在し、get_prior_statsが動作すること。
    def test_real_reference_data_has_max_dd20_and_stats_computable(self):
        data = pm.load_reference_data()
        amd_dates = sorted(e["date"] for e in data["events_by_ticker"]["AMD"])
        last_date = amd_dates[-1]
        result = pm.get_prior_stats("AMD", last_date, data=data)
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["median"])
        self.assertIsNotNone(result["win_rate"])
        self.assertIsNotNone(result["max_dd_mean"])
        self.assertIsNotNone(result["max_dd_median"])
        # get_prior_meanの既存表示値と完全に一致すること(同一母集団の証跡)
        mean_result = pm.get_prior_mean("AMD", last_date, data=data)
        self.assertEqual(mean_result["mean"], result["mean"])
        self.assertEqual(mean_result["n"], result["n"])


if __name__ == "__main__":
    unittest.main()
