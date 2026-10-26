# -*- coding: utf-8 -*-
"""q5_prior_mean_reference.json を生成する再現可能な生成スクリプト(design
2026-10-26、「prior_mean対象銘柄ごとに、その銘柄自身の過去株価データへ本番と
同じQ5ロジックを適用してQ5 Day0履歴を再生成する」方式への切り替え)。

【これより前の版との違い】
以前の版は、研究セッションのTempスクラッチパッドに置かれた一度限りの
danger_features.json(48銘柄、2007-2026)から値を抜き出すだけだった。この版は
本番の判定ロジック(下記)を対象銘柄自身の実際の株価履歴に適用して、
Q5 Day0イベント履歴をこのスクリプト自身の実行でゼロから再現する。これにより
watchlistにある48銘柄ユニバース外の銘柄(MXL/PENG/ZS/ANET/LRCX/PANW/TENB/RBRK等)
についても同じ方法でprior_mean統計を持てるようになる。

【絶対に守ること(ユーザー確定仕様)】
- quintile_logic.py / q5_universe_logic.py / stock_logic.py は一切変更しない
  (このスクリプトはこれらをimportして呼ぶだけ)。
- Q5のrotation universe(quintile_logic.REFERENCE_UNIVERSE、48銘柄+SPY)・
  日次ローテーションプールの対象は変更しない。percentile bounds は
  REFERENCE_UNIVERSEの銘柄だけから計算し、watchlistにあるユニバース外の
  銘柄(新規8銘柄)はそのboundsで判定されるだけで、プール自体には一切
  寄与しない(Q5本体の母集団を48→20等へ変更することは今回の実装範囲外)。
- 保存される全てのpred_scoreは、quintile_logic.knn_predict_score(本番の
  低速だが正確な実装)を直接呼んで計算する。研究セッションで検証済みの
  numpy高速版KNN(max_abs_diff=0.0で本番と数値一致を確認済み)は、本番
  データ生成には使わない(多数ティッカー分の計算時間を短縮するため、
  ティッカー単位でのmultiprocessing並列化だけ行う。アルゴリズム自体の
  近似・差し替えは一切行わない)。
- Day0の定義はq5_universe_logic.detect_day0_tickers(前回Qが既知かつ
  今日Q5かつ前回Q5でない場合のみ)をそのまま使う。未来データは使わない
  (特徴量は当日までのclosesのみのexpanding windowで計算し、poolも当日
  までのスコアだけで更新する)。
- ret20/max_dd20の定義は既存のq5_prior_mean_reference.jsonと同一
  (Day0終値基準、20営業日後終値までのリターン/以降20営業日の終値のうち
  最も下落した時点のリターン、Day0自身は対象外、終値ベース)。

【データソース】
本番のTwelve Data APIキーはこのビルド環境から利用できない(認証情報への
アクセスが許可されていないサンドボックス環境のため)。そのためYahoo
Finance公開chart API(調整後終値、無料・APIキー不要)を使用する。この
事実はdata_source/data_periodとして出力JSONのメタデータに明記する(design
section 9の「本番データソースを使えない場合は明示する」仕様)。将来Twelve
Dataが利用可能な環境で再実行したい場合は、fetch_price_history()だけを
差し替えればよい(以降のロジックはデータソースに依存しない)。

実行方法: python build_q5_prior_mean_reference.py
(初回は全57銘柄のダウンロード+スコア計算で数分〜十数分かかる。価格データは
.q5_price_cache/ にキャッシュされ、再実行時はキャッシュを再利用する。
.q5_price_cache/ は.gitignoreで除外済みでリポジトリには含めない。)
"""
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from datetime import date, datetime, timezone, timedelta
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quintile_logic  # noqa: E402 (本番、無変更でimportして使うだけ)
import q5_universe_logic  # noqa: E402 (本番、無変更でimportして使うだけ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".q5_price_cache")
SCORE_CACHE_DIR = os.path.join(BASE_DIR, ".q5_score_cache")
OUTPUT_PATH = os.path.join(BASE_DIR, "q5_prior_mean_reference.json")

MIN_PRIOR_N = 10  # 既存と同一、変更しない
MIN_HIST = quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES  # 200、既存と同一

REFERENCE_UNIVERSE = quintile_logic.REFERENCE_UNIVERSE  # 48ローテーション+SPY(無変更)
ROTATION_UNIVERSE = quintile_logic.ROTATION_UNIVERSE    # 48(無変更)

# 現在のwatchlist20銘柄のうち、REFERENCE_UNIVERSEに含まれない銘柄
# (design section 4: 現在の20銘柄すべてを対象にする)。
WATCHLIST_EXTRA_TICKERS = ["MXL", "PENG", "ZS", "ANET", "LRCX", "PANW", "TENB", "RBRK"]

# データ取得期間。既存の凍結データのcoverage.start_date(2007-08-23)との
# 連続性を保つため、ウォームアップ分の余裕を含め2006年初から取得する
# (各銘柄の実際の上場日がこれより後なら、取得されたデータの最初の日から
# 自然に開始されるだけで、架空の日付は作らない)。
FETCH_PERIOD1 = int(datetime(2006, 1, 1, tzinfo=timezone.utc).timestamp())


def fetch_price_history(ticker, period2=None):
    """Yahoo Finance公開chart APIから日次調整後終値・出来高を取得する
    (無料・APIキー不要)。本番のTwelve Data取得コード(stock_logic.py等)
    は一切呼ばない、完全に独立した取得関数。"""
    if period2 is None:
        period2 = int(time.time())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={FETCH_PERIOD1}&period2={period2}&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _parse_yahoo(raw):
    result = raw["chart"]["result"][0]
    meta = result["meta"]
    gmtoffset = meta.get("gmtoffset", -14400)
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes_raw = quote["close"]
    volumes_raw = quote["volume"]
    adj = result["indicators"].get("adjclose")
    adjclose_raw = adj[0]["adjclose"] if adj else closes_raw

    by_date = {}
    for i, ts in enumerate(timestamps):
        c = adjclose_raw[i] if adjclose_raw[i] is not None else closes_raw[i]
        if c is None:
            continue
        v = volumes_raw[i]
        local_dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=gmtoffset)
        by_date[local_dt.strftime("%Y-%m-%d")] = (float(c), float(v) if v is not None else 0.0)

    dates = sorted(by_date.keys())
    closes = [by_date[d][0] for d in dates]
    volumes = [by_date[d][1] for d in dates]
    return dates, closes, volumes


def load_or_fetch(ticker):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{ticker}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            d = json.load(f)
        return d["dates"], d["closes"], d["volumes"]
    for attempt in range(3):
        try:
            raw = fetch_price_history(ticker)
            dates, closes, volumes = _parse_yahoo(raw)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"dates": dates, "closes": closes, "volumes": volumes}, f)
            return dates, closes, volumes
        except Exception as e:  # noqa: BLE001 (取得失敗時はリトライ、最終的に例外伝播)
            print(f"  retry {ticker} attempt={attempt} error={e!r}")
            time.sleep(2)
    raise RuntimeError(f"failed to fetch price history for {ticker}")


def _score_worker(args):
    """multiprocessing用ワーカー(ティッカー単位で並列化するだけ、
    アルゴリズムは本番quintile_logic.compute_features/knn_predict_score
    をそのまま呼ぶ)。"""
    ticker, dates, closes, volumes, spy_dates, spy_closes = args
    reference = quintile_logic.load_reference_data()
    n = len(dates)
    scores = {}
    for i in range(MIN_HIST - 1, n):
        raw = quintile_logic.compute_features(
            dates[: i + 1], closes[: i + 1], volumes[: i + 1], spy_dates, spy_closes
        )
        scores[dates[i]] = quintile_logic.knn_predict_score(raw, reference)
    return ticker, scores


def compute_all_scores(price_data, spy_dates, spy_closes, n_workers=1):
    # n_workers=1(逐次実行)がデフォルト。当初14並列→6並列を試したが、この
    # 実行環境は物理メモリが約7.5GBしかなく(ユーザーの他アプリ使用で)空き
    # 容量が1GB未満のことが多いため、44519件のTRAIN参照データ(プロセスごと
    # に個別に保持)を複数プロセスで同時に保持しようとしてOOM(メモリ不足)で
    # 強制終了された(14並列時はCPU使用率が15%まで落ち込みスワップで実質
    # 停滞、6並列・逐次1プロセスでもOS側に強制終了されたことがある)。本番の
    # 判定アルゴリズム自体には一切関係ない、この実行環境のメモリ制約に合わせた
    # 並列度の調整であり、n_workers>1を指定すればmultiprocessingを使う
    # (メモリに余裕がある環境で再実行する場合向けに残す)。
    #
    # チェックポイント(design: 強制終了からの再開対応): ticker単位で
    # .q5_score_cache/<ticker>.json に計算済みスコアを保存する。再実行時は
    # 既に保存済みのtickerの計算をスキップする(本番コード・判定内容には
    # 一切影響しない、純粋に再開のための中間キャッシュ)。
    os.makedirs(SCORE_CACHE_DIR, exist_ok=True)
    tasks = [
        (t, dates, closes, volumes, spy_dates, spy_closes)
        for t, (dates, closes, volumes) in price_data.items()
    ]
    results = {}
    pending = []
    for task in tasks:
        ticker = task[0]
        ckpt_path = os.path.join(SCORE_CACHE_DIR, f"{ticker}.json")
        if os.path.exists(ckpt_path):
            with open(ckpt_path, encoding="utf-8") as f:
                results[ticker] = json.load(f)
            print(f"  scored {ticker}: {len(results[ticker])} days (cached)", flush=True)
        else:
            pending.append(task)

    def _save_checkpoint(ticker, scores):
        with open(os.path.join(SCORE_CACHE_DIR, f"{ticker}.json"), "w", encoding="utf-8") as f:
            json.dump(scores, f)

    if n_workers <= 1:
        for task in pending:
            ticker, scores = _score_worker(task)
            results[ticker] = scores
            _save_checkpoint(ticker, scores)
            print(f"  scored {ticker}: {len(scores)} days", flush=True)
        return results
    with Pool(processes=n_workers) as pool:
        for ticker, scores in pool.imap_unordered(_score_worker, pending):
            results[ticker] = scores
            _save_checkpoint(ticker, scores)
            print(f"  scored {ticker}: {len(scores)} days", flush=True)
    return results


def simulate_pool_and_judge(all_scores, judge_targets):
    """REFERENCE_UNIVERSE(48+SPY)だけでpool_history/boundsを進め(本番の
    update_pool_history/pool_percentile_boundsをそのまま呼ぶ)、judge_targets
    (ROTATION_UNIVERSE48銘柄+watchlist追加8銘柄)それぞれのQ1〜Q5系列と
    Day0イベント(q5_universe_logic.detect_day0_tickersをそのまま使用)を
    計算する。"""
    all_dates = sorted({d for t in REFERENCE_UNIVERSE for d in all_scores.get(t, {})})

    pool_history = None
    last_known_q = {}
    day0_events = {t: [] for t in judge_targets}

    for date_str in all_dates:
        scores_today_pool = {t: all_scores.get(t, {}).get(date_str) for t in REFERENCE_UNIVERSE}
        pool_history = quintile_logic.update_pool_history(pool_history, date_str, scores_today_pool)
        bounds = quintile_logic.pool_percentile_bounds(pool_history)
        if bounds is None:
            continue

        today_q = {}
        for t in judge_targets:
            sc = all_scores.get(t, {}).get(date_str)
            if sc is not None:
                today_q[t] = quintile_logic.assign_quintile(sc, bounds)

        for t in q5_universe_logic.detect_day0_tickers(last_known_q, today_q):
            day0_events[t].append(date_str)

        last_known_q.update(today_q)

    return day0_events


def _max_dd20(closes, i0):
    day0_close = closes[i0]
    window = closes[i0 + 1: i0 + 21]
    return min((c / day0_close - 1) * 100 for c in window)


def attach_returns(day0_dates_by_ticker, price_data):
    """Day0日付リストから、そのticker自身の終値系列でret20/max_dd20を計算する
    (20営業日先のデータが無いイベントは未確定として除外する)。"""
    out = {}
    for t, day0_dates in day0_dates_by_ticker.items():
        dates, closes, _ = price_data[t]
        idx_of_date = {d: i for i, d in enumerate(dates)}
        events = []
        for d0 in day0_dates:
            i0 = idx_of_date.get(d0)
            if i0 is None or i0 + 20 >= len(closes):
                continue
            c0 = closes[i0]
            ret20 = (closes[i0 + 20] / c0 - 1) * 100
            events.append({
                "date": d0,
                "ret20": round(ret20, 4),
                "max_dd20": round(_max_dd20(closes, i0), 4),
            })
        out[t] = sorted(events, key=lambda e: e["date"])
    return out


def _half_split_stats(events):
    n = len(events)
    if n < 10:
        return {"oos_status": "insufficient_for_oos_split"}
    half = n // 2
    h1, h2 = events[:half], events[half:]

    def seg(evs):
        rets = [e["ret20"] for e in evs]
        return {
            "n": len(evs),
            "mean": round(statistics.mean(rets), 2),
            "median": round(statistics.median(rets), 2),
            "win_rate": round(100.0 * sum(1 for r in rets if r > 0) / len(rets), 1),
            "period_start": evs[0]["date"], "period_end": evs[-1]["date"],
        }

    s1, s2 = seg(h1), seg(h2)
    return {
        "oos_status": "ok",
        "half1_n": s1["n"], "half1_mean": s1["mean"], "half1_median": s1["median"],
        "half1_win_rate": s1["win_rate"],
        "half1_period": f"{s1['period_start']}~{s1['period_end']}",
        "half2_n": s2["n"], "half2_mean": s2["mean"], "half2_median": s2["median"],
        "half2_win_rate": s2["win_rate"],
        "half2_period": f"{s2['period_start']}~{s2['period_end']}",
    }


def compute_extended_stats(events):
    """prior_median/prior_win_rate/prior_profit_factor/plus-minus率/平均最大DD/
    OOS半期分割(design section 3・16)。prior_mean自体は既存のget_prior_mean
    (q5_prior_mean_logic.py、無変更)が events_by_ticker から直接計算するので
    ここでは出さない(重複した定義を作らない)。研究・信頼性確認専用の追加
    フィールドであり、UIのBUY判定には使わない(design section 16の制約)。"""
    n = len(events)
    if n < MIN_PRIOR_N:
        return {"status": "insufficient", "n": n}
    rets = [e["ret20"] for e in events]
    dds = [e["max_dd20"] for e in events]
    pos = sum(r for r in rets if r > 0)
    neg = sum(-r for r in rets if r < 0)
    stats = {
        "status": "ok",
        "n": n,
        "prior_median": round(statistics.median(rets), 2),
        "prior_win_rate": round(100.0 * sum(1 for r in rets if r > 0) / n, 1),
        "prior_profit_factor": round(pos / neg, 2) if neg > 0 else None,
        "prior_plus10_rate": round(100.0 * sum(1 for r in rets if r >= 10) / n, 1),
        "prior_plus20_rate": round(100.0 * sum(1 for r in rets if r >= 20) / n, 1),
        "prior_minus10_rate": round(100.0 * sum(1 for r in rets if r <= -10) / n, 1),
        "prior_minus20_rate": round(100.0 * sum(1 for r in rets if r <= -20) / n, 1),
        "prior_minus30_rate": round(100.0 * sum(1 for r in rets if r <= -30) / n, 1),
        "prior_mean_max_dd": round(statistics.mean(dds), 2),
    }
    stats.update(_half_split_stats(events))
    return stats


def main():
    t_start = time.time()
    fetch_targets = sorted(set(REFERENCE_UNIVERSE) | set(WATCHLIST_EXTRA_TICKERS))
    judge_targets = sorted(set(ROTATION_UNIVERSE) | set(WATCHLIST_EXTRA_TICKERS))

    print(f"銘柄数(取得対象) = {len(fetch_targets)}")
    price_data = {}
    for t in fetch_targets:
        dates, closes, volumes = load_or_fetch(t)
        price_data[t] = (dates, closes, volumes)
        print(f"  loaded {t}: {len(dates)} days ({dates[0]}~{dates[-1]})")

    spy_dates, spy_closes, _ = price_data["SPY"]

    print("スコア計算中(multiprocessing、本番knn_predict_scoreをそのまま使用)...")
    all_scores = compute_all_scores(price_data, spy_dates, spy_closes)

    print("pool/bounds再現・Q1-5判定・Day0検出中...")
    day0_dates_by_ticker = simulate_pool_and_judge(all_scores, judge_targets)

    print("ret20/max_dd20付与中...")
    day0_full = attach_returns(day0_dates_by_ticker, price_data)

    events_by_ticker = {}
    extended_stats_by_ticker = {}
    for t in judge_targets:
        events_by_ticker[t] = [
            {"date": e["date"], "ret20": e["ret20"], "max_dd20": e["max_dd20"]}
            for e in day0_full[t]
        ]
        extended_stats_by_ticker[t] = compute_extended_stats(day0_full[t])

    all_dates_flat = [e["date"] for evs in events_by_ticker.values() for e in evs]
    output = {
        "definition": (
            "各銘柄のQ5 Day0イベントにおける、Day0時点終値からDay0+20営業日後"
            "終値までのリターン(%)。本番のquintile_logic.compute_features/"
            "knn_predict_score/update_pool_history/pool_percentile_bounds/"
            "assign_quintile、q5_universe_logic.detect_day0_tickersをそのまま"
            "使って対象銘柄自身の実際の株価履歴に適用し、生成したもの"
            "(build_q5_prior_mean_reference.py、design 2026-10-26)。"
            "Q1〜Q5判定・Q5 Day0判定・BUY判定とは無関係の表示専用の過去実績統計。"
        ),
        "max_dd20_definition": (
            "同じQ5 Day0イベントについて、Day0終値を基準に、以降20営業日の"
            "終値のうち最も下落した時点のリターン(%)。Day0自身は対象外、"
            "終値ベース(日中安値は使わない)。"
        ),
        "min_prior_n": MIN_PRIOR_N,
        "data_source": "Yahoo Finance public chart API (adjusted close, split+dividend-adjusted)",
        "data_source_note": (
            "本番のTwelve Data APIキーはこのビルド実行環境から利用できなかった"
            "ため、無料公開のYahoo Finance調整後終値を使用した(design section 9"
            "の明記ルール)。Q5判定ロジック自体(compute_features以降)は本番と"
            "完全に同一であり、変更したのはデータ取得元のみ。"
        ),
        "rotation_universe_unchanged": True,
        "rotation_universe_note": (
            "percentile boundsはquintile_logic.REFERENCE_UNIVERSE(48銘柄+SPY、"
            "無変更)の日次スコアだけから計算している。watchlist追加銘柄"
            "(" + ", ".join(WATCHLIST_EXTRA_TICKERS) + ")はそのboundsで判定される"
            "だけで、プール自体には一切寄与しない(Q5本体のrotation universeは"
            "48のまま、今回の変更対象外)。"
        ),
        "generated_at": date.today().isoformat(),
        "coverage": {
            "start_date": min(all_dates_flat) if all_dates_flat else None,
            "end_date": max(all_dates_flat) if all_dates_flat else None,
            "n_events": len(all_dates_flat),
            "n_tickers": len(events_by_ticker),
        },
        "events_by_ticker": events_by_ticker,
        "extended_stats_by_ticker": extended_stats_by_ticker,
        "extended_stats_note": (
            "prior_median/prior_win_rate/prior_profit_factor/+-閾値到達率/"
            "平均最大DD/OOS半期分割統計(design section 3・16)。研究・信頼性"
            "確認専用であり、q5_prior_mean_logic.py(無変更)はこのキーを一切"
            "読まない。既存のget_prior_mean/get_prior_statsはevents_by_ticker"
            "から都度計算するため、このフィールドの有無に影響を受けない。"
        ),
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"\n銘柄数: {len(events_by_ticker)}")
    print(f"総イベント数: {len(all_dates_flat)}")
    if all_dates_flat:
        print(f"期間: {min(all_dates_flat)} 〜 {max(all_dates_flat)}")
    print(f"出力: {OUTPUT_PATH}")
    print(f"所要時間: {time.time()-t_start:.1f}秒")


if __name__ == "__main__":
    main()
