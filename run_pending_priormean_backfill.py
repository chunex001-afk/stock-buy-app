# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 2:
priormean:pending_backfillキューから新規銘柄を1件だけ取り出してバック
フィルする、既存の日次処理(daily-refresh.yml / q5-universe-refresh.yml)
とは完全に独立したワークフロー専用スクリプト。

【絶対に守ること(ユーザー確定仕様)】
- backfill_new_ticker_prior_mean.backfill_one_ticker(Phase 1、無変更)を
  そのまま呼ぶだけ。新しいバックフィルロジックはここでは作らない。
- pending_backfillが空の場合は何もせず即座に終了する(API呼び出し・重い
  計算は一切発生しない)。
- 1回の実行で処理するのは最大1銘柄のみ(複数の新規銘柄が溜まっていても、
  既存処理を圧迫しないよう1回1銘柄に制限する、ユーザー確定仕様)。
- 2026-10-28修正(Head-of-Line Blocking解消): 失敗したtickerもstatus="failed"
  を記録した上でpending_backfillキューから一旦削除する(以前はキューに
  残り続け、そのtickerが永久にキュー先頭を占有し後続の新規銘柄が処理
  されなくなる問題があった)。再試行回数の上限は追加しない。翌日の
  refresh_priormean_incremental.py(無変更)がstatus="failed"のtickerを
  再試行対象として検出し、pending_backfillへ再度追加する既存仕様はそのまま
  維持する(つまり「無期限に再試行され続ける」という既存の思想自体は
  変えず、「失敗した1件が他の新規銘柄をブロックしない」点だけを直す)。

【既知の制約、重要】
backfill_one_ticker()の価格データ取得は、Phase 1時点では
.q5_price_cache/<ticker>.json(ローカルキャッシュ)からの読み込みのみを
実装しており、本番でこのファイルが存在しない「本当に新しい銘柄」の
場合にどうやって数十年分の価格データを用意するかは未実装・未検証です
(Twelve Dataの既存fetch_daily_seriesはoutputsize=300が上限で、多年度の
バックフィルには使えません)。本番で実際に未知の新規銘柄を処理する前に、
この価格データ取得方法を別途決める必要があります(詳細は実装報告を参照)。

実行方法: python run_pending_priormean_backfill.py
"""
import sys

import backfill_new_ticker_prior_mean as backfill_module
import redis_store as store


def main():
    pending = list(store.get_priormean_pending_backfill() or [])
    if not pending:
        print("[PRIORMEAN-BACKFILL][SKIP] 保留中の新規バックフィル対象銘柄はありません。")
        return 0

    ticker = pending[0]
    print(f"[PRIORMEAN-BACKFILL] {ticker} のバックフィルを開始します(キュー残り{len(pending)}件)。")
    try:
        outcome = backfill_module.backfill_one_ticker(ticker)
    except FileNotFoundError as e:
        print(f"[PRIORMEAN-BACKFILL][FAILED] {ticker}: 価格データが見つかりません({e})。"
              "status=failedを記録し、キューからは一旦外します"
              "(翌日のrefresh_priormean_incremental.pyが再度キューへ追加し、再試行します)。")
        store.set_priormean_backfill_status(ticker, "failed")
        store.set_priormean_pending_backfill([t for t in pending if t != ticker])
        return 1
    except Exception as e:  # noqa: BLE001 (このジョブ全体を落とさない、失敗を記録して次回に委ねる)
        print(f"[PRIORMEAN-BACKFILL][FAILED] {ticker}: 予期しないエラー: {e}。"
              "status=failedを記録し、キューからは一旦外します"
              "(翌日のrefresh_priormean_incremental.pyが再度キューへ追加し、再試行します)。")
        store.set_priormean_backfill_status(ticker, "failed")
        store.set_priormean_pending_backfill([t for t in pending if t != ticker])
        return 1

    remaining = [t for t in pending if t != ticker]
    store.set_priormean_pending_backfill(remaining)

    if outcome.get("skipped"):
        print(f"[PRIORMEAN-BACKFILL] {ticker}: {outcome.get('reason')} のためスキップしました。")
    else:
        result = outcome["result"]
        print(f"[PRIORMEAN-BACKFILL][DONE] {ticker}: Day0={result['day0_count']}件 "
              f"確定={len(result['resolved_events'])}件 未確定={len(result['unresolved_day0_dates'])}件 "
              f"所要時間={outcome['elapsed']:.1f}秒 キュー残り={len(remaining)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
