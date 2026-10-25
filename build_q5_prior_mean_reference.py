# -*- coding: utf-8 -*-
"""q5_prior_mean_reference.json を生成する一度限りの変換スクリプト。

Q5 Day0銘柄に追加表示する「Q5過去平均リターン」(design 2026-10-23、
ticker-reliability研究Phase1〜3で検証済みのprior_mean定義)が使う、
銘柄別Q5 Day0イベント履歴(date, ret20)を、研究セッションで既に計算済みの
danger_features.json(n=5848、48銘柄、2007-2026、ret20 = Day0から20営業日後の
リターン)から抜き出して固定(frozen)データとして保存する。

このスクリプトは本番コード(app.py/refresh.py/q5_prior_mean_logic.py)からは
一切importされない(build_quintile_reference_data.pyと同じ位置付け)。

Phase1〜3研究(validation_ticker_reliability_q5メモリ参照)で確立したprior_mean
の定義・MIN_PRIOR_N=10のしきい値を変更せずそのまま使う。新しい独自の定義は
作らない。

出典: 研究セッション(1ee90faf-d5af-43c3-b921-44f69a7aa5fe)のscratchpadに
既存のdanger_features.json(ticker/date/ret20を含む、全48銘柄のQ5 Day0
イベント一覧)。元データの取得・Q5判定ロジック自体はここでは一切再計算しない
(既存の計算結果をそのまま抜き出すだけ)。

2026-10-25(design: 中央値・勝率・平均最大DD追加)、同じ研究セッションscratchpad
のday0_events_full.json(各イベントのfwd_closes=Day0〜Day0+20営業日の21点の
日次終値)から、各イベントのmax_dd20(Day0終値を基準に、以降20営業日の終値の
うち最も下落した時点のリターン%、Day0自身は対象外・終値ベース)を追加で計算し、
events_by_tickerの各イベントに"max_dd20"フィールドとして追加する。既存の
date/ret20フィールドの値・個数は一切変更しない(実装前に、danger_features.json
由来の5,848件全件がday0_events_full.jsonに完全一致で存在し、fwd_closesが
全件21点揃っており、fwd_closesから再計算したret20が既存ret20と全件(5,848件、
最大誤差0.0)完全一致することを確認済み)。

実行方法: python build_q5_prior_mean_reference.py
"""
import json
from collections import defaultdict

SOURCE_PATH = (
    r"C:\Users\chune\AppData\Local\Temp\claude\C--Users-chune-stock-buy-app"
    r"\1ee90faf-d5af-43c3-b921-44f69a7aa5fe\scratchpad\q5_aiadj_ratio\danger_features.json"
)
# 2026-10-25追加: max_dd20計算用の日次終値パス(fwd_closes)の出典。
DRAWDOWN_SOURCE_PATH = (
    r"C:\Users\chune\AppData\Local\Temp\claude\C--Users-chune-stock-buy-app"
    r"\1ee90faf-d5af-43c3-b921-44f69a7aa5fe\scratchpad\q5_aiadj_ratio\day0_events_full.json"
)
OUTPUT_PATH = "q5_prior_mean_reference.json"

# Phase1〜3研究で確立した、個別銘柄のprior_meanを「参考情報として表示してよい」
# と判断する最低限の過去Q5 Day0件数(validation_ticker_reliability_q5メモリ参照、
# 集団プールではn>=3でも安定したが、個別銘柄単位の判断としてはn>=10を最低ライン
# として推奨、という研究結論をそのまま採用する)。
MIN_PRIOR_N = 10


def _max_dd20(fwd_closes):
    """Day0終値(fwd_closes[0])を基準に、以降20営業日の終値(fwd_closes[1:21])の
    うち最も下落した時点のリターン(%)。Day0自身はmax drawdownの判定対象に
    含めない(ユーザー確定定義、2026-10-25)。終値ベースであり日中安値は使わない。"""
    day0_close = fwd_closes[0]
    return min((c / day0_close - 1) * 100 for c in fwd_closes[1:21])


def main():
    with open(SOURCE_PATH, encoding="utf-8") as f:
        all_events = json.load(f)

    with open(DRAWDOWN_SOURCE_PATH, encoding="utf-8") as f:
        full_events = json.load(f)
    full_by_key = {(e["ticker"], e["date"]): e for e in full_events}

    events_by_ticker = defaultdict(list)
    for e in all_events:
        key = (e["ticker"], e["date"])
        full_e = full_by_key[key]
        assert len(full_e["fwd_closes"]) == 21, f"fwd_closes not 21 points: {key}"
        recomputed_ret20 = (full_e["fwd_closes"][-1] / full_e["fwd_closes"][0] - 1) * 100
        assert abs(recomputed_ret20 - e["ret20"]) < 0.01, f"ret20 mismatch: {key}"
        events_by_ticker[e["ticker"]].append({
            "date": e["date"],
            "ret20": e["ret20"],
            "max_dd20": round(_max_dd20(full_e["fwd_closes"]), 4),
        })
    for ticker in events_by_ticker:
        events_by_ticker[ticker].sort(key=lambda e: e["date"])

    dates = sorted(e["date"] for e in all_events)

    output = {
        "definition": (
            "各銘柄のQ5 Day0イベントにおける、Day0時点終値からDay0+20営業日後"
            "終値までのリターン(%)。ticker-reliability研究Phase1〜3"
            "(validation_ticker_reliability_q5メモリ)で検証済みのret20/"
            "prior_meanの定義と完全に同一。Q1〜Q5判定・Q5 Day0判定・BUY判定とは"
            "無関係の表示専用の過去実績統計である。"
        ),
        "max_dd20_definition": (
            "同じQ5 Day0イベントについて、Day0終値を基準に、以降20営業日の"
            "終値のうち最も下落した時点のリターン(%)。Day0自身は対象外、"
            "終値ベース(日中安値は使わない)。"
        ),
        "min_prior_n": MIN_PRIOR_N,
        "source": SOURCE_PATH,
        "drawdown_source": DRAWDOWN_SOURCE_PATH,
        "generated_at": "2026-10-25",
        "coverage": {
            "start_date": dates[0], "end_date": dates[-1],
            "n_events": len(all_events), "n_tickers": len(events_by_ticker),
        },
        "events_by_ticker": dict(events_by_ticker),
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"銘柄数: {len(events_by_ticker)}")
    print(f"総イベント数: {len(all_events)}")
    print(f"期間: {dates[0]} 〜 {dates[-1]}")
    print(f"出力: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
