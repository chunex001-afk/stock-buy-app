# -*- coding: utf-8 -*-
"""prior_mean表示範囲の拡張(design 2026-10-24)のテスト。

既存のq5_prior_mean_logic.get_prior_mean(計算方法・基準データとも無変更)を
そのまま呼ぶだけの、以下2つの読み取り専用関数を検証する。
  - app._build_current_q5_prior_means: 現在Q5の銘柄(保有有無を問わない)
  - app._build_portfolio_view: 保有銘柄(新しく"prior_mean"フィールドを追加)

新しいQ5判定ロジック・新しいprior_mean計算ロジックは一切作っていないこと
(どちらの関数も、既存のq5_prior_mean_logic.get_prior_meanをmockで差し替えて
「呼ばれた引数」を検証するだけで、ロジック自体の再実装は無い)を重視する。

「①新たにDay0になった銘柄」は既存のdactionBuyCard経由で既に表示されている
(design 2026-10-23)ため、ここでは変更していない(test_daily_actions.pyの
既存テストでカバー済み)。
"""
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


def _row(ticker, history, status="ready"):
    return {"ticker": ticker, "price": 100.0, "quintile": {"status": status, "current_q": None, "history": history}}


def _ok_pm(mean, n=42):
    return {"status": "ok", "n": n, "mean": mean}


def _insufficient_pm(n=3):
    return {"status": "insufficient", "n": n, "mean": None}


# ---------------------------------------------------------------------------
# ② 現在Q5となっている銘柄(保有有無を問わない)
# ---------------------------------------------------------------------------

class BuildCurrentQ5PriorMeansTests(unittest.TestCase):
    def test_ticker_currently_q5_gets_prior_mean(self):
        rows = [_row("AAA", [{"date": CAL[10], "q": "Q5"}])]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4, 31)) as m:
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result, [{"ticker": "AAA", "prior_mean": _ok_pm(2.4, 31)}])
        # 「Q5になった日」ではなく、anchor_date(最新取引日)を基準日に使うこと
        m.assert_called_once_with("AAA", CAL[20])

    def test_ticker_currently_not_q5_is_excluded(self):
        rows = [_row("AAA", [{"date": CAL[10], "q": "Q4"}])]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result, [])

    def test_ticker_was_q5_but_dropped_out_is_excluded(self):
        history = [{"date": CAL[5], "q": "Q5"}, {"date": CAL[15], "q": "Q3"}]
        rows = [_row("AAA", history)]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result, [])

    def test_not_ready_status_is_excluded(self):
        rows = [_row("AAA", [{"date": CAL[10], "q": "Q5"}], status="pending")]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(2.4)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result, [])

    def test_negative_prior_mean_still_displayed(self):
        rows = [_row("AAA", [{"date": CAL[10], "q": "Q5"}])]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(-3.2, 15)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result[0]["prior_mean"]["mean"], -3.2)

    def test_prior_n_below_10_still_displayed_as_insufficient(self):
        rows = [_row("AAA", [{"date": CAL[10], "q": "Q5"}])]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_insufficient_pm(n=4)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(result[0]["prior_mean"], {"status": "insufficient", "n": 4, "mean": None})

    def test_multiple_current_q5_tickers_all_included_in_row_order(self):
        rows = [
            _row("AAA", [{"date": CAL[10], "q": "Q5"}]),
            _row("BBB", [{"date": CAL[5], "q": "Q4"}]),
            _row("CCC", [{"date": CAL[12], "q": "Q5"}]),
        ]
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)):
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual([r["ticker"] for r in result], ["AAA", "CCC"])

    def test_no_new_quintile_logic_invented_reuses_resolve_q_for_date(self):
        # _resolve_q_for_dateの前方補完(carry-forward)仕様をそのまま使って
        # いること(直近のエントリがanchor_dateより前でも、それが現在のQとして
        # 採用される)を確認する=既存ロジックの再利用の証跡。
        rows = [_row("AAA", [{"date": CAL[5], "q": "Q5"}])]  # anchor(=CAL[20])には履歴なし、直近がQ5
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(1.0)) as m:
            result = app._build_current_q5_prior_means(rows, CAL[20])
        self.assertEqual(len(result), 1)
        m.assert_called_once_with("AAA", CAL[20])


# ---------------------------------------------------------------------------
# ③ Day0で購入し、購入価格を入力している銘柄(portfolio holdings)
# ---------------------------------------------------------------------------

class BuildPortfolioViewPriorMeanTests(unittest.TestCase):
    def setUp(self):
        self.fake_pool_history = {"dates": CAL, "scores_by_date": {}}
        p1 = mock.patch.object(app.store, "get_pool_history", side_effect=lambda: self.fake_pool_history)
        p1.start()
        self.addCleanup(p1.stop)
        self.ticker_records = {}
        p2 = mock.patch.object(app.store, "get_ticker_record", side_effect=lambda t: self.ticker_records.get(t))
        p2.start()
        self.addCleanup(p2.stop)

    def _set_positions(self, positions):
        state = {"positions": positions, "last_updated": CAL[-1]}
        p = mock.patch.object(app.store, "get_portfolio_state", return_value=state)
        p.start()
        self.addCleanup(p.stop)

    def test_holding_with_entry_price_gets_prior_mean(self):
        self._set_positions({"AAA": {"entry_date": CAL[10], "entry_price": 50.0, "shares": None,
                                      "profit_taken": False}})
        self.ticker_records["AAA"] = {"price": 55.0}
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(4.1, 18)) as m:
            view = app._build_portfolio_view()
        holding = view["holdings"][0]
        self.assertEqual(holding["prior_mean"], _ok_pm(4.1, 18))
        # カットオフは「Day0=購入時のentry_date」であること(未来情報を使わない)
        m.assert_called_once_with("AAA", CAL[10])

    def test_existing_fields_unchanged_by_prior_mean_addition(self):
        self._set_positions({"AAA": {"entry_date": CAL[10], "entry_price": 50.0, "shares": 2.0,
                                      "profit_taken": False}})
        self.ticker_records["AAA"] = {"price": 60.0}
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", return_value=_ok_pm(4.1)):
            view = app._build_portfolio_view()
        holding = view["holdings"][0]
        self.assertEqual(holding["entry_price"], 50.0)
        self.assertEqual(holding["current_price"], 60.0)
        self.assertAlmostEqual(holding["gain_pct"], 20.0)
        self.assertEqual(holding["shares"], 2.0)
        self.assertAlmostEqual(holding["current_value"], 120.0)

    def test_no_entry_date_yields_insufficient_without_calling_logic(self):
        # entry_date=""(空文字、_trading_day_index等の既存ヘルパーがstr型を
        # 前提にしているため"" を使う。Noneは実運用では発生しない想定
        # (watchlist登録時に必ずentry_dateが設定される、test_watchlist_add_entry.py
        # 参照)だが、念のため「entry_dateが偽値」の経路を検証する。
        self._set_positions({"AAA": {"entry_date": "", "entry_price": 50.0, "shares": None,
                                      "profit_taken": False}})
        self.ticker_records["AAA"] = {"price": 55.0}
        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean") as m:
            view = app._build_portfolio_view()
        m.assert_not_called()
        self.assertEqual(view["holdings"][0]["prior_mean"], {"status": "insufficient", "n": 0, "mean": None})

    def test_empty_portfolio_returns_empty_holdings(self):
        self._set_positions({})
        view = app._build_portfolio_view()
        self.assertEqual(view["holdings"], [])

    def test_multiple_holdings_each_get_own_prior_mean(self):
        self._set_positions({
            "AAA": {"entry_date": CAL[5], "entry_price": 10.0, "shares": None, "profit_taken": False},
            "BBB": {"entry_date": CAL[15], "entry_price": 20.0, "shares": None, "profit_taken": False},
        })
        self.ticker_records["AAA"] = {"price": 11.0}
        self.ticker_records["BBB"] = {"price": 22.0}

        def fake_pm(ticker, cutoff):
            return _ok_pm(1.0) if ticker == "AAA" else _ok_pm(2.0)

        with mock.patch.object(app.q5_prior_mean_logic, "get_prior_mean", side_effect=fake_pm):
            view = app._build_portfolio_view()
        by_ticker = {h["ticker"]: h for h in view["holdings"]}
        self.assertEqual(by_ticker["AAA"]["prior_mean"]["mean"], 1.0)
        self.assertEqual(by_ticker["BBB"]["prior_mean"]["mean"], 2.0)


if __name__ == "__main__":
    unittest.main()
