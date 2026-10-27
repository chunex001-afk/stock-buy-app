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


# ---------------------------------------------------------------------------
# q5_prior_mean_logic.label_prior_mean のテスト(design 2026-10-25、「Q5信頼度β」)。
# prior_mean(平均)のみから決まる定性ラベル。median/win_rate/max_dd_meanは
# 一切使わない(新しい合成スコアは作らない、ユーザー確定仕様)ことを重視する。
# ---------------------------------------------------------------------------

class LabelPriorMeanTests(unittest.TestCase):
    def test_insufficient_status_returns_none(self):
        self.assertIsNone(pm.label_prior_mean({"status": "insufficient", "n": 3, "mean": None}))

    def test_none_input_returns_none(self):
        self.assertIsNone(pm.label_prior_mean(None))

    def test_negative_mean_is_weak_forbidden(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": -0.1}), "弱い・購入禁止")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": -10.0}), "弱い・購入禁止")

    def test_zero_to_just_under_3_is_standard(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 0.0}), "標準")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 2.9}), "標準")

    def test_3_to_just_under_4_5_is_strongest(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 3.0}), "🔥 最強")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 4.4}), "🔥 最強")

    def test_4_5_to_just_under_7_is_high_caution(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 4.5}), "⚠️ 高めだが要注意")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 6.9}), "⚠️ 高めだが要注意")

    def test_7_to_just_under_10_is_crash_risk_caution(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 7.0}), "⚠️ 暴落リスクに注意")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 9.9}), "⚠️ 暴落リスクに注意")

    def test_10_and_above_is_special_crash_risk_caution(self):
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 10.0}), "⚠️ 暴落リスクに特に注意")
        self.assertEqual(pm.label_prior_mean({"status": "ok", "n": 20, "mean": 50.0}), "⚠️ 暴落リスクに特に注意")

    def test_label_does_not_depend_on_median_win_rate_or_max_dd(self):
        # ユーザー確定の最重要制約: ラベルはprior_mean(mean)のみで決まり、
        # median/win_rate/max_dd_mean等の他フィールドの有無・値に一切影響されない。
        base = {"status": "ok", "n": 20, "mean": 5.0}
        with_extra = dict(base, median=-99.0, win_rate=0.0, max_dd_mean=-99.0)
        self.assertEqual(pm.label_prior_mean(base), pm.label_prior_mean(with_extra))


# ---------------------------------------------------------------------------
# q5_prior_mean_logic.label_prior_mean_with_win_rate のテスト(design
# 2026-10-26、「監視銘柄のQ5過去平均」パネル専用の新ラベル、勝率研究結果を
# 反映)。既存のlabel_prior_meanとは完全に独立した新規関数であり、上の
# LabelPriorMeanTestsには一切影響しない。
# ---------------------------------------------------------------------------

def _pm_ok(mean, n=100):
    return {"status": "ok", "n": n, "mean": mean}


def _stats_ok(win_rate, n=100):
    return {"status": "ok", "n": n, "mean": None, "median": None,
            "win_rate": win_rate, "max_dd_mean": None, "max_dd_median": None}


class LabelPriorMeanWithWinRateTests(unittest.TestCase):
    # --- 実例(本文記載の実測値に基づく) ---
    def test_aehr_like_positive_mean_but_low_win_rate_is_weak(self):
        # AEHR: +3.4%だが勝率44.7% → meanだけなら「🔥最強」だが、勝率が
        # 50%未満なので「🔴 弱い・購入優先度低」(今回の変更の主目的)。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(3.4), _stats_ok(44.7)),
            "🔴 弱い・購入優先度低",
        )

    def test_mxl_like_is_strongest(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(3.6), _stats_ok(57.4)),
            "🔥 最強",
        )

    def test_anet_like_is_excellent(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.9), _stats_ok(66.7)),
            "🟢 優良",
        )

    def test_panw_like_is_excellent(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.7), _stats_ok(62.1)),
            "🟢 優良",
        )

    def test_pltr_like_is_high_expectation_high_risk(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(6.2), _stats_ok(60.0)),
            "🔥 高期待・高リスク",
        )

    def test_crdo_like_is_high_expectation_high_risk(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(12.8), _stats_ok(66.7)),
            "🔥 高期待・高リスク",
        )

    def test_sndk_like_is_high_expectation_high_risk(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(19.5), _stats_ok(80.0)),
            "🔥 高期待・高リスク",
        )

    def test_nbis_like_is_high_expectation_high_risk(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(12.5), _stats_ok(59.1)),
            "🔥 高期待・高リスク",
        )

    def test_peng_like_negative_mean_is_weak(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(-0.6), _stats_ok(42.6)),
            "🔴 弱い・購入優先度低",
        )

    def test_mrvl_like_negative_mean_is_weak_even_if_win_rate_ok(self):
        # mean<0が最優先(優先順位1)なので、win_rate>=50でも弱いまま。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(-0.1), _stats_ok(50.3)),
            "🔴 弱い・購入優先度低",
        )

    # --- 境界値 ---
    def test_boundary_mean_0_with_high_win_rate_is_excellent(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(0.0), _stats_ok(55.0)),
            "🟢 優良",
        )

    def test_boundary_mean_3_with_high_win_rate_is_strongest(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(3.0), _stats_ok(55.0)),
            "🔥 最強",
        )

    def test_boundary_mean_just_under_3_with_high_win_rate_is_excellent(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.999), _stats_ok(55.0)),
            "🟢 優良",
        )

    def test_boundary_mean_5_with_high_win_rate_is_high_expectation(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(5.0), _stats_ok(55.0)),
            "🔥 高期待・高リスク",
        )

    def test_boundary_mean_just_under_5_with_high_win_rate_is_strongest(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(4.999), _stats_ok(55.0)),
            "🔥 最強",
        )

    def test_boundary_mean_5_with_low_win_rate_is_special_crash_risk(self):
        # 2026-10-27ユーザー確定でラベル文言のみ変更("⚠️ 暴落リスクに特に
        # 注意" → "⚠️ 暴落リスク有"、判定境界(mean>=5かつwin_rate<55)自体は
        # 無変更)。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(5.0), _stats_ok(54.999)),
            "⚠️ 暴落リスク有",
        )

    def test_boundary_mean_5_with_win_rate_just_under_50_is_crash_risk_not_weak(self):
        # 2026-10-27ユーザー確定の仕様変更本体: 以前はmean>=5でもwin_rate<50
        # なら(mean<0 or win_rate<50の条件が先に当たり)「🔴 弱い・購入優先度
        # 低」になっていたが、mean>=5の判定を最優先にしたことで、win_rateが
        # 50%未満であっても「🔴」にはならず「⚠️ 暴落リスク有」になる。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(5.0), _stats_ok(49.999)),
            "⚠️ 暴落リスク有",
        )

    def test_boundary_mean_5_with_win_rate_60_new_rule_cases(self):
        # 新ルールの境界値セット(今回のユーザー依頼に記載された4ケース)。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(5.0), _stats_ok(55.0)),
            "🔥 高期待・高リスク",
        )
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(5.0), _stats_ok(54.9)),
            "⚠️ 暴落リスク有",
        )
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(6.0), _stats_ok(54.0)),
            "⚠️ 暴落リスク有",
        )
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(6.0), _stats_ok(60.0)),
            "🔥 高期待・高リスク",
        )

    def test_boundary_win_rate_50_is_not_weak(self):
        # win_rate丁度50%は「<50」ではないので弱いラベルにはならない
        # (mean0-5%なら、win>=55でなければ標準になる)。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.0), _stats_ok(50.0)),
            "🟡 標準",
        )

    def test_boundary_win_rate_just_under_50_is_weak(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.0), _stats_ok(49.999)),
            "🔴 弱い・購入優先度低",
        )

    def test_boundary_win_rate_55_is_excellent_not_standard(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.0), _stats_ok(55.0)),
            "🟢 優良",
        )

    def test_boundary_win_rate_just_under_55_is_standard(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(2.0), _stats_ok(54.999)),
            "🟡 標準",
        )

    def test_mean_0_to_5_with_win_50_to_55_is_standard(self):
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(4.0), _stats_ok(52.0)),
            "🟡 標準",
        )

    # --- データ不足(prior_n<10相当)は既存のinsufficient扱いを維持 ---
    def test_prior_mean_insufficient_returns_none(self):
        insufficient_pm = {"status": "insufficient", "n": 9, "mean": None}
        self.assertIsNone(pm.label_prior_mean_with_win_rate(insufficient_pm, _stats_ok(60.0)))

    def test_prior_stats_insufficient_returns_none(self):
        insufficient_stats = {"status": "insufficient", "n": 9, "mean": None,
                               "median": None, "win_rate": None,
                               "max_dd_mean": None, "max_dd_median": None}
        self.assertIsNone(pm.label_prior_mean_with_win_rate(_pm_ok(3.4), insufficient_stats))

    def test_n_exactly_10_with_ok_status_is_evaluable(self):
        # prior_n=10(MIN_PRIOR_Nの境界)はstatus="ok"になる前提(get_prior_mean/
        # get_prior_statsの既存仕様どおり)なので、ラベルが正常に算出される。
        self.assertEqual(
            pm.label_prior_mean_with_win_rate(_pm_ok(3.4, n=10), _stats_ok(57.0, n=10)),
            "🔥 最強",
        )

    def test_none_prior_mean_returns_none(self):
        self.assertIsNone(pm.label_prior_mean_with_win_rate(None, _stats_ok(60.0)))

    def test_none_prior_stats_returns_none(self):
        self.assertIsNone(pm.label_prior_mean_with_win_rate(_pm_ok(3.4), None))

    def test_does_not_affect_existing_label_prior_mean(self):
        # 既存のlabel_prior_mean(prior_meanのみ)の挙動が、新関数の追加によって
        # 一切変わっていないことの回帰確認。
        self.assertEqual(pm.label_prior_mean(_pm_ok(3.4)), "🔥 最強")


# ---------------------------------------------------------------------------
# q5_prior_mean_logic.get_prior_stats_recent_years のテスト(design
# 2026-10-26、「全期間×直近5年prior_mean」研究結果を反映した新規関数)。
# get_prior_stats(全期間、無変更)と同じ母集団定義・未来データ混入防止
# ルールに、「day0_dateからN年以内」の期間制限を追加しただけであることを
# 重視する。
# ---------------------------------------------------------------------------

def _recent_data(events_by_ticker, min_prior_n=10):
    return {"min_prior_n": min_prior_n, "events_by_ticker": events_by_ticker}


class GetPriorStatsRecentYearsTests(unittest.TestCase):
    def test_sufficient_recent_history_returns_all_fields(self):
        # day0=2025-01-01の直近5年(2020-01-01以降)に10件、win_rate/median等
        # が全期間と同じロジックで計算されること。
        events = [
            {"date": f"2021-{m:02d}-01", "ret20": v, "max_dd20": -5.0}
            for m, v in zip(range(1, 11), [10.0] * 10)
        ]
        data = _recent_data({"AAA": events})
        result = pm.get_prior_stats_recent_years("AAA", "2025-01-01", data=data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["n"], 10)
        self.assertEqual(result["mean"], 10.0)
        self.assertEqual(result["win_rate"], 100.0)

    def test_fewer_than_10_in_recent_5_years_is_insufficient(self):
        # 直近5年以内に9件しかない場合、全期間側がokでもinsufficientになる。
        events = [
            {"date": f"2021-{m:02d}-01", "ret20": 10.0, "max_dd20": -5.0}
            for m in range(1, 10)
        ]
        data = _recent_data({"AAA": events})
        result = pm.get_prior_stats_recent_years("AAA", "2025-01-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 9)
        self.assertIsNone(result["mean"])

    def test_events_older_than_5_years_are_excluded(self):
        # day0=2025-06-01の5年前は2020-06-01。2019年のイベント(5年より前)は
        # 直近5年側の計算からは除外される一方、全期間側(get_prior_stats)には
        # 含まれる(=直近5年側だけ件数が減ってinsufficientになり得る)ことを
        # 確認する。
        old_events = [
            {"date": f"2015-{m:02d}-01", "ret20": -50.0, "max_dd20": -60.0}
            for m in range(1, 6)
        ]  # 5件、5年window外
        recent_events = [
            {"date": f"2022-{m:02d}-01", "ret20": 10.0, "max_dd20": -5.0}
            for m in range(1, 6)
        ]  # 5件、5年window内
        events = old_events + recent_events
        data = _recent_data({"AAA": events})

        full = pm.get_prior_stats("AAA", "2025-06-01", data=data)
        recent = pm.get_prior_stats_recent_years("AAA", "2025-06-01", data=data)

        self.assertEqual(full["n"], 10)  # 全期間は10件全て含む
        self.assertEqual(recent["n"], 5)  # 直近5年は5件だけ(window外を除外)
        self.assertEqual(recent["status"], "insufficient")  # 5件<MIN_PRIOR_N(10)

    def test_future_leakage_excludes_events_on_or_after_day0_date(self):
        events = [
            {"date": f"2024-{m:02d}-01", "ret20": 10.0, "max_dd20": -5.0}
            for m in range(1, 11)
        ] + [
            {"date": "2025-01-05", "ret20": 10.0, "max_dd20": -5.0},  # day0自身
            {"date": "2025-02-01", "ret20": -999.0, "max_dd20": -999.0},  # 未来
        ]
        data = _recent_data({"AAA": events})
        result = pm.get_prior_stats_recent_years("AAA", "2025-01-05", data=data)
        self.assertEqual(result["n"], 10)  # day0自身・未来は含まない
        self.assertEqual(result["mean"], 10.0)

    def test_zero_history_is_insufficient(self):
        data = _recent_data({"AAA": []})
        result = pm.get_prior_stats_recent_years("AAA", "2025-01-01", data=data)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["n"], 0)

    def test_does_not_affect_existing_get_prior_stats(self):
        # 既存のget_prior_stats(全期間)の結果が、新関数の追加によって一切
        # 変わっていないことの回帰確認。
        events = [
            {"date": f"2015-{m:02d}-01", "ret20": 5.0, "max_dd20": -3.0}
            for m in range(1, 11)
        ]
        data = _recent_data({"AAA": events})
        full = pm.get_prior_stats("AAA", "2025-01-01", data=data)
        self.assertEqual(full["status"], "ok")
        self.assertEqual(full["mean"], 5.0)

    def test_real_reference_data_recent_years_computable(self):
        # 実データ(本番で使うq5_prior_mean_reference.json)でのエンドツーエンド
        # 確認。MUは長期履歴銘柄のため直近5年でも十分なイベント数があるはず。
        data = pm.load_reference_data()
        result = pm.get_prior_stats_recent_years("MU", "2026-10-26", data=data)
        self.assertIn(result["status"], ("ok", "insufficient"))
        if result["status"] == "ok":
            self.assertIsNotNone(result["mean"])
            self.assertIsNotNone(result["win_rate"])


if __name__ == "__main__":
    unittest.main()
