# -*- coding: utf-8 -*-
"""一時調査スクリプト(2026-10-28、読み取り専用)。

FORMについて、Twelve Data adjust="all"(分割+配当調整)が、既存の
Yahoo Finance(.q5_price_cache/FORM.json、分割+配当調整済みadjclose)に
どれだけ近づくかを、現在のadjust="splits"と比較する。

【絶対に守ること】
- backfill_new_ticker_prior_mean.py / twelvedata_client.py / refresh.py /
  quintile_logic.py / q5_universe_logic.py / priormean_incremental_logic.py
  は一切変更しない(importして呼ぶだけ)。
- twelvedata_client.ADJUSTはこのスクリプト内でモンキーパッチ(実行時に
  モジュール属性を一時的に上書き)するだけで、ファイル自体は一切変更しない。
- Redisへの書き込みは一切行わない(store.get_priormean_bounds_historyの
  読み取りのみ)。priormean:events:FORM/priormean:backfill_status:FORMは
  一切読み書きしない(本番のFORM結果はそのまま維持する)。
- Twelve Data呼び出しは合計3回だけ(SPY×1 + FORM×2〈splits/all〉)。

実行方法: python _tmp_form_adjust_diag.py
"""
import json
import os
import statistics

import quintile_logic as q
import priormean_incremental_logic as inc
import q5_prior_mean_logic as pml
import redis_store as store
import twelvedata_client as td

TICKER = "FORM"
ANCHOR = "2026-10-27"
LOCAL_DIR = "_tmp_form_adjust_diag_data"


def load_local(ticker):
    with open(os.path.join(LOCAL_DIR, f"{ticker}.json"), encoding="utf-8") as f:
        d = json.load(f)
    return d["dates"], d["closes"], d.get("volumes")


def compute_day0(dates, closes, volumes, spy_dates, spy_closes, bounds_history, reference_train):
    scores = {}
    for i in range(q.MIN_HISTORY_FOR_FULL_FEATURES - 1, len(dates)):
        raw = q.compute_features(
            dates[: i + 1], closes[: i + 1], (volumes[: i + 1] if volumes else None),
            spy_dates, spy_closes,
        )
        scores[dates[i]] = q.knn_predict_score(raw, reference_train)
    result = inc.backfill_ticker_day0_events(TICKER, dates, closes, scores, bounds_history)
    day0_dates = sorted(set(e["date"] for e in result["resolved_events"]) | set(result["unresolved_day0_dates"]))
    return result, day0_dates


def prior_stats_for(static_data, resolved_events):
    merged = inc.merge_reference_data(static_data, {TICKER: resolved_events})
    pm = pml.get_prior_mean(TICKER, ANCHOR, data=merged)
    ps = pml.get_prior_stats(TICKER, ANCHOR, data=merged)
    return pm, ps


def match_rate(yahoo_set, other_set, label, period_label):
    inter = yahoo_set & other_set
    yahoo_only = yahoo_set - other_set
    other_only = other_set - yahoo_set
    rate = (len(inter) / len(yahoo_set) * 100) if yahoo_set else None
    jaccard = (len(inter) / len(yahoo_set | other_set) * 100) if (yahoo_set | other_set) else None
    print(f"--- Yahoo vs {label} ({period_label}) ---")
    print(f"  Yahoo件数={len(yahoo_set)} {label}件数={len(other_set)} 一致={len(inter)}件")
    print(f"  Yahooのみ={len(yahoo_only)}件 {label}のみ={len(other_only)}件")
    print(f"  一致率(Yahoo基準, 一致/Yahoo件数)= {rate:.1f}%" if rate is not None else "  一致率=N/A")
    print(f"  Jaccard(一致/和集合)= {jaccard:.1f}%" if jaccard is not None else "")
    return {"inter": len(inter), "yahoo_only": len(yahoo_only), "other_only": len(other_only), "rate": rate, "jaccard": jaccard}


def main():
    api_key = os.environ["TWELVEDATA_API_KEY"]
    td_calls = 0

    bounds_history = store.get_priormean_bounds_history()
    assert bounds_history, "priormean:bounds_history が取得できません(Phase 0未投入?)"
    print(f"bounds_history: {len(bounds_history)}日分 {min(bounds_history)}~{max(bounds_history)}")

    static_data = pml.load_reference_data()
    reference_train = q.load_reference_data()

    # --- 1) ローカルYahoo(既存.q5_price_cache相当、一時転送分) ---
    yahoo_form_dates, yahoo_form_closes, yahoo_form_vol = load_local("FORM")
    yahoo_spy_dates, yahoo_spy_closes, _ = load_local("SPY")
    print(f"\n[Yahoo] FORM {len(yahoo_form_dates)}日分 {yahoo_form_dates[0]}~{yahoo_form_dates[-1]}")

    yahoo_result, yahoo_day0 = compute_day0(
        yahoo_form_dates, yahoo_form_closes, yahoo_form_vol,
        yahoo_spy_dates, yahoo_spy_closes, bounds_history, reference_train,
    )
    yahoo_pm, yahoo_ps = prior_stats_for(static_data, yahoo_result["resolved_events"])
    print(f"[Yahoo] day0_count={yahoo_result['day0_count']} resolved={len(yahoo_result['resolved_events'])} "
          f"unresolved={len(yahoo_result['unresolved_day0_dates'])}")
    print(f"[Yahoo] prior_n={yahoo_pm['n']} prior_mean={yahoo_pm['mean']} "
          f"median={yahoo_ps['median']} win_rate={yahoo_ps['win_rate']}")

    # --- 2) Twelve Data SPY(splits、既存デフォルト。FORM側の比較に共通で使う) ---
    td_spy_dates, td_spy_closes, _td_spy_vol, _lows, _attempts = td.fetch_daily_series(
        "SPY", api_key, outputsize=5000,
    )
    td_calls += 1
    print(f"\n[TD-SPY] {len(td_spy_dates)}日分 {td_spy_dates[0]}~{td_spy_dates[-1]}")

    # --- 3) Twelve Data FORM adjust="splits"(現状の本番フォールバックと同じ設定) ---
    original_adjust = td.ADJUST
    try:
        td.ADJUST = "splits"
        td_splits_dates, td_splits_closes, td_splits_vol, _lows, _attempts = td.fetch_daily_series(
            TICKER, api_key, outputsize=5000,
        )
        td_calls += 1
    finally:
        td.ADJUST = original_adjust
    print(f"[TD-splits] FORM {len(td_splits_dates)}日分 {td_splits_dates[0]}~{td_splits_dates[-1]}")

    splits_result, splits_day0 = compute_day0(
        td_splits_dates, td_splits_closes, td_splits_vol,
        td_spy_dates, td_spy_closes, bounds_history, reference_train,
    )
    splits_pm, splits_ps = prior_stats_for(static_data, splits_result["resolved_events"])
    print(f"[TD-splits] day0_count={splits_result['day0_count']} resolved={len(splits_result['resolved_events'])} "
          f"unresolved={len(splits_result['unresolved_day0_dates'])}")
    print(f"[TD-splits] prior_n={splits_pm['n']} prior_mean={splits_pm['mean']} "
          f"median={splits_ps['median']} win_rate={splits_ps['win_rate']}")
    print("  (本番実測との整合性チェック: 本番はDay0=328 resolved=325 prior_mean=0.5 median=0.4 win_rate=51.1 のはず)")

    # --- 4) Twelve Data FORM adjust="all"(分割+配当調整、候補) ---
    try:
        td.ADJUST = "all"
        td_all_dates, td_all_closes, td_all_vol, _lows, _attempts = td.fetch_daily_series(
            TICKER, api_key, outputsize=5000,
        )
        td_calls += 1
    finally:
        td.ADJUST = original_adjust
    print(f"[TD-all] FORM {len(td_all_dates)}日分 {td_all_dates[0]}~{td_all_dates[-1]}")

    all_result, all_day0 = compute_day0(
        td_all_dates, td_all_closes, td_all_vol,
        td_spy_dates, td_spy_closes, bounds_history, reference_train,
    )
    all_pm, all_ps = prior_stats_for(static_data, all_result["resolved_events"])
    print(f"[TD-all] day0_count={all_result['day0_count']} resolved={len(all_result['resolved_events'])} "
          f"unresolved={len(all_result['unresolved_day0_dates'])}")
    print(f"[TD-all] prior_n={all_pm['n']} prior_mean={all_pm['mean']} "
          f"median={all_ps['median']} win_rate={all_ps['win_rate']}")

    # --- 5) 一致率比較(全期間・共通期間) ---
    yahoo_set = set(yahoo_day0)
    splits_set = set(splits_day0)
    all_set = set(all_day0)

    print("\n==================== 全期間での比較 ====================")
    match_rate(yahoo_set, splits_set, "TD-splits", "全期間")
    match_rate(yahoo_set, all_set, "TD-all", "全期間")

    common_start = max(td_splits_dates[0], td_all_dates[0])
    yahoo_set_common = {d for d in yahoo_set if d >= common_start}
    splits_set_common = {d for d in splits_set if d >= common_start}
    all_set_common = {d for d in all_set if d >= common_start}
    print(f"\n==================== 共通期間({common_start}以降)での比較 ====================")
    match_rate(yahoo_set_common, splits_set_common, "TD-splits", "共通期間")
    match_rate(yahoo_set_common, all_set_common, "TD-all", "共通期間")

    print(f"\nTwelve Data呼び出し回数(このdiagnosticだけで消費したcredits)= {td_calls}")


if __name__ == "__main__":
    main()
