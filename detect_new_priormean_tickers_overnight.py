# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-28c、design確認済み)Phase 3: 24:00 JST
締切の深夜バックフィル専用。新規銘柄検出だけを行う軽量スクリプト。

【これが解決する問題】
既存のrefresh_priormean_incremental.py(日次1回、08:40 JST実行)は、
①新規銘柄検出 ②Day0検出 ③20営業日経過イベント確定 の3つを1つの処理に
まとめている。②③は本体daily-refresh.pyが当日分のquintile:pool_history/
quintile:state/を更新した後(07:00 JST以降)でなければ正しく動作しない
ため、①だけを深夜(24:00 JST締切の直後、00:10 JST目安)に早めて実行する
ことはできない。

このスクリプトは①(新規銘柄検出・pending_backfillへの登録)だけを独立に
行う。既存のrefresh_priormean_incremental.py自体は一切変更しない(②③は
現在の08:40 JST実行にそのまま残す、design 2026-10-28cユーザー確定方針)。
このスクリプトが新規銘柄を検出してstatus="pending"にした後は、既存の
08:40 JST側のdetect_new_tickers(無変更)がそれを見て「既に対象」として
自然に除外するため、二重登録は発生しない。

【絶対に守ること(ユーザー確定仕様)】
- refresh_priormean_incremental.py / priormean_incremental_logic.
  detect_new_tickers(既存、無変更)は一切変更しない。新しく追加した
  priormean_incremental_logic.detect_new_tickers_for_overnight_backfill
  (既存のdetect_new_tickersをそのまま呼ぶだけの追加関数)だけを使う。
- quintile:pool_history / quintile:state / priormean:incremental_state
  には一切アクセスしない(Day0検出・イベント確定は完全にこのスクリプトの
  対象外)。
- 1回の実行で検出できる新規銘柄は複数件あってよい(既存のdetect_new_
  tickersと同じ、新規銘柄の検出・キュー登録自体は軽量なRedis読み取り
  だけで重い処理が無いため、1銘柄ずつに制限する必要がない)。実際の
  バックフィル実行(run_pending_priormean_backfill.py、無変更)は
  引き続き1回の実行で1銘柄だけを処理する。
- 24:00 JST(=翌日0:00 JST)以降に追加された銘柄は今回は検出対象から
  除外し、次の深夜(または既存の08:40 JST日次処理)に委ねる。

実行方法: python detect_new_priormean_tickers_overnight.py
"""
import sys
from datetime import datetime, timedelta, timezone

import priormean_incremental_logic as inc
import q5_prior_mean_logic as pml
import redis_store as store

_JST = timezone(timedelta(hours=9))


def _today_midnight_jst_iso():
    """今日0:00 JST(=前日24:00 JST)のISO8601文字列。watchlist追加時刻
    (app._now_jst_iso、同じ固定オフセット+09:00)と辞書順比較できる形式。"""
    now_jst = datetime.now(_JST)
    midnight = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def main():
    watchlist = store.get_watchlist() or []
    if not watchlist:
        print("[PRIORMEAN-OVERNIGHT-DETECT][SKIP] watchlistが空のため終了します。")
        return 0

    static_data = pml.load_reference_data()
    static_tickers = set(static_data.get("events_by_ticker", {}).keys())

    status_by_ticker = {t: store.get_priormean_backfill_status(t) for t in watchlist}
    added_at_by_ticker = store.get_priormean_watchlist_added_at()
    cutoff_iso = _today_midnight_jst_iso()

    new_tickers = inc.detect_new_tickers_for_overnight_backfill(
        watchlist, static_tickers, status_by_ticker, added_at_by_ticker, cutoff_iso,
    )

    if not new_tickers:
        print(f"[PRIORMEAN-OVERNIGHT-DETECT][SKIP] cutoff={cutoff_iso} "
              "締切前に追加された新規バックフィル対象銘柄はありません。")
        return 0

    pending_backfill = list(store.get_priormean_pending_backfill() or [])
    for t in new_tickers:
        if t not in pending_backfill:
            pending_backfill.append(t)
        store.set_priormean_backfill_status(t, "pending")
    store.set_priormean_pending_backfill(pending_backfill)

    print(f"[PRIORMEAN-OVERNIGHT-DETECT][DONE] cutoff={cutoff_iso} "
          f"新規バックフィル対象を検出: {new_tickers} "
          f"(priormean:pending_backfillへ追加しました。キュー残り{len(pending_backfill)}件。"
          "実際のバックフィルはrun_pending_priormean_backfill.py〈無変更〉が行います)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
