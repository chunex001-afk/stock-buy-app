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

実行方法: python build_q5_prior_mean_reference.py
"""
import json
from collections import defaultdict

SOURCE_PATH = (
    r"C:\Users\chune\AppData\Local\Temp\claude\C--Users-chune-stock-buy-app"
    r"\1ee90faf-d5af-43c3-b921-44f69a7aa5fe\scratchpad\q5_aiadj_ratio\danger_features.json"
)
OUTPUT_PATH = "q5_prior_mean_reference.json"

# Phase1〜3研究で確立した、個別銘柄のprior_meanを「参考情報として表示してよい」
# と判断する最低限の過去Q5 Day0件数(validation_ticker_reliability_q5メモリ参照、
# 集団プールではn>=3でも安定したが、個別銘柄単位の判断としてはn>=10を最低ライン
# として推奨、という研究結論をそのまま採用する)。
MIN_PRIOR_N = 10


def main():
    with open(SOURCE_PATH, encoding="utf-8") as f:
        all_events = json.load(f)

    events_by_ticker = defaultdict(list)
    for e in all_events:
        events_by_ticker[e["ticker"]].append({"date": e["date"], "ret20": e["ret20"]})
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
        "min_prior_n": MIN_PRIOR_N,
        "source": SOURCE_PATH,
        "generated_at": "2026-10-23",
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
