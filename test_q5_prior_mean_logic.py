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


if __name__ == "__main__":
    unittest.main()
