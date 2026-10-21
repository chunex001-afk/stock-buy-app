# -*- coding: utf-8 -*-
"""自分の実績(portfolio:state.trade_history)とQ5 48銘柄の仮想実績
(q5universe:state.entries)を比較する集計ロジック(design 2026-10-21)。

Redis/Twelve Dataには一切アクセスしない純粋関数のみ(引数のlist/dictも
変更しない)。読み取り専用の集計であり、既存のQ1〜Q5判定・BUY/SELL判定・
20営業日保有・+50%利確・Risk-off・trade_history/positionsの書き込み
ロジックには一切関与しない。

【「直近N営業日」の境界(design確定仕様)】
今日を0営業日前とし、anchor_idx - date_idx < N(0〜N-1営業日前を含み、
N営業日前は除外)。

【累計の定義(design確定仕様)】
私=trade_history全件(日付フィルタなし)。
Q5=day20_priceが確定している全entriesのday20リターンのみ(本番運用の
「Q5 Day0→最大20営業日保有」と一致させるため、day5/day60は混ぜない)。

【5/20/60営業日ホライズン別の全期間集計(design確定仕様)】
「どのホライズンが最も期待値が高いか」を将来分析できるよう、day5/20/60の
生データ(q5universe:entries)は捨てずに全期間版の集計(all_time_by_horizon)
も別途提供する。
"""
import statistics

import q5_universe_logic

CUMULATIVE_HORIZON = 20


def _aggregate(returns):
    """リターン%のリストから{n, mean, median, win_rate}を計算する。
    win_rateは0超の件数の割合(%)。空リストならn=0、他は全てNone。"""
    n = len(returns)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "win_rate": None}
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": n,
        "mean": statistics.mean(returns),
        "median": statistics.median(returns),
        "win_rate": (wins / n) * 100,
    }


def _within_recent_window(date_idx, anchor_idx, window_days):
    """date_idxがanchor_idxから見て直近window_days営業日以内(今日=0営業日前
    を含み、window_days営業日前は除外)かどうかを返す。date_idx/anchor_idx
    のいずれかがNone、またはdate_idxが未来(anchor_idxより後)の場合はFalse。"""
    if date_idx is None or anchor_idx is None:
        return False
    delta = anchor_idx - date_idx
    return 0 <= delta < window_days


def _safe_trading_day_index(calendar_dates, date_str):
    """date_strがNone(未確定のdayN_date等)の場合はNoneを返す、
    q5_universe_logic.trading_day_indexのNoneセーフなラッパー
    (bisectにNoneを渡すとTypeErrorになるため)。"""
    if date_str is None:
        return None
    return q5_universe_logic.trading_day_index(calendar_dates, date_str)


def trade_returns_in_window(trade_history, trading_calendar, anchor_date, window_days):
    """私のtrade_historyのうち、exit_dateが直近window_days営業日以内の
    return_pctのリストを返す(無ければ空リスト)。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    returns = []
    for trade in trade_history:
        date_idx = _safe_trading_day_index(trading_calendar, trade.get("exit_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days) and trade.get("return_pct") is not None:
            returns.append(trade["return_pct"])
    return returns


def trade_returns_cumulative(trade_history):
    """私のtrade_history全件(日付フィルタなし)のreturn_pctのリストを返す。"""
    return [t["return_pct"] for t in trade_history if t.get("return_pct") is not None]


def _q5_return(entry, horizon):
    """entryのday{horizon}_priceとday0_priceからリターン%を計算する。
    どちらか欠けていればNone(無理に0やNoneから判定しない)。"""
    day0_price = entry.get("day0_price")
    day_price = entry.get(f"day{horizon}_price")
    if day0_price is None or day_price is None:
        return None
    return (day_price / day0_price - 1) * 100


def q5_returns_in_window(q5_entries, trading_calendar, anchor_date, window_days, horizon):
    """Q5 entriesのうち、day{horizon}_dateが直近window_days営業日以内かつ
    day{horizon}_priceが確定しているもののリターン%のリストを返す。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    returns = []
    for entry in q5_entries:
        date_idx = _safe_trading_day_index(trading_calendar, entry.get(f"day{horizon}_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            r = _q5_return(entry, horizon)
            if r is not None:
                returns.append(r)
    return returns


def q5_returns_all_time(q5_entries, horizon):
    """Q5 entries全件(日付フィルタなし)のうち、day{horizon}_priceが確定して
    いるもののリターン%のリストを返す(ホライズン別の全期間分析用)。"""
    returns = []
    for entry in q5_entries:
        r = _q5_return(entry, horizon)
        if r is not None:
            returns.append(r)
    return returns


def q5_returns_cumulative(q5_entries):
    """累計定義(design確定): day20リターンのみ、日付フィルタなし。"""
    return q5_returns_all_time(q5_entries, CUMULATIVE_HORIZON)


def build_comparison_report(trade_history, q5_entries, trading_calendar, anchor_date):
    """比較画面が必要とする集計をすべて組み立てる(design確定仕様)。
    戻り値:
    {
      "recent_window": {
        "5": {"mine": agg, "q5": agg, "diff": float|None},
        "20": {...}, "60": {...},
        "cumulative": {"mine": agg, "q5": agg, "diff": float|None}
      },
      "all_time_by_horizon": {"5": agg, "20": agg, "60": agg}  (Q5のみ)
    }
    trading_calendar/anchor_dateが未確定(空/None)の場合でも例外を出さず、
    直近N営業日版は全て0件として返す(累計・全期間版は営業日カレンダーに
    依存しないため影響を受けない)。"""
    trading_calendar = trading_calendar or []

    def _row(mine_returns, q5_returns):
        mine_agg = _aggregate(mine_returns)
        q5_agg = _aggregate(q5_returns)
        diff = (mine_agg["mean"] - q5_agg["mean"]) if (mine_agg["mean"] is not None and q5_agg["mean"] is not None) else None
        return {"mine": mine_agg, "q5": q5_agg, "diff": diff}

    recent_window = {}
    for horizon in (5, 20, 60):
        mine_returns = trade_returns_in_window(trade_history, trading_calendar, anchor_date, horizon)
        q5_returns = q5_returns_in_window(q5_entries, trading_calendar, anchor_date, horizon, horizon)
        recent_window[str(horizon)] = _row(mine_returns, q5_returns)

    recent_window["cumulative"] = _row(
        trade_returns_cumulative(trade_history),
        q5_returns_cumulative(q5_entries),
    )

    all_time_by_horizon = {
        str(horizon): _aggregate(q5_returns_all_time(q5_entries, horizon))
        for horizon in (5, 20, 60)
    }

    return {"recent_window": recent_window, "all_time_by_horizon": all_time_by_horizon}


MOMENTUM_WINDOWS = (5, 20, 60)


def _momentum_return(entry, latest_price):
    """entryのday0_priceと、48銘柄の当日終値スナップショットlatest_price
    (ticker→price)から、「day0から現在までの途中経過リターン%」を計算する
    (design 2026-10-21、Q5直近勢い)。day5/20/60の確定状況は一切見ない
    (未経過でも計算する、という本機能の目的そのもの)。day0_priceが無い、
    またはそのtickerがlatest_priceに存在しない(取得失敗・未知のticker等)
    場合はNone(無理に0から計算しない)。"""
    day0_price = entry.get("day0_price")
    current_price = (latest_price or {}).get(entry.get("ticker"))
    if day0_price is None or current_price is None:
        return None
    return (current_price / day0_price - 1) * 100


def q5_momentum_matches_in_window(q5_entries, latest_price, trading_calendar, anchor_date, window_days):
    """Q5 entriesのうち、day0_dateが直近window_days営業日以内のものを
    (entry, momentum_return)のペアのリストとして返す(「確定成績」とは
    異なり、day5/20/60の確定を一切問わない)。momentum_returnが計算できない
    (_momentum_returnがNoneを返す)entryはそもそも結果に含めない。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    matches = []
    for entry in q5_entries:
        date_idx = _safe_trading_day_index(trading_calendar, entry.get("day0_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            r = _momentum_return(entry, latest_price)
            if r is not None:
                matches.append((entry, r))
    return matches


def build_momentum_report(q5_entries, latest_price, trading_calendar, anchor_date):
    """「Q5直近勢い」(design 2026-10-21)の集計を組み立てる。5/20/60の各
    ウィンドウについて{n, mean, median, win_rate, unique_day0_dates}を返す。
    5/20/60のウィンドウは入れ子(直近5営業日のDay0は直近20・60営業日の
    条件も満たす)であり、「確定成績」の排他的な行とは性質が異なる
    (design上の意図した挙動)。latest_price未確定(空dict/None)の場合、
    全ホライズンでn=0として返す(エラーにはしない、マイグレーション不要)。"""
    trading_calendar = trading_calendar or []
    report = {}
    for window_days in MOMENTUM_WINDOWS:
        matches = q5_momentum_matches_in_window(q5_entries, latest_price, trading_calendar, anchor_date, window_days)
        returns = [r for _, r in matches]
        unique_day0_dates = len({entry["day0_date"] for entry, _ in matches})
        agg = _aggregate(returns)
        agg["unique_day0_dates"] = unique_day0_dates
        report[str(window_days)] = agg
    return report


def enrich_entries_with_returns(q5_entries):
    """Q5 entries個別一覧表示用に、保存されていないリターン%を都度計算して
    付与したコピーを返す(引数は変更しない、Redisには保存しない)。"""
    enriched = []
    for entry in q5_entries:
        copy = dict(entry)
        for horizon in (5, 20, 60):
            copy[f"day{horizon}_return_pct"] = _q5_return(entry, horizon)
        enriched.append(copy)
    return enriched
