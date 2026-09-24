"""β(ベータ)表示専用ロジック。

Q1〜Q5判定(quintile_logic.py)とは完全に独立したモジュールであり、
quintile_logic.py側からは一切importされない。ここで計算するβは表示専用の
補足情報であり、pred_score・percentile境界・assign_quintileのいずれの
入力にも使わない(β自体は新しい判定材料としてQ1〜Q5に組み込まない、
ユーザー確定仕様)。

β = 対象銘柄の日次リターンとSPY日次リターンの共分散 / SPY日次リターンの分散
252営業日ローリング、各日はその日"まで"のデータだけを使う(先読みなし)。
検証セッション(β2.5以上でのQ1〜Q5段階性検証)で使ったβの定義・ウィンドウ幅
(252営業日、標本共分散・標本分散、ddof=1)と同一にする。
"""
import statistics

BETA_WINDOW = 252

# (下限[inclusive], 上限[exclusive], キー, 表示ラベル)。ユーザー確定仕様
# (2026-09-25): 境界値は上位側の区分に含める([lo, hi)形式)。
BETA_BANDS = [
    (float("-inf"), 0.8, "low", "低ベータ"),
    (0.8, 1.3, "normal", "標準ベータ"),
    (1.3, 2.5, "high", "高ベータ"),
    (2.5, float("inf"), "extreme", "超高ベータ"),
]

# 超高ベータ(β>=2.5)にのみ添える補足文言。検証結果(β2.5以上でもQ1/Q2と
# Q3/Q4/Q5には一定の差が残っている)を踏まえ、「差なし」とは表示しない
# (ユーザー確定仕様)。Q1〜Q5判定の無効化・Q5除外・購入判定変更は一切行わない。
EXTREME_BETA_NOTE = "Q1〜Q5の段階差は小さめ"


def compute_beta(dates, closes, spy_dates, spy_closes, window=BETA_WINDOW):
    """252営業日ローリングβを計算する(純粋関数、Redis/API呼び出しなし)。

    dates/closes: 対象銘柄の日足終値(古い→新しい順、
    twelvedata_client.fetch_daily_seriesと同じ形式)。
    spy_dates/spy_closes: 同じ形式のSPY日足終値。

    対象銘柄とSPYの日次リターンを日付で突き合わせ、直近window件のペアが
    揃わない場合はNoneを返す(推測値で埋めない、データ不足を明示するため)。
    ここで参照するのはdates/closesに含まれる「その日までの」データのみで、
    将来の価格は一切使わない。
    """
    if not dates or not closes or not spy_dates or not spy_closes:
        return None
    if len(dates) < 2 or len(spy_dates) < 2:
        return None

    spy_ret_by_date = {}
    for i in range(1, len(spy_dates)):
        prev = spy_closes[i - 1]
        if prev:
            spy_ret_by_date[spy_dates[i]] = spy_closes[i] / prev - 1

    paired = []  # (stock_ret, spy_ret)、古い→新しい順
    for i in range(1, len(dates)):
        prev = closes[i - 1]
        if not prev:
            continue
        spy_ret = spy_ret_by_date.get(dates[i])
        if spy_ret is None:
            continue
        paired.append((closes[i] / prev - 1, spy_ret))

    if len(paired) < window:
        return None

    window_pairs = paired[-window:]
    stock_rets = [p[0] for p in window_pairs]
    spy_rets = [p[1] for p in window_pairs]

    spy_var = statistics.variance(spy_rets)  # 標本分散(ddof=1)
    if not spy_var:
        return None
    mean_s = statistics.mean(stock_rets)
    mean_m = statistics.mean(spy_rets)
    cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(stock_rets, spy_rets)) / (len(window_pairs) - 1)
    return cov / spy_var


def classify_beta(beta):
    """βの値からベータ区分キー・表示ラベルを返す(純粋関数)。
    beta=Noneの場合は(None, None)を返す(呼び出し側で「β —」「ベータ算出不可」
    等の表示に振り分ける)。"""
    if beta is None:
        return None, None
    for lo, hi, key, label in BETA_BANDS:
        if lo <= beta < hi:
            return key, label
    return None, None
