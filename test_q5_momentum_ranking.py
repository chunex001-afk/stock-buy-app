# -*- coding: utf-8 -*-
"""「Q5勢い上位8」(app._compute_q5_momentum_ranking、2026-09-30追加、
2026-10-05に表示件数を上位5件→上位8件に変更)のテスト。

既存のq5_signal(8取引日有効期限)とthree_day_return/week_return
(stock_logic.compute_indicators)をそのまま読むだけの表示専用の純粋関数
であることを確認する。Q1〜Q5判定・pred_score・購入判定・既存のランキング
(stock_logic.sort_rows)・q5_signal自体の計算ロジックには一切触れていない。

Redis・Twelve Dataへは一切アクセスしない(すべてプレーンなdictで完結)。
"""
import unittest

import app


def _row(ticker, current_q, signal_status, three_day, week, status="ready"):
    quintile = {
        "status": status,
        "current_q": current_q,
        "current_q_label": current_q,
        "q5_signal": {"status": signal_status, "day0_date": "2026-08-10", "days_elapsed": 1}
        if signal_status is not None else None,
    }
    return {
        "ticker": ticker, "quintile": quintile,
        "three_day_return": three_day, "week_return": week,
    }


class FilterConditionTests(unittest.TestCase):
    def test_excludes_expired_signal(self):
        rows = [_row("EXP", "Q4", "expired", 5.0, 5.0)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_excludes_no_signal(self):
        rows = [_row("NEV", "Q3", None, 5.0, 5.0)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_excludes_pending_status(self):
        rows = [_row("PEND", "Q3", "active", 5.0, 5.0, status="pending")]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_excludes_non_positive_three_day_return(self):
        rows = [_row("A", "Q4", "active", 0.0, 5.0), _row("B", "Q4", "active", -1.0, 5.0)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_excludes_non_positive_week_return(self):
        rows = [_row("A", "Q4", "active", 5.0, 0.0), _row("B", "Q4", "active", 5.0, -1.0)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_excludes_missing_returns(self):
        rows = [_row("A", "Q4", "active", None, 5.0), _row("B", "Q4", "active", 5.0, None)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_includes_ok_status_current_q5(self):
        rows = [_row("CUR5", "Q5", "ok", 3.0, 4.0)]
        result = app._compute_q5_momentum_ranking(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "CUR5")

    def test_includes_active_status_regardless_of_current_q_level(self):
        """current_qを問わない(ユーザー確定仕様): Q1/Q2でもq5_signal=activeなら対象。"""
        rows = [
            _row("LOWQ", "Q1", "active", 2.0, 3.0),
            _row("LOWQ2", "Q2", "active", 2.0, 3.0),
        ]
        result = app._compute_q5_momentum_ranking(rows)
        tickers = {r["ticker"] for r in result}
        self.assertEqual(tickers, {"LOWQ", "LOWQ2"})


class RankingOrderTests(unittest.TestCase):
    def test_sorted_by_momentum_score_descending(self):
        rows = [
            _row("LOW", "Q4", "active", 1.0, 1.0),   # momentum 2.0
            _row("HIGH", "Q5", "ok", 8.2, 12.4),      # momentum 20.6
            _row("MID", "Q4", "active", 5.1, 10.3),   # momentum 15.4
        ]
        result = app._compute_q5_momentum_ranking(rows)
        self.assertEqual([r["ticker"] for r in result], ["HIGH", "MID", "LOW"])

    def test_tie_break_uses_higher_three_day_return(self):
        rows = [
            _row("A", "Q4", "active", 3.0, 7.0),   # momentum 10.0, 3day=3.0
            _row("B", "Q5", "ok", 6.0, 4.0),        # momentum 10.0, 3day=6.0
        ]
        result = app._compute_q5_momentum_ranking(rows)
        self.assertEqual([r["ticker"] for r in result], ["B", "A"])

    def test_truncates_to_top_8(self):
        rows = [_row(f"T{i}", "Q4", "active", float(i), float(i)) for i in range(1, 11)]
        result = app._compute_q5_momentum_ranking(rows)
        self.assertEqual(len(result), 8)
        # 最もmomentumが高い上位8件(T10..T3)が残っていること
        self.assertEqual(
            [r["ticker"] for r in result],
            ["T10", "T9", "T8", "T7", "T6", "T5", "T4", "T3"],
        )

    def test_empty_when_no_candidates(self):
        rows = [_row("A", "Q3", "expired", 5.0, 5.0), _row("B", "Q2", None, 3.0, 3.0)]
        self.assertEqual(app._compute_q5_momentum_ranking(rows), [])

    def test_momentum_score_field_present(self):
        rows = [_row("A", "Q5", "ok", 3.0, 4.5)]
        result = app._compute_q5_momentum_ranking(rows)
        self.assertAlmostEqual(result[0]["momentum_score"], 7.5)


class DisplayNoLongerShowsCurrentQTests(unittest.TestCase):
    """2026-09-30(2回目)の表示変更: 「🔥 Q5勢い上位8」から現在Q(Q1〜Q5)
    バッジを削除し、順位・銘柄・3日間・1週間の4項目のみ表示するようにした
    ことを確認する。判定ロジック(app._compute_q5_momentum_ranking)自体は
    無変更で、current_q/current_q_labelは引き続き返り値に含まれる
    (JS側の描画だけが変更対象)。"""

    def test_ranking_logic_still_returns_current_q_fields(self):
        rows = [_row("A", "Q5", "ok", 3.0, 4.5)]
        result = app._compute_q5_momentum_ranking(rows)
        # 判定ロジック自体は変更していないため、current_q/current_q_labelは
        # データとしては引き続き返る(表示に使わないだけ)。
        self.assertEqual(result[0]["current_q"], "Q5")
        self.assertEqual(result[0]["current_q_label"], "Q5")

    def test_js_render_no_longer_renders_qbadge_for_current_q(self):
        self.assertNotIn('QBADGE[m.current_q]', app.HTML)

    def test_js_render_still_shows_rank_ticker_three_day_week(self):
        # renderQ5Momentum関数の描画テンプレート断片が、順位・銘柄・3日間・
        # 1週間の4項目のみで構成されていることを確認する。
        start = app.HTML.index("function renderQ5Momentum")
        end = app.HTML.index("\n}", start)
        body = app.HTML[start:end]
        self.assertIn("q5momrank", body)
        self.assertIn("q5momticker", body)
        self.assertIn("m.three_day_return", body)
        self.assertIn("m.week_return", body)
        self.assertNotIn("qbadge", body)
        self.assertNotIn("current_q", body)


if __name__ == "__main__":
    unittest.main()
