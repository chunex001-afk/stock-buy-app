# -*- coding: utf-8 -*-
"""任意売却を記録するUI(design 2026-10-21)の回帰テスト。

バックエンドAPI(/api/portfolio/confirm_sell)自体は今回変更していない
(既存のtest_trade_history.pyが引き続きカバーする)。このファイルは
app.HTML(埋め込みJS)側の変更が意図通りであることを、文字列内容の検証で
確認する(ブラウザでの実際の動作確認はこの変更に対して別途実施済み)。

確認観点:
- 保有銘柄一覧(renderPortfolioStatus)の「売却を記録」ボタン・フォームが、
  20営業日SELL判定(holdDays)に関係なく、保有銘柄すべてに対して生成される
  (dactionSellCard(日次アクション、Day20以上限定)とは別の生成経路)
- 売却フォームが既存の/api/portfolio/confirm_sellをそのまま呼んでいる
  (新しいAPIを追加していない)
- 売却フォームに株数(shares)の入力が含まれていない
- 「取得情報を編集」と「売却を記録」が同一の排他状態(pfOpenPanel)で管理され、
  同時に開けない構造になっている
- 旧実装の識別子(pfEditingTicker/pfToggleEdit/pfCancelEdit)が残っていない
  (リネーム漏れの回帰防止)
- dactionSellCard(既存の20営業日SELLカード)自体は変更されていない
"""
import unittest

import app


def _portfolio_status_body():
    start = app.HTML.index("function renderPortfolioStatus")
    end = app.HTML.index("function updateRanking")
    return app.HTML[start:end]


class ManualSellButtonAlwaysRenderedTests(unittest.TestCase):
    def test_sell_toggle_button_generated_unconditionally_per_holding(self):
        body = _portfolio_status_body()
        self.assertIn("売却を記録", body)
        # holdDays(20営業日SELL判定用の表示色分け)とは独立したボタン生成で
        # あることの確認: sellToggleBtnの生成行自体にholdDaysへの参照がない。
        sell_btn_line_start = body.index('const sellToggleBtn =')
        sell_btn_line_end = body.index("\n", sell_btn_line_start)
        self.assertNotIn("holdDays", body[sell_btn_line_start:sell_btn_line_end])

    def test_sell_form_calls_existing_confirm_sell_endpoint_only(self):
        body = _portfolio_status_body()
        self.assertIn("/api/portfolio/confirm_sell", app.HTML)
        start = app.HTML.index("async function saveManualSell")
        end = app.HTML.index("\n}", app.HTML.index("fetch(", start))
        func_body = app.HTML[start:end]
        self.assertIn("/api/portfolio/confirm_sell", func_body)
        self.assertNotIn("/api/portfolio/confirm_buy", func_body)
        self.assertNotIn("/api/portfolio/edit_entry", func_body)

    def test_sell_form_has_no_shares_input(self):
        body = _portfolio_status_body()
        sell_form_start = body.index("const sellForm =")
        sell_form_end = body.index('return `<div class="pfstatus-row">')
        sell_form_snippet = body[sell_form_start:sell_form_end]
        self.assertNotIn("shares", sell_form_snippet)
        self.assertNotIn("株数", sell_form_snippet)
        self.assertIn("売却価格", sell_form_snippet)
        self.assertIn("pfsell-date-", sell_form_snippet)


class PanelExclusivityTests(unittest.TestCase):
    def test_edit_and_sell_panels_share_one_exclusive_state(self):
        body = _portfolio_status_body()
        self.assertIn("pfOpenPanel", body)
        self.assertIn('pfOpenPanel.mode === "edit"', body)
        self.assertIn('pfOpenPanel.mode === "sell"', body)

    def test_old_single_purpose_edit_state_removed(self):
        # design 2026-10-20時点のpfEditingTicker単独変数・専用トグル関数は、
        # 排他制御のためpfOpenPanelに統合され、リネームされている。
        self.assertNotIn("pfEditingTicker", app.HTML)
        self.assertNotIn("pfToggleEdit(", app.HTML)
        self.assertNotIn("pfCancelEdit(", app.HTML)
        self.assertIn("pfTogglePanel(", app.HTML)
        self.assertIn("pfClosePanel(", app.HTML)


class ExistingSellCardUnaffectedTests(unittest.TestCase):
    """既存の20営業日SELLカード(dactionSellCard、design 2026-10-21フェーズ1で
    追加した売却価格・売却日入力)が今回のUI追加で変更されていないことの確認。"""

    def test_daction_sell_card_still_has_its_own_exit_inputs(self):
        start = app.HTML.index("function dactionSellCard")
        end = app.HTML.index("function dactionProfitCard")
        body = app.HTML[start:end]
        self.assertIn("daction-exitprice-", body)
        self.assertIn("daction-exitdate-", body)
        self.assertIn("confirmDailyAction('sell'", body)
        self.assertNotIn("shares", body)


if __name__ == "__main__":
    unittest.main()
