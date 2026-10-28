# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27、design調査)Phase 0: REFERENCE_UNIVERSE
(quintile_logic.REFERENCE_UNIVERSE、48銘柄+SPY、無変更)の過去の日次Q1〜Q5
bounds時系列を一度だけ計算し、Redis(priormean:bounds_history、TTLなし)へ
保存する専用スクリプト。

【これが解決する問題】
研究で判明した通り、新規ウォッチリスト銘柄(例: FORM)を本番でバックフィル
するには、REFERENCE_UNIVERSE48銘柄の過去のpool/bounds時系列が必要だが、
これは研究セッションのローカルキャッシュ(.q5_score_cache、.gitignore対象)
にしか存在せず、本番(GitHub Actions/Render)には永続化されていなかった。
このスクリプトはその前提データを一度だけ作り、Redisに保存する。

【絶対に守ること(ユーザー確定仕様)】
- quintile_logic.py / q5_prior_mean_logic.py / q5_universe_logic.py /
  stock_logic.py / switch_recommendation_logic.py は一切変更しない
  (このスクリプトはこれらをimportして呼ぶだけ)。
- q5_prior_mean_reference.json(gitコミット済み、56銘柄)は一切変更・
  上書きしない。このスクリプトは読み込みもしない(無関係な別データ)。
- bounds計算のロジック(quintile_logic.compute_features/knn_predict_score/
  update_pool_history/pool_percentile_bounds)は無変更のまま呼ぶだけ。
  新しいboundsの定義・計算式は一切作らない。
- 保存先はRedis(priormean:bounds_history)のみ。TTLは付けない
  (redis_store.set_json の ex 引数を渡さない=無期限)。
- GitHub Actionsの既存ワークフロー(daily-refresh.yml / q5-universe-
  refresh.yml)には一切組み込まない。このスクリプトは手動で一度だけ実行する
  専用処理。

【データソース】
REFERENCE_UNIVERSE(48銘柄+SPY)の価格データは、既存の.q5_price_cache/
(build_q5_prior_mean_reference.py実行時に生成済み、.gitignore対象)を
そのまま再利用する。pred_scoreも、.q5_score_cache/(同じく生成済み)が
存在すればそのまま再利用し(新規計算なし、高速)、存在しない場合のみ
quintile_logic.compute_features/knn_predict_scoreで計算する(将来、
これらのキャッシュが無い環境で再実行する場合のフォールバック)。

実行方法: python build_priormean_bounds_history.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quintile_logic  # noqa: E402 (本番、無変更でimportして使うだけ)
import redis_store as store  # noqa: E402 (本番、無変更でimportして使うだけ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRICE_CACHE_DIR = os.path.join(BASE_DIR, ".q5_price_cache")
SCORE_CACHE_DIR = os.path.join(BASE_DIR, ".q5_score_cache")

REFERENCE_UNIVERSE = quintile_logic.REFERENCE_UNIVERSE  # 48銘柄+SPY、無変更
MIN_HIST = quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES  # 200、無変更


def load_price_series(ticker):
    path = os.path.join(PRICE_CACHE_DIR, f"{ticker}.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["dates"], d["closes"], d["volumes"]


def load_or_compute_scores(ticker, dates, closes, volumes, spy_dates, spy_closes, reference_train):
    """.q5_score_cache/<ticker>.json があればそのまま再利用する(新規計算
    なし、既存のbuild_q5_prior_mean_reference.py実行時に生成済みのキャッシュ)。
    無ければ本番と同じquintile_logic.compute_features/knn_predict_scoreで
    計算する(将来、キャッシュが無い環境向けのフォールバック。この経路は
    銘柄1件あたり数分かかる)。"""
    cache_path = os.path.join(SCORE_CACHE_DIR, f"{ticker}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f), True
    scores = {}
    for i in range(MIN_HIST - 1, len(dates)):
        raw = quintile_logic.compute_features(
            dates[: i + 1], closes[: i + 1], volumes[: i + 1], spy_dates, spy_closes,
        )
        scores[dates[i]] = quintile_logic.knn_predict_score(raw, reference_train)
    return scores, False


def main():
    t_start = time.time()
    reference_train = quintile_logic.load_reference_data()  # TRAIN参照データ、無変更

    price_data = {}
    for t in REFERENCE_UNIVERSE:
        price_data[t] = load_price_series(t)
        print(f"  loaded price: {t} ({len(price_data[t][0])}日)")

    spy_dates, spy_closes, _ = price_data["SPY"]

    all_scores = {}
    cache_hits = 0
    for t in REFERENCE_UNIVERSE:
        dates, closes, volumes = price_data[t]
        scores, from_cache = load_or_compute_scores(t, dates, closes, volumes, spy_dates, spy_closes, reference_train)
        all_scores[t] = scores
        cache_hits += int(from_cache)
        print(f"  scored: {t} ({len(scores)}日分、{'キャッシュ再利用' if from_cache else '新規計算'})")

    print(f"スコア取得完了: {cache_hits}/{len(REFERENCE_UNIVERSE)}銘柄がキャッシュ再利用")

    all_dates = sorted({d for t in REFERENCE_UNIVERSE for d in all_scores.get(t, {})})

    pool_history = None
    bounds_by_date = {}
    for date_str in all_dates:
        scores_today = {t: all_scores.get(t, {}).get(date_str) for t in REFERENCE_UNIVERSE}
        pool_history = quintile_logic.update_pool_history(pool_history, date_str, scores_today)
        bounds = quintile_logic.pool_percentile_bounds(pool_history)
        if bounds is None:
            continue
        bounds_by_date[date_str] = [round(b, 6) for b in bounds]

    elapsed = time.time() - t_start
    print(f"\nbounds計算日数: {len(bounds_by_date)}日")
    if bounds_by_date:
        first_d = min(bounds_by_date)
        last_d = max(bounds_by_date)
        print(f"期間: {first_d} 〜 {last_d}")
    print(f"所要時間: {elapsed:.1f}秒")

    payload_json = json.dumps(bounds_by_date, ensure_ascii=False)
    size_bytes = len(payload_json.encode("utf-8"))
    print(f"保存データサイズ: {size_bytes:,} bytes ({size_bytes/1024:.1f} KB)")

    # ローカル検証用コピー(リポジトリにはコミットしない、.gitignore対象の
    # 作業用ファイル。Redisが唯一の正式な保存先)。
    local_copy_path = os.path.join(BASE_DIR, "_priormean_bounds_history_local_copy.json")
    with open(local_copy_path, "w", encoding="utf-8") as f:
        f.write(payload_json)
    print(f"ローカル検証用コピー: {local_copy_path}")

    if store.is_configured():
        ok = store.set_priormean_bounds_history(bounds_by_date)
        print(f"Redis保存(priormean:bounds_history、TTLなし): {'成功' if ok else '失敗'}")
    else:
        print("Redis未設定(UPSTASH_REDIS_REST_URL/TOKENが無い環境)のため、"
              "Redisへの保存はスキップしました。ローカル検証用コピーのみ出力しています。")

    print(f"\n合計所要時間: {time.time()-t_start:.1f}秒")


if __name__ == "__main__":
    main()
