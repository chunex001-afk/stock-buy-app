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


def _trading_days_held(entry_date, as_of_date, trading_calendar):
    """entry_dateからas_of_dateまでの経過営業日数を返す(どちらかが
    trading_calendarに見つからない場合はNone、架空の値は作らない)。"""
    entry_idx = _safe_trading_day_index(trading_calendar, entry_date)
    as_of_idx = _safe_trading_day_index(trading_calendar, as_of_date)
    if entry_idx is None or as_of_idx is None:
        return None
    return as_of_idx - entry_idx


def my_confirmed_returns(trade_history, positions, current_price_by_ticker,
                          trading_calendar, anchor_date, threshold_days):
    """私の「①保有確定」(design 2026-10-21確定仕様)。実際の購入日
    (entry_date)を起点に、経過営業日数がthreshold_days以上(「20営業日
    保有を基本運用として扱う」という考え方を5/60にも適用し、「少なくとも
    threshold_days営業日経過した=その時点の運用結果が確定的に評価できる」
    ものを対象にする)。

    - 売却済み(trade_history): 実際の保有期間(entry_date→exit_date)が
      threshold_days以上なら、実際のreturn_pct(exit_price基準)を採用する
      (保有期間がthreshold_days未満で売却した取引は対象外、まだ
      threshold_days分の運用結果とは言えないため)
    - 保有中(positions): 経過営業日数(entry_date→anchor_date=今日)が
      threshold_days以上(基本運用期間を超えて保有が続いている)場合、
      現在価格を暫定の確定値として採用する(Q5のday5/20/60_priceのような
      厳密な「ちょうどその日」の凍結値ではなく、既存データ構造の制約上、
      現在価格を代用する近似値であることに注意)

    戻り値: return%のリスト。"""
    returns = []
    for trade in trade_history:
        held = _trading_days_held(trade.get("entry_date"), trade.get("exit_date"), trading_calendar)
        if held is not None and held >= threshold_days and trade.get("return_pct") is not None:
            returns.append(trade["return_pct"])

    for ticker, pos in (positions or {}).items():
        held = _trading_days_held(pos.get("entry_date"), anchor_date, trading_calendar)
        if held is not None and held >= threshold_days:
            entry_price = pos.get("entry_price")
            current_price = (current_price_by_ticker or {}).get(ticker)
            if entry_price is not None and current_price is not None:
                returns.append((current_price / entry_price - 1) * 100)
    return returns


def build_my_confirmed_report(trade_history, positions, current_price_by_ticker,
                               trading_calendar, anchor_date):
    """私の「①保有確定」ブロック(design確定仕様)。5/20/60営業日それぞれ
    について{n, mean, median, win_rate}を返す。Q5側の「確定成績」
    (recent_window[...].q5、既存・無変更)と並べて表示する想定。"""
    trading_calendar = trading_calendar or []
    return {
        str(n): _aggregate(my_confirmed_returns(trade_history, positions, current_price_by_ticker,
                                                 trading_calendar, anchor_date, n))
        for n in MOMENTUM_WINDOWS
    }


def my_recent_purchases(trade_history, positions, trading_calendar, anchor_date, window_days):
    """私の「②現在価格」の対象選別(design確定仕様)。実際の購入日
    (entry_date)が直近window_days営業日以内の購入を、保有中(open)と
    売却済み(sold)に分けて返す(design確定仕様: 現在価格ブロックでは
    売却済みを「別途」扱うため、2つのリストに分離したまま返す。混ぜて
    1つの平均にしない)。

    戻り値: (open_positions_list, sold_trades_list)。open_positions_listの
    各要素は(ticker, pos)のタプル。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    open_list, sold_list = [], []
    for trade in trade_history:
        date_idx = _safe_trading_day_index(trading_calendar, trade.get("entry_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            sold_list.append(trade)
    for ticker, pos in (positions or {}).items():
        date_idx = _safe_trading_day_index(trading_calendar, pos.get("entry_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            open_list.append((ticker, pos))
    return open_list, sold_list


def build_my_current_price_report(trade_history, positions, current_price_by_ticker,
                                   trading_calendar, anchor_date):
    """私の「②現在価格」ブロック(design確定仕様)。実際の購入日が直近
    5/20/60営業日以内の購入について、保有中は実際の取得価格→現在価格の
    mark-to-market、売却済みは実績として確定した売却リターンを「別途」
    返す(1つの平均に混ぜない)。

    戻り値: {"5": {"open": agg, "sold": agg}, "20": {...}, "60": {...}}。
    Q5側の「直近勢い」(momentum、既存・無変更)のopenと並べて比較する想定。"""
    trading_calendar = trading_calendar or []
    report = {}
    for window_days in MOMENTUM_WINDOWS:
        open_list, sold_list = my_recent_purchases(trade_history, positions, trading_calendar,
                                                     anchor_date, window_days)
        open_returns = []
        for ticker, pos in open_list:
            entry_price = pos.get("entry_price")
            current_price = (current_price_by_ticker or {}).get(ticker)
            if entry_price is not None and current_price is not None:
                open_returns.append((current_price / entry_price - 1) * 100)
        sold_returns = [t["return_pct"] for t in sold_list if t.get("return_pct") is not None]
        report[str(window_days)] = {"open": _aggregate(open_returns), "sold": _aggregate(sold_returns)}
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


# ---------------------------------------------------------------------------
# 「③実運用成績」(design 2026-10-21最終確定仕様)。
#
# 目的: 「自分がQ5から選んで実際に購入している銘柄群が、Q5 48銘柄を機械的に
# 運用した場合と比べて本当に良いのか」を、資金運用成績ではなく銘柄単位の
# 騰落率で継続的に検証する。株数・資金額・NAV・複利・再投資は一切使わない
# (_aggregate()がリターン%のリストから平均・中央値・勝率を計算するだけ)。
#
# Q5側: Q5 Day0を仮想購入日とし、「原則20営業日保有→20営業日後に仮想売却」
# という既存の運用ルールと同じ基準で評価する。20営業日未満ならDay0→現在
# 価格(まだ仮想売却前なのでmark-to-market)、20営業日以上ならDay0→20営業日後
# 価格で固定する(day20_priceが一度確定したら、その後は絶対に現在価格を
# 使わない)。既存のQ5確定成績・Q5直近勢いとは別の第3の計算で、既存の
# build_comparison_report/build_momentum_reportは一切変更しない。
#
# 私側: 実際の購入日(entry_date)を基準に、売却済みはentry_price→exit_price
# (既存のreturn_pctをそのまま使う)、保有中はentry_price→現在価格。
#
# 観測期間(5/20/60/120/180営業日)は「保有期間」ではなく「現在から遡った
# 観測ウィンドウ」。day0_date/entry_dateがウィンドウ内であれば、Q5側の
# 20営業日運用が完了済みかどうか・私側が売却済みかどうかを問わず対象に
# する(「まだ経過していないから除外」はしない)。
# ---------------------------------------------------------------------------

ACTUAL_PERFORMANCE_WINDOWS = (5, 20, 60, 120, 180)
ACTUAL_PERFORMANCE_HOLD_DAYS = 20  # Q5側の仮想運用ルール(既存のHOLD_DAYSと
                                    # 同じ考え方だが、app.py側の既存HOLD_DAYS
                                    # 定数には一切触れない、ここだけの定数)


def _q5_actual_performance_return(entry, latest_price, trading_calendar, anchor_date):
    """Q5側の「①Q5 48」用リターン(design 2026-10-21改訂: 20営業日SELL
    ルール厳守のため、現在価格の代用を20営業日未満に限定)。
    ・day20_priceが既に確定していればそれで固定する(20営業日経過後は
    現在価格を二度と使わない。day20_priceは既存のq5_universe_logic.
    fill_checkpointsにより、一度確定したら以後変更されない値であることが
    既に保証されている)。
    ・day20_priceが未確定でも、Day0から20営業日未満ならまだ仮想売却前
    なのでday0→現在価格のmark-to-marketを使って良い。
    ・day20_priceが未確定で、かつDay0から20営業日以上経過している場合は
    「本来は20営業日時点で仮想売却して確定しているはずなのに、その価格が
    取得できていない」状態であり、現在価格を絶対に代用してはいけない
    (遡及補完が追いつくまでは未確定としてNoneを返し、集計から除外する)。
    20営業日の経過判定は、他の機能(my_confirmed_returns等)と同じ
    _trading_days_held(既存の営業日カレンダー/trading_day_index)で行う
    (calendar日数ではなく営業日数で判定する)。day0_priceが無ければNone。"""
    day0_price = entry.get("day0_price")
    if day0_price is None:
        return None
    day20_price = entry.get("day20_price")
    if day20_price is not None:
        return (day20_price / day0_price - 1) * 100
    elapsed = _trading_days_held(entry.get("day0_date"), anchor_date, trading_calendar)
    if elapsed is not None and elapsed >= ACTUAL_PERFORMANCE_HOLD_DAYS:
        return None  # 20営業日以上経過済みなのにday20_price未確定 → 現在価格を使わず未確定扱い
    current_price = (latest_price or {}).get(entry.get("ticker"))
    if current_price is None:
        return None
    return (current_price / day0_price - 1) * 100


def q5_actual_performance_returns(q5_entries, latest_price, trading_calendar, anchor_date, window_days):
    """day0_dateが直近window_days営業日以内のQ5 entry全件(20営業日運用が
    完了済みかどうかを問わない)について、_q5_actual_performance_returnの
    リターン%のリストを返す。戻り値は(returns, total, unconfirmed)のタプル。
    total=観測ウィンドウ内のDay0件数(対象件数)、unconfirmed=そのうち
    20営業日以上経過済みなのにday20_priceが未確定で集計から除外された件数
    (returns側はtotal-unconfirmed件=確定件数になる)。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    returns = []
    total = 0
    for entry in q5_entries:
        date_idx = _safe_trading_day_index(trading_calendar, entry.get("day0_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            total += 1
            r = _q5_actual_performance_return(entry, latest_price, trading_calendar, anchor_date)
            if r is not None:
                returns.append(r)
    unconfirmed = total - len(returns)
    return returns, total, unconfirmed


def my_actual_performance_returns(trade_history, positions, current_price_by_ticker,
                                   trading_calendar, anchor_date, window_days):
    """私側の「②私の実運用」用リターン(design確定仕様)。entry_date(購入日)
    が直近window_days営業日以内の購入(売却済み・保有中を問わず)を対象に、
    売却済みは既存のreturn_pct(実売却価格基準)、保有中はentry_price→
    現在価格のmark-to-marketでリターン%のリストを返す。"""
    anchor_idx = _safe_trading_day_index(trading_calendar, anchor_date)
    returns = []
    for trade in trade_history:
        date_idx = _safe_trading_day_index(trading_calendar, trade.get("entry_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days) and trade.get("return_pct") is not None:
            returns.append(trade["return_pct"])
    for ticker, pos in (positions or {}).items():
        date_idx = _safe_trading_day_index(trading_calendar, pos.get("entry_date"))
        if _within_recent_window(date_idx, anchor_idx, window_days):
            entry_price = pos.get("entry_price")
            current_price = (current_price_by_ticker or {}).get(ticker)
            if entry_price is not None and current_price is not None:
                returns.append((current_price / entry_price - 1) * 100)
    return returns


def build_actual_performance_report(q5_entries, trade_history, positions, latest_price,
                                     current_price_by_ticker, trading_calendar, anchor_date):
    """「③実運用成績」(design確定仕様、2026-10-21改訂: Q5側未確定件数の
    可視化を追加)。5/20/60/120/180営業日それぞれについて、Q5側(①Q5 48を
    機械的に20営業日運用した場合の成績)・私側(②私が実際に購入した銘柄群
    の成績)・差(私−Q5)を返す。Q5側のaggには通常の{n, mean, median,
    win_rate}に加えて、observation window内のDay0総数(total)と、20営業日
    以上経過済みなのにday20_price未確定で集計から除外された件数
    (unconfirmed)を追加する(n = total - unconfirmedの関係、データ欠損で
    nが減っていることが分かるようにするため)。私側にはこの概念が無いため
    追加しない。
    戻り値: {"5": {"q5": agg(+total/unconfirmed), "mine": agg, "mean_diff",
    "median_diff", "win_rate_diff"}, "20": {...}, "60": {...}, "120": {...},
    "180": {...}}。"""
    trading_calendar = trading_calendar or []
    report = {}
    for window_days in ACTUAL_PERFORMANCE_WINDOWS:
        q5_returns, q5_total, q5_unconfirmed = q5_actual_performance_returns(
            q5_entries, latest_price, trading_calendar, anchor_date, window_days)
        mine_returns = my_actual_performance_returns(trade_history, positions, current_price_by_ticker,
                                                       trading_calendar, anchor_date, window_days)
        q5_agg = _aggregate(q5_returns)
        q5_agg["total"] = q5_total
        q5_agg["unconfirmed"] = q5_unconfirmed
        mine_agg = _aggregate(mine_returns)

        def _diff(key):
            a, b = mine_agg.get(key), q5_agg.get(key)
            return (a - b) if (a is not None and b is not None) else None

        report[str(window_days)] = {
            "q5": q5_agg, "mine": mine_agg,
            "mean_diff": _diff("mean"), "median_diff": _diff("median"), "win_rate_diff": _diff("win_rate"),
        }
    return report
