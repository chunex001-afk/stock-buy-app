# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-28d、design確認済み)Phase 4: watchlist
追加直後の即時dispatchバックフィル専用ドライバー。

GitHub Actions workflow_dispatch(priormean-backfill-dispatch.yml)の
`inputs.ticker`から1銘柄だけ受け取り、backfill_new_ticker_prior_mean.
backfill_one_ticker(既存、無変更)をそのまま呼ぶだけの薄いスクリプト。
新しいバックフィルロジックはここでは一切作らない。

【絶対に守ること(ユーザー確定仕様)】
- backfill_new_ticker_prior_mean.py / priormean_incremental_logic.py /
  quintile_logic.py / twelvedata_client.py は一切変更しない(importして
  呼ぶだけ)。
- 失敗時の扱いは既存のrun_pending_priormean_backfill.py(無変更)と同じ
  方針: status="failed"を記録するだけで、新しい補正ロジックは追加しない。
  既存のdetect_new_tickers/detect_new_tickers_for_overnight_backfillが
  "failed"を再試行対象として検出し、次回cronがpending_backfill経由で
  再試行する(既存の仕組みをそのまま使う、キュー操作はこのスクリプトでは
  行わない)。
- 二重起動防止はbackfill_one_ticker内のロック取得(design 2026-10-28d、
  既存のtry_acquire_push_dispatch_lockと同じSET NXパターン)に委ねる。
  このスクリプト自身はロックの取得・解放を行わない。

実行方法: python backfill_dispatched_ticker.py <TICKER>
"""
import sys

import backfill_new_ticker_prior_mean as backfill_module
import redis_store as store


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("使い方: python backfill_dispatched_ticker.py <TICKER>", file=sys.stderr)
        return 2

    ticker = sys.argv[1].strip().upper()
    print(f"[PRIORMEAN-DISPATCH] {ticker} の即時バックフィルを開始します。")

    try:
        outcome = backfill_module.backfill_one_ticker(ticker)
    except FileNotFoundError as e:
        print(f"[PRIORMEAN-DISPATCH][FAILED] {ticker}: 価格データが見つかりません({e})。"
              "status=failedを記録します(既存cronが再試行対象として検出し、"
              "pending_backfill経由で再試行します)。")
        store.set_priormean_backfill_status(ticker, "failed")
        return 1
    except Exception as e:  # noqa: BLE001 (このジョブ自体を落とさず、失敗を記録して既存cronに委ねる)
        print(f"[PRIORMEAN-DISPATCH][FAILED] {ticker}: 予期しないエラー: {e}。"
              "status=failedを記録します(既存cronが再試行対象として検出し、"
              "pending_backfill経由で再試行します)。")
        store.set_priormean_backfill_status(ticker, "failed")
        return 1

    if outcome.get("skipped"):
        print(f"[PRIORMEAN-DISPATCH][SKIP] {ticker}: {outcome.get('reason')} のためスキップしました。")
    else:
        result = outcome["result"]
        print(f"[PRIORMEAN-DISPATCH][DONE] {ticker}: Day0={result['day0_count']}件 "
              f"確定={len(result['resolved_events'])}件 未確定={len(result['unresolved_day0_dates'])}件 "
              f"所要時間={outcome['elapsed']:.1f}秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
