# -*- coding: utf-8 -*-
"""Q5 Day0銘柄に追加表示する「Q5過去平均リターン」(design 2026-10-23)の
判定ロジック。ticker-reliability研究Phase1〜3(validation_ticker_reliability_q5
メモリ参照)で検証済みのprior_mean定義をそのまま使う、表示専用の参考情報。

【絶対に守ること】
- 既存のQ1〜Q5判定・Q5 Day0判定・BUY/SELL判定には一切関与しない(ここの関数の
  戻り値をそれらの判定条件に使ってはならない)。
- prior_meanは「そのQ5 Day0イベントの日付より厳密に過去」の同一銘柄Q5 Day0
  イベントだけから計算する(未来情報を絶対に混入させない、day0_date自身の
  イベントも含めない)。
- データが足りない(過去Q5 Day0件数がMIN_PRIOR_N未満)銘柄は0%等で補完せず、
  status="insufficient"として呼び出し側に委ねる。

Redis/Twelve Dataへは一切アクセスしない純粋関数(quintile_logic.load_reference_data
と同じ、プロセス内キャッシュ付きJSON読み込みパターン)。
"""
import json
import os
import statistics

REFERENCE_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "q5_prior_mean_reference.json")

# Phase1〜3研究で確立した、個別銘柄のprior_meanを「参考情報として表示してよい」
# と判断する最低限の過去Q5 Day0件数。これより少ない銘柄は「データ不足」とする
# (validation_ticker_reliability_q5メモリ: 個別銘柄の判断としてはn>=10を
# 最低ラインとして推奨、という研究結論をそのまま採用)。
MIN_PRIOR_N = 10

_reference_cache = None


def load_reference_data(path=REFERENCE_DATA_PATH):
    """q5_prior_mean_reference.json(研究データから生成した凍結参照データ)を
    読み込む。プロセス内で使い回す(quintile_logic.load_reference_dataと同じ
    パターン)。"""
    global _reference_cache
    if _reference_cache is None:
        with open(path, encoding="utf-8") as f:
            _reference_cache = json.load(f)
    return _reference_cache


def get_prior_mean(ticker, day0_date, data=None):
    """指定銘柄のprior_mean(そのday0_dateより厳密に過去のQ5 Day0イベントだけの
    20営業日後リターンの平均、%)を返す純粋関数。

    戻り値: {"status": "ok", "n": int, "mean": float(%、小数1桁)}
         または {"status": "insufficient", "n": int, "mean": None}
            (過去イベント件数がMIN_PRIOR_N未満、または参照データに
             その銘柄自体が存在しない場合。n=0の場合も含む)

    day0_dateそのものの日付、またそれより後のイベントは一切使わない
    (文字列比較 date < day0_date、research側のdate < day0_date定義と同一、
    ISO8601 "YYYY-MM-DD"形式なので文字列比較がそのまま日付比較になる)。
    """
    if data is None:
        data = load_reference_data()
    events = data.get("events_by_ticker", {}).get(ticker, [])
    min_n = data.get("min_prior_n", MIN_PRIOR_N)
    prior = [e["ret20"] for e in events if e["date"] < day0_date]
    n = len(prior)
    if n < min_n:
        return {"status": "insufficient", "n": n, "mean": None}
    return {"status": "ok", "n": n, "mean": round(statistics.mean(prior), 1)}


def get_prior_stats(ticker, day0_date, data=None):
    """get_prior_mean(上記、無変更)と完全に同じ母集団(そのday0_dateより厳密に
    過去の同一銘柄Q5 Day0イベント)から、中央値・勝率・平均最大DD・中央値最大DD
    を追加で計算する(design 2026-10-25、表示・研究専用の追加統計)。

    【絶対に守ること】
    - get_prior_mean()自体は一切変更しない(このファイルの上の関数は無変更)。
      このget_prior_statsは完全に独立した新規関数であり、get_prior_meanの
      フィルタ条件(date < day0_date)を重複実装している(既存関数を壊すリスクを
      ゼロにするため、意図的に共有ヘルパー化していない)。
    - 既存のQ1〜Q5判定・Q5 Day0判定・BUY判定・ranking・20営業日保有ルール・
      Risk-off・switch_recommendation_logic.py・AI_UNIVERSEのいずれにも
      一切使用しない、表示・研究専用の参考情報。

    max_dd20(イベントごとの最大下落幅)の定義(ユーザー確定、2026-10-25):
      Day0終値を基準に、以降20営業日の終値のうち最も下落した時点のリターン(%)。
      Day0自身はmax drawdownの判定対象に含めない。終値ベース(日中安値は
      使わない)。build_q5_prior_mean_reference.pyが生成時に計算済みの値を
      そのまま使う(ここでは再計算しない)。

    戻り値:
      {"status": "ok", "n": int,
       "mean": float, "median": float, "win_rate": float,
       "max_dd_mean": float, "max_dd_median": float}
      または(nがmin_prior_n未満、または参照データにticker自体が存在しない場合)
      {"status": "insufficient", "n": int,
       "mean": None, "median": None, "win_rate": None,
       "max_dd_mean": None, "max_dd_median": None}

    win_rate: prior中、ret20 > 0 となるイベントの割合(%、小数1桁)。
    mean/median/max_dd_mean/max_dd_meanは全て同じprior(day0_dateより厳密に
    過去のイベント)から算出する(mean/median/win_rate/max_dd_mean/
    max_dd_medianは全て同一母集団)。"""
    if data is None:
        data = load_reference_data()
    events = data.get("events_by_ticker", {}).get(ticker, [])
    min_n = data.get("min_prior_n", MIN_PRIOR_N)
    prior = [e for e in events if e["date"] < day0_date]
    n = len(prior)
    if n < min_n:
        return {
            "status": "insufficient", "n": n,
            "mean": None, "median": None, "win_rate": None,
            "max_dd_mean": None, "max_dd_median": None,
        }
    rets = [e["ret20"] for e in prior]
    dds = [e["max_dd20"] for e in prior]
    win_rate = 100.0 * sum(1 for r in rets if r > 0) / n
    return {
        "status": "ok", "n": n,
        "mean": round(statistics.mean(rets), 1),
        "median": round(statistics.median(rets), 1),
        "win_rate": round(win_rate, 1),
        "max_dd_mean": round(statistics.mean(dds), 1),
        "max_dd_median": round(statistics.median(dds), 1),
    }
