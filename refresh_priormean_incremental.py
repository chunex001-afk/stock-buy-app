# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 2: 日次の
軽量差分更新バッチ。

既存のrefresh.py(daily-refresh.yml)・refresh_q5_universe.py
(q5-universe-refresh.yml)とは完全に分離した別のGitHub Actionsから1日1回
呼ばれる想定。既存のQ1〜Q5判定・Q5 Day0判定・BUY/SELL判定・prior_mean計算・
ラベル判定・Q5ランキング・Risk-off・switch_recommendation_logic・
watchlist・q5_prior_mean_reference.jsonのいずれにも一切書き込まない
(読み取りのみ)。

【このジョブが行う3つのこと】
1. 新規銘柄検出(API呼び出しなし): watchlistとq5_prior_mean_reference.json
   +priormean:backfill_status:*を比較し、新規にバックフィルが必要な銘柄を
   priormean:pending_backfillへ追加するだけ(実際のバックフィルは別の
   run_pending_priormean_backfill.py・別ワークフローが担当。このジョブ
   自身は重い処理を一切行わない)。
2. バックフィル済み銘柄のDay0検出(API呼び出しなし): 本体refresh.pyが
   既に毎日計算・保存しているquintile:state:{ticker}(current_q/
   previous_q/q5_price_path)をそのまま読むだけで、新しいDay0が発生したか
   判定する(q5_universe_logic.detect_day0_tickers、無変更)。新規の
   pred_score計算・Twelve Data呼び出しは一切発生しない。
3. 保留中イベントの確定(20営業日経過した分だけ、必要な銘柄だけ新規
   フェッチ): quintile:state:{ticker}のq5_price_pathはDay0〜Day5
   (6日分)しか保持していないため、Day0+20の終値はこのジョブが
   refresh._td_fetch_one(既存、無変更)で取得する。ただし20営業日に
   到達した保留イベントが無い日は、この新規フェッチは一切発生しない
   (「新規銘柄なし→数秒で終了」という設計目標どおり)。

【絶対に守ること(ユーザー確定仕様)】
- quintile_logic.py / q5_prior_mean_logic.py / q5_universe_logic.py /
  stock_logic.py / switch_recommendation_logic.py / refresh.py は一切
  変更しない(refresh.remaining_td_budget/_td_fetch_oneを無変更のまま
  importして呼ぶだけ、refresh_q5_universe.pyと同じ再利用方針)。
- q5_prior_mean_reference.json(既存56銘柄)は一切書き込まない。
- REFERENCE_UNIVERSE(48銘柄)は一切再計算しない。
- 1ティッカーの取得失敗が他のティッカーの処理を止めない(fault isolation、
  q5universe:stateのall-or-nothingとは意図的に異なる設計。理由:
  prior_meanの差分データは補助的な参考情報であり、1銘柄の失敗で全体を
  止めるより、失敗した銘柄だけ次回に持ち越す方が安全〈セクション⑥の
  「Redis障害時も既存動作を優先して安全にフォールバック」という要件に
  対応〉)。
- 20営業日未経過のイベントは確定しない(未来データ混入防止)。
- 新規バックフィルの実行(重い処理)はこのジョブの中では一切行わない。

実行方法: python refresh_priormean_incremental.py
"""
import os
import sys

import priormean_incremental_logic as inc
import q5_prior_mean_logic as pml
import q5_universe_logic
import redis_store as store
import refresh  # 既存のremaining_td_budget/_td_fetch_oneを再利用するだけ、無変更


def main():
    pool_history = store.get_pool_history()
    if not pool_history or not pool_history.get("dates"):
        print("[PRIORMEAN-INC][SKIP] quintile:pool_historyが未初期化のため終了します"
              "(本体の日次更新がまだ一度も成功していません。Twelve Dataは呼んでいません)。")
        return 0

    trading_calendar = sorted(pool_history["dates"])
    today = trading_calendar[-1]

    inc_state = store.get_priormean_incremental_state() or {}
    if inc_state.get("last_processed_date") == today:
        print(f"[PRIORMEAN-INC][SKIP] {today}分は既に処理済みのため終了します"
              "(Twelve Dataは呼んでいません、同日の重複実行を防止)。")
        return 0

    watchlist = store.get_watchlist() or []
    static_data = pml.load_reference_data()
    static_tickers = set(static_data.get("events_by_ticker", {}).keys())

    # --- ①新規銘柄検出(API呼び出しなし) ---
    status_by_ticker = {t: store.get_priormean_backfill_status(t) for t in watchlist}
    new_tickers = inc.detect_new_tickers(watchlist, static_tickers, status_by_ticker)
    if new_tickers:
        pending_backfill = list(store.get_priormean_pending_backfill() or [])
        for t in new_tickers:
            if t not in pending_backfill:
                pending_backfill.append(t)
            store.set_priormean_backfill_status(t, "pending")
        store.set_priormean_pending_backfill(pending_backfill)
        print(f"[PRIORMEAN-INC] 新規バックフィル対象を検出: {new_tickers} "
              "(priormean:pending_backfillへ追加しました。実際のバックフィルは別ジョブが行います)。")

    # --- ②バックフィル済み銘柄のDay0検出(API呼び出しなし、quintile:stateを再利用) ---
    backfilled_tickers = [t for t in watchlist if status_by_ticker.get(t) == "done"]
    prev_q_state = dict(inc_state.get("prev_q") or {})
    pending = list(inc_state.get("pending") or [])
    new_day0_count = 0

    for ticker in backfilled_tickers:
        qstate = store.get_quintile_state(ticker)
        if not qstate or qstate.get("current_q") is None:
            continue  # データ不足等で本日Q判定が保留中の銘柄はスキップ(次回に委ねる)
        current_q = qstate["current_q"]
        prev_q = prev_q_state.get(ticker)
        if prev_q is not None:
            day0_hit = q5_universe_logic.detect_day0_tickers({ticker: prev_q}, {ticker: current_q})
            if day0_hit:
                price_path = qstate.get("q5_price_path") or []
                day0_price = price_path[0]["price"] if price_path else None
                if day0_price is not None:
                    pending.append({"ticker": ticker, "day0_date": today, "day0_price": day0_price})
                    new_day0_count += 1
        prev_q_state[ticker] = current_q

    # --- ③保留中イベントの確定(20営業日経過した分だけ、必要な銘柄だけ新規フェッチ) ---
    api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    still_pending = []
    resolved_count = 0
    fetch_cache = {}  # 同じ銘柄を同じ実行内で2回フェッチしないためのキャッシュ

    for entry in pending:
        if entry["day0_date"] not in trading_calendar:
            still_pending.append(entry)
            continue
        day0_idx = trading_calendar.index(entry["day0_date"])
        target_idx = day0_idx + 20
        if target_idx >= len(trading_calendar) or trading_calendar[target_idx] > today:
            still_pending.append(entry)  # まだ20営業日経過していない
            continue

        ticker = entry["ticker"]
        if ticker not in fetch_cache:
            if not api_key:
                fetch_cache[ticker] = None
            else:
                budget_left, _ = refresh.remaining_td_budget()
                if budget_left <= 0:
                    fetch_cache[ticker] = None
                else:
                    result, _hit_rl, _err, _msg, _lows = refresh._td_fetch_one(ticker, api_key, today)
                    fetch_cache[ticker] = result

        result = fetch_cache[ticker]
        if result is None:
            still_pending.append(entry)  # 取得失敗/予算切れ、次回再試行(このticker以外は影響を受けない)
            continue

        dates, closes, _volumes = result
        close_by_date = dict(zip(dates, closes))
        resolved = inc.resolve_pending_event(entry["day0_price"], close_by_date, entry["day0_date"], trading_calendar)
        if resolved is None:
            still_pending.append(entry)  # 対象日が取得範囲外、次回再試行
            continue

        existing_events = store.get_priormean_events(ticker) or []
        merged_events = inc.dedup_events(existing_events, [resolved])
        if store.set_priormean_events(ticker, merged_events):
            resolved_count += 1
        else:
            still_pending.append(entry)  # Redis保存失敗、次回再試行

    ok = store.set_priormean_incremental_state({
        "prev_q": prev_q_state,
        "pending": still_pending,
        "last_processed_date": today,
    })
    if not ok:
        print("[PRIORMEAN-INC][WARN] priormean:incremental_stateの保存に失敗しました"
              "(次回実行時に同じ日付から再試行されます。既存56銘柄・既存Q1〜Q5判定には影響しません)。")

    print(f"[PRIORMEAN-INC][DONE] {today}: 新規バックフィル検出={len(new_tickers)}件 "
          f"新規Day0={new_day0_count}件 確定={resolved_count}件 保留中={len(still_pending)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
