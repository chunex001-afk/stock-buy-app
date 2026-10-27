# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 1:
新規ウォッチリスト銘柄1件のprior_mean用Q5 Day0履歴をバックフィルする
専用スクリプト。

2026-10-27追加(案B、研究・検証済み): ローカル価格キャッシュ
(.q5_price_cache/<ticker>.json)が存在しない「本当に新しい銘柄」専用に、
Twelve Data(twelvedata_client.fetch_daily_series、無変更)をoutputsize=5000
で1回だけ呼ぶフォールバックをload_price_series()に追加した。既存56銘柄は
常にローカルキャッシュが存在するためこの分岐には到達せず、挙動は無変更。
日次refresh.py側のoutputsize=300(既存)もこの変更の対象外(別ファイル、
別関数、完全に無関係)。研究で確認済みの再現性(既存56銘柄でprior_mean差分
中央値0.0%pt・最大1.2%pt、FORM実測で334/331件・prior_mean+0.7%・
win_rate52.0%、いずれも基準値に近似)はq5_prior_mean_reference.json
生成時には使わない(既存56銘柄の値は一切変更しない)。

【絶対に守ること(ユーザー確定仕様)】
- quintile_logic.py / q5_prior_mean_logic.py / q5_universe_logic.py /
  stock_logic.py / switch_recommendation_logic.py / twelvedata_client.py /
  refresh.py は一切変更しない。
- q5_prior_mean_reference.json(既存56銘柄)は一切書き込まない(読み込みも
  しない。銘柄一覧の比較はdetect_new_tickersに渡す側=呼び出し元が
  q5_prior_mean_logic.load_reference_data()で読んで渡す設計とし、この
  スクリプト自体はそのファイルパスを一切知らない)。
- REFERENCE_UNIVERSE(48銘柄)は一切再計算しない。Phase 0が保存した
  priormean:bounds_historyだけを使う。
- 既にstatus="done"の銘柄は再バックフィルしない(冪等)。
- 20営業日未経過のDay0はret20を確定させない(未来データ混入防止)。
- Twelve Data取得失敗時(APIキー未設定・TdApiError等)は、架空データで
  補完せず例外をそのまま呼び出し元(run_pending_priormean_backfill.py、
  既存の例外ハンドラ無変更)に伝播させ、status="failed"としてキューに
  残し次回再試行させる(新しい補正ロジックは追加しない)。

Phase 1では、GitHub Actionsへの組み込み・日次自動実行・新規銘柄の自動
スケジューリングは一切行わない(Phase 2予定)。このスクリプトは手動で
ティッカーを指定して実行する想定。

実行方法: python backfill_new_ticker_prior_mean.py FORM [--dry-run]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quintile_logic  # noqa: E402 (本番、無変更)
import redis_store as store  # noqa: E402 (本番、無変更の既存部分+Phase 0/1で追加した関数のみ)
import priormean_incremental_logic as inc  # noqa: E402 (Phase 1新規、純粋関数)
import twelvedata_client as td  # noqa: E402 (本番refresh.pyと同じ無変更モジュール、新規銘柄フォールバック専用に呼ぶだけ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRICE_CACHE_DIR_DEFAULT = os.path.join(BASE_DIR, ".q5_price_cache")

# 新規銘柄の初回バックフィル専用フォールバックのoutputsize(design 2026-10-27
# 研究・検証済み)。既存のrefresh.AI_MARKET_DECLINE_ATH_OUTPUTSIZEと同じ値
# (Twelve Data 1リクエストの上限、creditsはoutputsizeに依存しない)。
# 日次refresh.py本体のtwelvedata_client.OUTPUTSIZE=300(既存、無変更)とは
# 完全に別の値であり、このファイル(新規銘柄バックフィル専用)の中でのみ使う。
NEW_TICKER_BACKFILL_OUTPUTSIZE = 5000


def load_price_series(ticker, price_cache_dir):
    """この銘柄の価格データを読み込む。

    ローカルキャッシュ(.q5_price_cache/<ticker>.json、既存56銘柄・研究用)が
    存在する場合は常にそれを使う(既存の挙動、無変更)。存在しない場合のみ、
    新規銘柄の初回バックフィル専用としてTwelve Dataへoutputsize=5000で
    1回だけフォールバック取得する(design 2026-10-27、案B)。"""
    path = os.path.join(price_cache_dir, f"{ticker}.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d["dates"], d["closes"], d.get("volumes")
    except FileNotFoundError:
        return _fetch_price_series_from_twelvedata(ticker)


def _fetch_price_series_from_twelvedata(ticker):
    """新規銘柄の初回バックフィル専用フォールバック(design 2026-10-27、案B)。
    twelvedata_client.fetch_daily_series自体は無変更のまま、
    outputsize=NEW_TICKER_BACKFILL_OUTPUTSIZE(5000)で1回だけ呼ぶ。

    失敗時(APIキー未設定・TdApiError等)は例外をそのまま伝播させる(架空の
    データで補完しない)。呼び出し元のrun_pending_priormean_backfill.py
    (既存、無変更)が既存のtry/exceptでstatus="failed"を記録し、キューには
    残して次回再試行する。"""
    api_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            f"{ticker}: ローカル価格キャッシュが無く、TWELVEDATA_API_KEYも未設定のため取得できません。"
        )
    dates, closes, volumes, _lows, _attempts = td.fetch_daily_series(
        ticker, api_key, outputsize=NEW_TICKER_BACKFILL_OUTPUTSIZE,
    )
    return dates, closes, volumes


def compute_ticker_scores(dates, closes, volumes, spy_dates, spy_closes, reference_train):
    """この新規銘柄自身のpred_scoreを計算する(本番のquintile_logic.
    compute_features/knn_predict_scoreを無変更のまま呼ぶだけ。
    REFERENCE_UNIVERSE48銘柄は一切参照しない、この銘柄自身の価格データと
    SPY・TRAIN参照データだけで完結する)。"""
    scores = {}
    for i in range(quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES - 1, len(dates)):
        raw = quintile_logic.compute_features(
            dates[: i + 1], closes[: i + 1],
            volumes[: i + 1] if volumes else None,
            spy_dates, spy_closes,
        )
        scores[dates[i]] = quintile_logic.knn_predict_score(raw, reference_train)
    return scores


def backfill_one_ticker(ticker, price_cache_dir=None, dry_run=False):
    """新規銘柄1件をバックフィルする。戻り値には計測用の統計を含む。"""
    price_cache_dir = price_cache_dir or PRICE_CACHE_DIR_DEFAULT
    t_start = time.time()

    status = store.get_priormean_backfill_status(ticker)
    if status == "done":
        print(f"{ticker}: 既にバックフィル済み(status=done)のためスキップします(冪等)。")
        return {"skipped": True, "reason": "already_done", "ticker": ticker}

    bounds_by_date = store.get_priormean_bounds_history()
    if not bounds_by_date:
        raise RuntimeError(
            "priormean:bounds_history が取得できません。先にPhase 0の"
            "build_priormean_bounds_history.pyを実行してください。"
        )

    dates, closes, volumes = load_price_series(ticker, price_cache_dir)
    spy_dates, spy_closes, _ = load_price_series("SPY", price_cache_dir)

    if not dry_run:
        store.set_priormean_backfill_status(ticker, "in_progress")

    reference_train = quintile_logic.load_reference_data()
    t_score_start = time.time()
    scores_by_date = compute_ticker_scores(dates, closes, volumes, spy_dates, spy_closes, reference_train)
    score_elapsed = time.time() - t_score_start

    result = inc.backfill_ticker_day0_events(ticker, dates, closes, scores_by_date, bounds_by_date)

    existing_events = store.get_priormean_events(ticker) or []
    merged_events = inc.dedup_events(existing_events, result["resolved_events"])

    elapsed = time.time() - t_start
    print(f"{ticker}: 価格データ{len(dates)}日分 / スコア計算{len(scores_by_date)}日分"
          f"({score_elapsed:.1f}秒) / Day0合計={result['day0_count']}件 "
          f"確定={len(result['resolved_events'])}件 未確定={len(result['unresolved_day0_dates'])}件 "
          f"合計所要時間={elapsed:.1f}秒")

    if dry_run:
        print(f"{ticker}: dry_run=Trueのため、Redisへは書き込みません(status=in_progressにも変更していません)。")
        return {
            "skipped": False, "ticker": ticker, "result": result,
            "merged_events": merged_events, "elapsed": elapsed, "score_elapsed": score_elapsed,
        }

    store.set_priormean_events(ticker, merged_events)
    store.set_priormean_backfill_status(ticker, "done")
    return {
        "skipped": False, "ticker": ticker, "result": result,
        "merged_events": merged_events, "elapsed": elapsed, "score_elapsed": score_elapsed,
    }


def main():
    parser = argparse.ArgumentParser(description="新規ウォッチリスト銘柄1件のprior_mean用Q5 Day0履歴をバックフィルする")
    parser.add_argument("ticker")
    parser.add_argument("--dry-run", action="store_true", help="Redisへ書き込まず結果だけ表示する")
    parser.add_argument("--price-cache-dir", default=None)
    args = parser.parse_args()
    backfill_one_ticker(args.ticker.upper(), price_cache_dir=args.price_cache_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
