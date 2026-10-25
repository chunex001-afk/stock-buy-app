# -*- coding: utf-8 -*-
"""watchlist全銘柄のprior_mean一覧(design 2026-10-25、「登録段階でprior_meanを
確認する」ための一覧)のテスト。

app._build_watchlist_prior_means は以下だけを行う読み取り専用関数であること
を検証する。
  - 既存のq5_prior_mean_logic.get_prior_mean/load_reference_data(計算方法・
    基準データとも無変更)をそのまま呼ぶだけで、新しいprior_mean計算ロジック・
    新しいQ5判定ロジックは一切作らない
  - 表示順はtickers引数(=watchlist登録順)の順序をそのまま維持する(並べ替え
    は一切行わない)
  - 現在Q5かどうかには関与しない(既存のapp._build_current_q5_prior_meansとは
    無関係に、tickers全件を対象にする)
"""
import unittest
from unittest import mock

import app


def _ok_pm(mean, n=42):
    return {"status": "ok", "n": n, "mean": mean}


def _insufficient_pm(n=3):
    return {"status": "insufficient", "n": n, "mean": None}


class BuildWatchlistPriorMeansTests(unittest.TestCase):
    def _mock_reference_data(self, events_by_ticker):
        return mock.patch.object(
            app.q5_prior_mean_logic, "load_reference_data",
            return_value={"events_by_ticker": events_by_ticker},
        )

    def test_all_tickers_included_regardless_of_q5_state(self):
        # _build_current_q5_prior_meansとは異なり、Q5状態を一切見ない
        # (rowsではなくticker文字列のリストだけを受け取る)。
        with self._mock_reference_data({"AAA": [], "BBB": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)):
            result = app._build_watchlist_prior_means(["AAA", "BBB"], "2026-10-20")
        self.assertEqual([r["ticker"] for r in result], ["AAA", "BBB"])

    def test_registration_order_preserved_not_sorted_by_mean(self):
        # ユーザー確定仕様: prior_mean降順ではなく、watchlist登録順をそのまま
        # 維持する。わざと降順と逆になる値を与えて、並び替えが発生しないことを
        # 確認する。
        means = {"AAA": _ok_pm(-5.0), "BBB": _ok_pm(10.0), "CCC": _ok_pm(2.0)}

        def fake_pm(ticker, cutoff, data=None):
            return means[ticker]

        with self._mock_reference_data({"AAA": [], "BBB": [], "CCC": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", side_effect=fake_pm):
            result = app._build_watchlist_prior_means(["AAA", "BBB", "CCC"], "2026-10-20")
        self.assertEqual([r["ticker"] for r in result], ["AAA", "BBB", "CCC"])

    def test_anchor_date_passed_through_unchanged(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)) as m:
            app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        # dataはload_reference_dataの戻り値そのものが渡されること(都度の
        # ファイル再読み込みを避けるための既存get_prior_mean(data=)引数の
        # 再利用)。
        args, kwargs = m.call_args
        self.assertEqual(args[0], "AAA")
        self.assertEqual(args[1], "2026-10-20")

    def test_negative_mean_is_passed_through_unchanged(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(-3.2, 15)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertEqual(result[0]["prior_mean"], {"status": "ok", "n": 15, "mean": -3.2})

    def test_insufficient_n_is_passed_through_unchanged(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_insufficient_pm(n=4)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertEqual(result[0]["prior_mean"], {"status": "insufficient", "n": 4, "mean": None})

    def test_ticker_in_reference_universe_is_flagged_true(self):
        with self._mock_reference_data({"AAA": [{"date": "2020-01-01", "ret20": 1.0}]}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertTrue(result[0]["in_reference_universe"])

    def test_ticker_not_in_reference_universe_is_flagged_false(self):
        # 48銘柄の参照データに存在しない銘柄(ユーザーが自由に追加した
        # ティッカー)は、get_prior_mean自体はinsufficient(n=0)を返すが、
        # それとは別に「そもそも対象外」であることを明示するフラグ。
        with self._mock_reference_data({"OTHER": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_insufficient_pm(n=0)):
            result = app._build_watchlist_prior_means(["ZZZ"], "2026-10-20")
        self.assertFalse(result[0]["in_reference_universe"])

    def test_empty_watchlist_returns_empty_list(self):
        with self._mock_reference_data({}):
            result = app._build_watchlist_prior_means([], "2026-10-20")
        self.assertEqual(result, [])

    def test_does_not_call_resolve_q_for_date_or_touch_rows(self):
        # 既存のQ1〜Q5判定(_resolve_q_for_date)には一切触れない(ticker文字列の
        # リストだけを受け取る関数であり、rows構造やquintile historyは不要)。
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)), \
             mock.patch.object(app, "_resolve_q_for_date") as resolve_mock:
            app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        resolve_mock.assert_not_called()

    # 9. watchlist表示にprior_statsが追加されること(design 2026-10-25)
    def test_prior_stats_field_added_alongside_prior_mean(self):
        fake_stats = {"status": "ok", "n": 31, "mean": 2.4, "median": 2.0,
                      "win_rate": 60.0, "max_dd_mean": -8.0, "max_dd_median": -7.0}
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4, 31)), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_stats", return_value=fake_stats) as stats_mock:
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertEqual(result[0]["prior_stats"], fake_stats)
        # prior_meanと同じticker/anchor_dateで呼ばれること(同一母集団の証跡)
        stats_mock.assert_called_once_with("AAA", "2026-10-20", data=mock.ANY)

    def test_prior_stats_insufficient_matches_prior_mean_insufficient(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_insufficient_pm(n=3)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        # get_prior_statsはmockしていない(本物のロジックが走る)。AAAは参照
        # データに存在しない(events=[])ため、自然にinsufficientになること。
        self.assertEqual(result[0]["prior_stats"]["status"], "insufficient")
        self.assertIsNone(result[0]["prior_stats"]["median"])

    def test_multiple_tickers_each_get_own_prior_stats(self):
        stats = {
            "AAA": {"status": "ok", "n": 20, "mean": 1.0, "median": 0.5, "win_rate": 55.0,
                    "max_dd_mean": -5.0, "max_dd_median": -4.0},
            "BBB": {"status": "ok", "n": 30, "mean": 2.0, "median": 1.5, "win_rate": 65.0,
                    "max_dd_mean": -6.0, "max_dd_median": -5.0},
        }

        def fake_stats(ticker, cutoff, data=None):
            return stats[ticker]

        with self._mock_reference_data({"AAA": [], "BBB": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_stats", side_effect=fake_stats):
            result = app._build_watchlist_prior_means(["AAA", "BBB"], "2026-10-20")
        by_ticker = {r["ticker"]: r for r in result}
        self.assertEqual(by_ticker["AAA"]["prior_stats"]["median"], 0.5)
        self.assertEqual(by_ticker["BBB"]["prior_stats"]["median"], 1.5)

    def test_existing_prior_mean_and_in_reference_universe_fields_unchanged(self):
        # 11. prior_stats追加後も既存のprior_mean/in_reference_universeの
        # 値・挙動が変わらないこと
        with self._mock_reference_data({"AAA": [{"date": "2020-01-01", "ret20": 1.0}]}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4, 31)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertEqual(result[0]["prior_mean"], _ok_pm(2.4, 31))
        self.assertTrue(result[0]["in_reference_universe"])
        self.assertIn("prior_stats", result[0])

    # Q5信頼度β(design 2026-10-25): prior_mean_labelがwatchlist表示にも
    # 追加されること、かつ本物のq5_prior_mean_logic.label_prior_mean
    # (mockしていない)がそのまま使われること。
    def test_prior_mean_label_added_and_matches_real_label_logic(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(3.2, 31)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertEqual(result[0]["prior_mean_label"], "🔥 最強")

    def test_prior_mean_label_none_when_insufficient(self):
        with self._mock_reference_data({"AAA": []}), \
             mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_insufficient_pm(n=4)):
            result = app._build_watchlist_prior_means(["AAA"], "2026-10-20")
        self.assertIsNone(result[0]["prior_mean_label"])


if __name__ == "__main__":
    unittest.main()
