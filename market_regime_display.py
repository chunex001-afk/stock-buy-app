# -*- coding: utf-8 -*-
"""Q5ランキング画面に表示する「市場環境(SPY過熱度)」の参考情報(design
2026-10-27、Candidate C研究の表示専用実装)。

研究結果(本リポジトリ外のバックテスト、2026-10-27時点の最終結論): 「SPYが
SMA200を+10%以上上回っている間はQ5 Day0の新規購入を0%にする」というCandidate
Cは、2018〜2025年のOOSでは一貫してQ5無条件買いよりCAGR・最大DDを改善したが、
**2026年の完全ホールドアウト検証では明確に失敗した**(リターン・最大DDとも
Q5無条件買いを下回った)。そのため本番の自動判定には採用せず、「参考情報として
表示するだけ」の機能としてここに実装する。

【絶対に守ること】
- ここにある関数はいずれも、Q1〜Q5判定・Q5 Day0判定・BUY/SELL判定・Q5ランキング・
  prior_mean/win_rate・watchlistの並び順・Risk-off判定・switch_recommendation_
  logic・ポートフォリオ計算・資金配分のいずれにも一切使用しない。
- is_market_overheated()という関数を用意するが、Q1〜Q5判定・BUY判定・ランキング・
  スコア・フィルタ・資金配分からは絶対に呼び出さないこと(呼び出すのはapp.pyの
  表示専用ビュー組み立て関数だけ)。
- Redis/Twelve Dataへは一切アクセスしない純粋関数。spy_close/sma200は呼び出し側
  (app.py、既存のstore.get_riskoff_state()が既に持っている値)からそのまま渡す。
  新しい外部API取得は一切追加しない。
- 「購入禁止」「Q5購入禁止」「Q5危険」「買わない方がいい」「投資比率を下げる」
  等の勧誘的・指示的な文言は一切使わない。「市場が過熱ゾーンにある」という事実の
  表示のみに留める。

【定義】(研究で確定、変更しない)
過熱判定: spy_close / sma200 - 1 >= OVERHEAT_THRESHOLD (0.10 = 10%)。
3段階の表示区分(あくまで表示のバケツ分けであり、ELEVATED_THRESHOLDは新しい
売買ルールの閾値ではない。過熱判定そのものは常にOVERHEAT_THRESHOLD=10%のみ):
  deviation <  ELEVATED_THRESHOLD(5%)             : "normal"     🟢 市場環境：通常
  ELEVATED_THRESHOLD <= deviation < OVERHEAT_THRESHOLD : "elevated"   🟡 やや高め
  deviation >= OVERHEAT_THRESHOLD(10%)             : "overheated" ⚠️ 市場過熱
"""

OVERHEAT_THRESHOLD = 0.10  # Candidate C研究で確定した閾値(10%)。表示専用、変更しない。
ELEVATED_THRESHOLD = 0.05  # 3段階表示のための中間区分(表示のみ、売買ルールではない)。

DISCLAIMER = "参考情報｜Q5の購入判定には影響しません"
NOTE = (
    "過去検証ではQ5の個別イベントよりも、ポートフォリオ全体の市場環境に"
    "影響する可能性が確認されています。※参考情報であり、Q5の購入判定には"
    "使用していません。"
)

LEVEL_META = {
    "normal": {"emoji": "🟢", "label": "市場環境：通常"},
    "elevated": {"emoji": "🟡", "label": "やや高め"},
    "overheated": {"emoji": "⚠️", "label": "市場過熱"},
}


def is_market_overheated(spy_close, sma200, threshold=OVERHEAT_THRESHOLD):
    """Candidate Cの定義そのもの(spy_close/sma200 - 1 >= threshold)を判定する
    純粋関数。表示専用。

    【絶対に守ること】この関数の戻り値を、Q1〜Q5判定・Q5 Day0判定・BUY/SELL
    判定・Q5ランキング・スコア・フィルタ・資金配分のいずれにも使用しないこと。
    呼び出すのはbuild_market_regime_view()(表示専用)だけにする。

    データ不足(spy_close/sma200がNone、またはsma200が0)の場合はNoneを返す
    (架空の値で補完しない)。"""
    if spy_close is None or sma200 is None or sma200 == 0:
        return None
    return (spy_close / sma200 - 1) >= threshold


def classify_overheat_level(deviation_pct):
    """deviation_pct(%、例: +7.4)を3段階ラベル("normal"/"elevated"/
    "overheated")に変換するだけの表示専用関数。deviation_pctがNoneならNone。"""
    if deviation_pct is None:
        return None
    if deviation_pct >= OVERHEAT_THRESHOLD * 100:
        return "overheated"
    if deviation_pct >= ELEVATED_THRESHOLD * 100:
        return "elevated"
    return "normal"


def build_market_regime_view(spy_close, sma200):
    """app.pyの既存store.get_riskoff_state()が既に保持しているspy_close/sma200
    をそのまま渡して呼び出す、表示専用ビューの組み立て関数。

    【絶対に守ること】この関数の戻り値を、表示(renderMarketRegime相当のJS)
    以外のどの処理にも使わないこと。他の判定ロジック・API・ソート順には一切
    接続しない。

    データ不足(spy_close/sma200が無い、refresh.py未実行直後等)の場合はNone
    を返す(呼び出し側はパネル自体を表示しない。aimarketdeclineパネルと同じ
    既存の扱い方)。架空の0%等で補完しない。"""
    if spy_close is None or sma200 is None or sma200 == 0:
        return None
    deviation_pct = round((spy_close / sma200 - 1) * 100, 1)
    level = classify_overheat_level(deviation_pct)
    meta = LEVEL_META[level]
    return {
        "spy_close": spy_close,
        "sma200": sma200,
        "deviation_pct": deviation_pct,
        "level": level,
        "emoji": meta["emoji"],
        "label": meta["label"],
        "is_overheated": level == "overheated",
        "disclaimer": DISCLAIMER,
        "note": NOTE,
    }
