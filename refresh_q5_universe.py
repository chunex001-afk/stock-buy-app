# -*- coding: utf-8 -*-
"""Q5 48銘柄(quintile_logic.ROTATION_UNIVERSE)の将来成績をforward-onlyで
蓄積する専用バッチ(design 2026-10-21)。

既存のrefresh.py(日次本体バッチ、daily-refresh.yml)とは完全に分離した
別のGitHub Actionsから1日1回呼ばれる想定。既存のQ1〜Q5判定・BUY/SELL判定・
+50%利確・Risk-off・AI一斉調整・portfolio:state・trade_history・watchlist
には一切書き込まない(読み取りも、本体が今日のデータまで処理済みかを確認
するためのquintile:pool_history読み取りのみ)。

【実行の安全策(API credits消費ゼロでスキップできる仕組み)】
起動直後、Twelve Dataを1回も呼ぶ前に、quintile:pool_historyの最終日と、
このジョブが前回確定処理した日付(q5universe:state.last_processed_date)を
比較する。本体refresh.pyがまだ今日の分を処理していない(pool_historyが
進んでいない)、またはこのジョブ自身が今日すでに処理済みの場合は、
Twelve Dataを一切呼ばずに終了する(cron時刻だけに頼らず、本体の処理状況
を見て安全に判断する、design確定方針)。

【完全性(all-or-nothing)】
48銘柄のうち1件でも取得失敗・レート制限・データ不足・取引日不一致が
あった場合、その日の処理全体を中止し、Redisには一切書き込まない
(prev_q・entries・last_processed_dateのいずれも更新しない)。次回実行時に
同じ日付から安全に再試行できる。Redisへの書き込みは、48銘柄全件の取得・
判定が完全に成功した後、最後に1回だけ行う(「全件取得・判定→検証→
一括確定」、途中状態を書き残さない)。

このジョブ自体はSPYを取得しない(rel_strength_spy特徴量はNoneのまま、
quintile_logic側の既存のTRAIN中央値補完に委ねる)。これにより48銘柄の
取得失敗点を増やさない(design上の選択、既存のrel_strength_spy欠損時の
扱いを変更するものではない)。

【day5/20/60チェックポイントの遡及補完(design 2026-10-21修正)】
day0からちょうど5/20/60営業日目の取引日そのものを評価対象とする。この
ジョブがちょうどその営業日に実行できず後日(翌日以降)に復旧した場合でも、
「今日の価格で代用」はしない。各ティッカーの当日取得分(outputsize分の
過去OHLC、追加のAPI呼び出し不要)に含まれる日付→終値から、該当する
チェックポイント日そのものの実際の終値を探して確定する(q5_universe_logic.
fill_checkpoints参照)。対象日が取得した履歴の範囲外で見つからない場合は
nullのまま次回以降に委ねる。day0の判定自体は従来どおりリアルタイム
(その日時点で分かる情報だけ)で行い、day0_date/day0_priceは一度確定したら
変更しない。
"""
import os
import sys

import q5_universe_logic as q5logic
import quintile_logic
import redis_store as store
import refresh  # 既存のremaining_td_budget・_td_fetch_oneを再利用するだけで、
                 # refresh.py自体は一切変更しない。


def main():
    api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    if not api_key:
        print("[Q5UNIV][ERROR] TWELVEDATA_API_KEYが未設定のため終了します。")
        return 1

    pool_history = store.get_pool_history()
    if not pool_history or not pool_history.get("dates"):
        print("[Q5UNIV][SKIP] quintile:pool_historyが未初期化のため終了します"
              "(本体の日次更新がまだ一度も成功していません。Twelve Dataは呼んでいません)。")
        return 0

    trading_calendar = sorted(pool_history["dates"])
    today = trading_calendar[-1]

    state = store.get_q5universe_state() or {}
    prev_q = dict(state.get("prev_q") or {})
    entries = list(state.get("entries") or [])
    last_processed_date = state.get("last_processed_date")

    if last_processed_date == today:
        print(f"[Q5UNIV][SKIP] {today}分は既に処理済みのため終了します"
              "(Twelve Dataは呼んでいません、同日の重複実行を防止)。")
        return 0

    bounds = quintile_logic.pool_percentile_bounds(pool_history)
    if bounds is None:
        print("[Q5UNIV][SKIP] 分位境界(pool_percentile_bounds)を計算できないため終了します"
              "(Twelve Dataは呼んでいません)。")
        return 0

    # --- 48銘柄全件取得・判定(完全性必須、1件でも失敗したら中止) ---
    today_q = {}
    prices_by_ticker = {}
    # close_by_date_by_ticker: 当日の取得(outputsize分の過去OHLC)に含まれる
    # 日付→終値のマップ。day5/20/60のチェックポイントを埋める際、「今日の
    # 価格で代用」せず、本来のチェックポイント日そのものの実際の終値を
    # ここから遡って引くために使う(design 2026-10-21修正)。追加のAPI呼び
    # 出しは発生しない(1回の取得で既に含まれているデータを使うだけ)。
    close_by_date_by_ticker = {}
    for ticker in quintile_logic.ROTATION_UNIVERSE:
        budget_left, _ = refresh.remaining_td_budget()
        if budget_left <= 0:
            print("[Q5UNIV][ABORT] Twelve Data予算切れのため本日の処理を中止します"
                  "(prev_q/entries/last_processed_dateのいずれも更新しません)。")
            return 1

        result, hit_rate_limit, error_type, message, _lows = refresh._td_fetch_one(ticker, api_key, today)
        if result is None:
            print(f"[Q5UNIV][ABORT] {ticker}の取得に失敗したため本日の処理を中止します"
                  f"({error_type}: {message})。")
            return 1

        dates, closes, volumes = result
        if dates[-1] != today:
            print(f"[Q5UNIV][ABORT] {ticker}の最終取引日({dates[-1]})が本日({today})と"
                  "一致しないため中止します(48銘柄全てが同じ取引日である必要があります)。")
            return 1
        if not quintile_logic.has_min_history_for_quintile(len(closes)):
            print(f"[Q5UNIV][ABORT] {ticker}の履歴が不足しているため中止します"
                  "(取得失敗をQ5以外として扱わず、判定不能として全体を中止します)。")
            return 1

        try:
            features = quintile_logic.compute_features(dates, closes, volumes)
            score = quintile_logic.knn_predict_score(features)
        except Exception as e:
            print(f"[Q5UNIV][ABORT] {ticker}の特徴量/スコア計算に失敗したため中止します: {e}")
            return 1

        today_q[ticker] = quintile_logic.assign_quintile(score, bounds)
        prices_by_ticker[ticker] = closes[-1]
        close_by_date_by_ticker[ticker] = dict(zip(dates, closes))

        if hit_rate_limit:
            print("[Q5UNIV][ABORT] Twelve DataがRATE_LIMITを返したため中止します。")
            return 1

    if len(today_q) != len(quintile_logic.ROTATION_UNIVERSE):
        print("[Q5UNIV][ABORT] 48銘柄全件の判定が完了しなかったため中止します。")
        return 1

    # --- ここまで全件成功。以降はメモリ上の計算のみ(Redisにはまだ書かない) ---
    day0_tickers = q5logic.detect_day0_tickers(prev_q, today_q)
    new_entries = q5logic.build_new_entries(day0_tickers, today, prices_by_ticker)
    filled_entries = q5logic.fill_checkpoints(entries, trading_calendar, today, close_by_date_by_ticker)
    final_entries = filled_entries + new_entries

    # --- 一括確定(1回のRedis書き込みでprev_q・entries・last_processed_dateを同時に確定) ---
    new_state = {"prev_q": today_q, "entries": final_entries, "last_processed_date": today}
    if not store.set_q5universe_state(new_state):
        print("[Q5UNIV][ABORT] Redisへの保存に失敗しました"
              "(prev_q/entries/last_processed_dateは更新されていません、次回再試行されます)。")
        return 1

    print(f"[Q5UNIV][DONE] {today}分を確定しました"
          f"(新規Day0 {len(new_entries)}件、entries合計 {len(final_entries)}件)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
