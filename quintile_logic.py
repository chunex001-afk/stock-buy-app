"""Q1〜Q5状態判定ロジック(IMPLEMENTATION_DESIGN_quintile_q1q5.md参照)。

既存のstock_logic.py(app.py/refresh.pyが使う購入判定)とは完全に独立したモジュール。
既存の`compute_upside_score`等のロジックには一切触れない。DAILY_API_BUDGET=22・
MAX_TICKERS=15はstock_logic.py側の値のままで、このモジュールは変更しない。
"""
import heapq
import json
import math
import os
import statistics
from datetime import date

import stock_logic

FEATURES = [
    "rsi", "ma20_dev", "ma50_dev", "ma200_dev", "high_gap",
    "trail_ret60", "vol60", "volume_ratio", "rel_strength_spy",
]

REFERENCE_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quintile_reference_data.json")
Q5_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quintile_q5_stats.json")

K_NEIGHBORS = 100

# データ不足銘柄の判定基準(2026-09-22、SKHY調査を受けて追加)。
# 9特徴量それぞれの必要レコード数(closesの長さ、stock_logic.sma・
# quintile_logic._trail_ret60/_vol60の実装から算出):
#   rsi=15, ma20_dev=20, volume_ratio=20, high_gap=常に計算可,
#   ma50_dev=50, trail_ret60=61, vol60=61, ma200_dev=200,
#   rel_strength_spy=trail_ret60(61件)に加え、当日分のSPYデータの有無にも依存。
# 銘柄自身の価格履歴の長さだけで決まる特徴量のうち最大値はma200_dev=200件で、
# これが実質的なボトルネックになる。rel_strength_spyは対象銘柄の履歴の長さでは
# なく「その日SPYを取得できたか」にも依存するため、この基準には含めない
# (backfill_quintile_history_for_new_tickerが元々SPY未取得の日はrel_strength_spy=None
# のままTRAIN中央値補完に委ねている、design 2026-09-17と同じ扱いを踏襲する)。
# ma200_dev(200件)を満たせば、rsi/ma20_dev/ma50_dev/volume_ratio/trail_ret60/vol60は
# 自動的にすべて満たされる(200 > 61 > 50 > 20 > 15のため)。
MIN_HISTORY_FOR_FULL_FEATURES = 200


def has_min_history_for_quintile(n_records):
    """9特徴量(rel_strength_spyを除く)がすべて計算可能になる最小レコード数
    (MIN_HISTORY_FOR_FULL_FEATURES=200、ma200_devの要件)を満たしているかを返す。
    quintile_logic.compute_features自体は変更しない(9特徴量の定義・欠損補完
    ロジックには一切触れない、判定基準を追加するだけの純粋関数)。"""
    return n_records >= MIN_HISTORY_FOR_FULL_FEATURES


_reference_cache = None
_q5_stats_cache = None


def load_reference_data(path=REFERENCE_DATA_PATH):
    """quintile_reference_data.json(TRAIN凍結データ)を読み込む。プロセス内で使い回す。"""
    global _reference_cache
    if _reference_cache is None:
        with open(path, encoding="utf-8") as f:
            _reference_cache = json.load(f)
    return _reference_cache


def load_q5_stats(path=Q5_STATS_PATH):
    """Q5の過去実績統計(design 2-8)を読み込む。バックテスト時に固定された参照値で
    あり、日次では再計算しない(app.pyの表示専用、将来予測ではないことを明示する)。"""
    global _q5_stats_cache
    if _q5_stats_cache is None:
        with open(path, encoding="utf-8") as f:
            _q5_stats_cache = json.load(f)
    return _q5_stats_cache


def _change_pct_series(closes):
    """各時点の前日比変化率(%)の系列。closes[0]に対応する要素はNone。
    validation/data/build_augmented.pyのchange_pct定義と同一(単純な前日比)。"""
    series = [None]
    for i in range(1, len(closes)):
        series.append((closes[i] / closes[i - 1] - 1) * 100)
    return series


def _trail_ret60(closes):
    """(price / 60営業日レコード前のprice - 1) * 100。
    validation/data/build_augmented.pyのtrail_ret60定義と同一(i>=60、closes[-61]基準)。"""
    if len(closes) < 61:
        return None
    return (closes[-1] / closes[-61] - 1) * 100


def _vol60(closes):
    """直近60件(当日含む)の日次change_pctの母標準偏差。60件中30件以上有効値が必要。
    validation/data/build_augmented.pyのvol60定義と同一(i>=60が計算条件)。"""
    if len(closes) < 61:
        return None
    change_pct = _change_pct_series(closes)
    window = [v for v in change_pct[-60:] if v is not None]
    if len(window) < 30:
        return None
    return statistics.pstdev(window)


def compute_features(dates, closes, volumes, spy_dates=None, spy_closes=None):
    """Stage14〜28で確定した9特徴量を計算する(ロジックは一切変更しない、
    IMPLEMENTATION_DESIGN_quintile_q1q5.md 2-1参照)。

    rsi・high_gap・volume_ratioは既存stock_logic.compute_indicatorsの値をそのまま使う
    (重複実装を避ける、design 2-1の方針通り)。ma20_dev/ma50_dev/ma200_dev・
    trail_ret60・vol60・rel_strength_spyはここで新規計算する。

    履歴不足で計算できない特徴量はNoneを返す(欠損補完はknn_predict_score側の
    TRAIN中央値補完で行う、compute_features自体では補完しない)。

    spy_dates/spy_closesを渡さない場合、rel_strength_spyは常にNoneになる。
    """
    ind = stock_logic.compute_indicators(dates, closes, volumes)
    price = closes[-1]

    ma20 = stock_logic.sma(closes, 20)
    ma50 = stock_logic.sma(closes, 50)
    ma200 = stock_logic.sma(closes, 200)

    ma20_dev = (price / ma20 - 1) * 100 if ma20 else None
    ma50_dev = (price / ma50 - 1) * 100 if ma50 else None
    ma200_dev = (price / ma200 - 1) * 100 if ma200 else None

    trail_ret60 = _trail_ret60(closes)
    vol60 = _vol60(closes)

    rel_strength_spy = None
    if trail_ret60 is not None and spy_dates and spy_closes:
        spy_index = {d: i for i, d in enumerate(spy_dates)}
        spy_idx = spy_index.get(dates[-1])
        if spy_idx is not None and spy_idx >= 60:
            spy_trail_ret60 = (spy_closes[spy_idx] / spy_closes[spy_idx - 60] - 1) * 100
            rel_strength_spy = trail_ret60 - spy_trail_ret60

    return {
        "rsi": ind["rsi"],
        "ma20_dev": ma20_dev,
        "ma50_dev": ma50_dev,
        "ma200_dev": ma200_dev,
        "high_gap": ind["high_gap"],
        "trail_ret60": trail_ret60,
        "vol60": vol60,
        "volume_ratio": ind["volume_ratio"],
        "rel_strength_spy": rel_strength_spy,
    }


def _standardize(raw_features, reference):
    """9特徴量の生の値(Noneを含みうる)を、TRAIN中央値補完→TRAIN標準化した
    9次元ベクトルに変換する(design 2-1の欠損補完方針)。"""
    vector = []
    for j, name in enumerate(FEATURES):
        v = raw_features.get(name)
        if v is None:
            v = reference["median_raw"][j]
        standardized = (v - reference["mean"][j]) / reference["std"][j]
        vector.append(standardized)
    return vector


def knn_predict_score(raw_features, reference=None, k=K_NEIGHBORS):
    """9特徴量からpred_scoreを計算する(design 2-2)。

    TRAIN凍結データ(quintile_reference_data.json)に対し、9次元の標準化ベクトルで
    ユークリッド距離によるk近傍(k=100)を計算し、近傍のmaxup60値の中央値を返す。
    numpyは使わずpure Pythonで実装する(design 2-2の方針通り)。
    """
    if reference is None:
        reference = load_reference_data()

    query = _standardize(raw_features, reference)
    records = reference["records"]

    distances = []
    for rec in records:
        x = rec["x"]
        dist_sq = 0.0
        for a, b in zip(query, x):
            diff = a - b
            dist_sq += diff * diff
        distances.append((dist_sq, rec["maxup60"]))

    nearest = heapq.nsmallest(k, distances, key=lambda t: t[0])
    neighbor_maxup60 = [maxup60 for _, maxup60 in nearest]
    return statistics.median(neighbor_maxup60)


# ---------------------------------------------------------------------------
# ローテーション取得(design 2-4・section 3。Stage29で確定した49銘柄→14グループ)
#
# 2026-09-16のTwelve Data本番実装レビューで、SPYはrel_strength_spy特徴量の
# 鮮度を保つため14日ローテーションから外し毎日直接取得する方針に変更した
# (ユーザー確定方針)。REFERENCE_UNIVERSE(プール構成員49銘柄、分位計算対象)
# 自体はStage24〜28の検証時と同一のまま変更せず、ローテーション対象だけを
# ROTATION_UNIVERSE(SPYを除いた48銘柄)に分離する。
# ---------------------------------------------------------------------------

REFERENCE_UNIVERSE = [
    "AAOI", "AEHR", "AI", "ALAB", "AMD", "APLD", "ARM", "ASML", "AVGO", "AXTI",
    "BE", "CAT", "CEG", "CIEN", "COHR", "CRDO", "CRWD", "CRWV", "DELL", "DLR",
    "EQIX", "GEV", "IREN", "JNJ", "JPM", "LITE", "LSCC", "MCHP", "MPWR", "MRVL",
    "MU", "NBIS", "NOW", "NRG", "NVDA", "ON", "PATH", "PG", "PLTR", "QCOM",
    "SMCI", "SNDK", "SNOW", "TSM", "VRT", "VST", "WMT", "XOM", "SPY",
]

# ローテーション対象(SPYを除いた48銘柄、REFERENCE_UNIVERSEと同じ並び順を維持)。
ROTATION_UNIVERSE = [sym for sym in REFERENCE_UNIVERSE if sym != "SPY"]

NUM_ROTATION_GROUPS = 14
ROTATION_EPOCH = "2023-01-01"

# 48銘柄をROTATION_UNIVERSEの並び順でindex % 14に割り当てたときの、各グループの銘柄数。
GROUP_SIZES = [0] * NUM_ROTATION_GROUPS
for _idx, _sym in enumerate(ROTATION_UNIVERSE):
    GROUP_SIZES[_idx % NUM_ROTATION_GROUPS] += 1
del _idx, _sym


def rotation_group_for_date(date_str, num_groups=NUM_ROTATION_GROUPS, epoch=ROTATION_EPOCH):
    """当日の日付だけから機械的にローテーショングループ番号を求める(design 2-4)。
    Redisに状態を持たないステートレスな純粋関数で、未来の日付は一切参照しない。
    実行が飛んだ日があっても、以降のグループ計算には影響しない。"""
    days_elapsed = (date.fromisoformat(date_str) - date.fromisoformat(epoch)).days
    return days_elapsed % num_groups


def tickers_for_group(group_idx):
    """指定グループ番号(0〜13)に属する銘柄一覧を返す(ROTATION_UNIVERSEのindex % 14、
    SPYはローテーション対象外で毎日別途直接取得する)。"""
    return [sym for i, sym in enumerate(ROTATION_UNIVERSE) if i % NUM_ROTATION_GROUPS == group_idx]


# ---------------------------------------------------------------------------
# ローリング252営業日分位判定(design 2-3)
# ---------------------------------------------------------------------------

POOL_WINDOW_DAYS = 252
PERCENTILE_POINTS = (20, 40, 60, 80)


def update_pool_history(pool_history, date_str, scores_by_ticker, max_days=POOL_WINDOW_DAYS):
    """当日の49銘柄(取得できた分のみ)の最新pred_scoreをプール履歴に追加し、
    max_days(252営業日)を超えた古い日付を切り捨てる(design 2-3・2-7)。

    pool_history: {"dates": [...], "scores_by_date": {"YYYY-MM-DD": [float, ...]}}形式
    (Noneなら新規作成)。当日までに取得済みのscores_by_tickerのみを使い、
    当日より後の日付のデータは一切参照しない(未来データリークなし、design section 5)。
    副作用なし(新しいdictを返す、引数のpool_historyは変更しない)。
    """
    if pool_history is None:
        pool_history = {"dates": [], "scores_by_date": {}}
    dates = list(pool_history.get("dates", []))
    scores_by_date = dict(pool_history.get("scores_by_date", {}))

    scores_today = [
        scores_by_ticker[sym] for sym in REFERENCE_UNIVERSE
        if scores_by_ticker.get(sym) is not None
    ]
    if date_str not in scores_by_date:
        dates.append(date_str)
    scores_by_date[date_str] = scores_today

    dates.sort()
    if len(dates) > max_days:
        for old_date in dates[:-max_days]:
            scores_by_date.pop(old_date, None)
        dates = dates[-max_days:]

    return {"dates": dates, "scores_by_date": scores_by_date}


def _percentile_linear(sorted_values, p):
    """numpy.percentile(デフォルトのlinear補間)と同一の結果を返す、pure Python実装。"""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = (p / 100) * (n - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[int(idx)]
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def pool_percentile_bounds(pool_history, percentiles=PERCENTILE_POINTS):
    """pool_historyに蓄積された直近252営業日×49銘柄のpred_scoreをフラット化した
    分布全体から20/40/60/80パーセンタイルを計算する(design 2-3)。
    プールが空ならNoneを返す。"""
    all_scores = [
        v for day_scores in pool_history.get("scores_by_date", {}).values()
        for v in day_scores
    ]
    if not all_scores:
        return None
    all_scores.sort()
    return [_percentile_linear(all_scores, p) for p in percentiles]


def assign_quintile(score, bounds):
    """pred_scoreとQ1〜Q5境界(bounds=[Q1/Q2, Q2/Q3, Q3/Q4, Q4/Q5]の4値)から
    Q1〜Q5を決定する純粋関数(design 2-3)。score・boundsだけを見る、状態を持たない関数。"""
    if score < bounds[0]:
        return "Q1"
    if score < bounds[1]:
        return "Q2"
    if score < bounds[2]:
        return "Q3"
    if score < bounds[3]:
        return "Q4"
    return "Q5"
