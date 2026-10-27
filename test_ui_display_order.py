# -*- coding: utf-8 -*-
"""表示順変更(design 2026-10-27、「🔔更新通知・銘柄追加・運用情報」ブロックを
「📊 保有銘柄のQ1〜Q5状態」の上に移動)が、意図通りの位置になっていることだけを
確認する表示専用テスト。app.HTML(generate_latest HTMLテンプレート文字列)を
直接検査するだけで、Flaskサーバーも外部アクセスも一切不要。

Q1〜Q5判定・Day0判定・BUY判定・prior_mean・watchlist・switch recommendation・
Risk-off・AI市場下落判定・市場過熱判定・購入判定・API取得処理のテストは、
既存の各テストファイル(test_q5_*.py / test_riskoff_state.py / test_switch_
recommendation.py / test_market_regime_display.py 等、いずれも無変更)が
引き続きカバーする。
"""
import unittest

import app


class DisplayOrderTests(unittest.TestCase):
    def test_push_notify_block_is_above_q1q5_title(self):
        html = app.HTML
        # 探すのは <div class="title">...</div> の方(ページの<title>タグには
        # 同じ文言が別途入っているため、class="title"付きで一意に特定する)。
        idx_push = html.index('id="pushNotifyBar"')
        idx_add_card = html.index('id="addBtn"')  # ＋追加（即時取得）ボタン
        idx_title = html.index('class="title">📊 保有銘柄のQ1〜Q5状態')
        self.assertLess(idx_push, idx_title, "pushNotifyBarはQ1〜Q5タイトルより上にあるべき")
        self.assertLess(idx_add_card, idx_title, "銘柄追加ブロックはQ1〜Q5タイトルより上にあるべき")

    def test_existing_panels_above_push_notify_block_unchanged(self):
        """riskoff/aimarketdecline/marketregime/dailyActions/portfolioStatus/
        currentQ5PriorMeans/comparePanelの相対順序は今回の変更対象外であり、
        引き続きpushNotifyBarより上にあること(移動ブロックの範囲外)。"""
        html = app.HTML
        idx_push = html.index('id="pushNotifyBar"')
        for panel_id in (
            'id="riskoff"', 'id="aimarketdecline"', 'id="marketregime"',
            'id="dailyActions"', 'id="portfolioStatus"',
            'id="currentQ5PriorMeans"', 'id="comparePanel"',
        ):
            with self.subTest(panel_id=panel_id):
                self.assertLess(html.index(panel_id), idx_push)

    def test_q5_list_and_momentum_still_below_title(self):
        """Q1〜Q5の実際の一覧(list/hero/q5momentum)はタイトルの下のまま
        (今回移動したのは追加・運用情報ブロックだけ)。"""
        html = app.HTML
        idx_title = html.index('class="title">📊 保有銘柄のQ1〜Q5状態')
        for el_id in ('id="q5momentum"', 'id="hero"', 'id="list"'):
            with self.subTest(el_id=el_id):
                self.assertGreater(html.index(el_id), idx_title)


if __name__ == "__main__":
    unittest.main()
