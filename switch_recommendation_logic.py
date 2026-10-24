# -*- coding: utf-8 -*-
"""Q5「乗り換え推奨」表示機能(design 2026-10-24)の判定ロジック。

市場環境バイアス研究+最終ホールドアウト検証(本リポジトリ外のバックテスト、
2026-10)で選定された4条件すべてを満たした場合のみ推奨を出す、表示専用の
純粋関数。

【絶対に守ること】
- 既存のQ5 Day0判定・Q5 acceptance・Q5順位付け・BUY判定・prior_mean計算・
  prior_n計算・20日保有ルール・Risk-off判定には一切関与しない(ここの関数の
  戻り値をそれらの判定条件に使ってはならない、逆方向の依存もない)。
- 新しいQ5判定ロジック・新しいprior_mean計算ロジック・新しい順位付けロジック
  は一切作らない。呼び出し側(app._compute_daily_actions)が既存ロジックだけで
  組み立てたbuy_candidates(既存のis_day0判定・既存のq5_prior_mean_logic.
  get_prior_mean結果・既存のwatchlist順)をフィルタするだけ。
- 自動売買・自動売却・自動乗り換えは一切行わない。Redis/HTTPへのアクセスも
  一切行わない。
"""

# 市場環境バイアス研究(AI_UNIVERSE当日リターンの閾値をTRAIN/TESTウォーク
# フォワード5分割+2025年以降の完全ホールドアウトで検証)で選ばれた固定閾値。
# AI_UNIVERSE当日騰落率がこの値以下の日を「AI市場が弱い日」とする。
AI_UNIVERSE_THRESHOLD_PCT = -2.0

# 乗り換え先候補のprior_mean条件(同研究の最終候補、3.0%以上4.5%未満)。
PRIOR_MEAN_LO = 3.0
PRIOR_MEAN_HI = 4.5

# 表示件数の上限(ユーザー確定仕様)。
MAX_CANDIDATES = 3


def _empty_result(ai_universe_daily_return, threshold_pct):
    return {
        "active": False,
        "ai_universe_daily_return": ai_universe_daily_return,
        "threshold_pct": threshold_pct,
        "candidates": [],
    }


def compute_switch_recommendation(buy_candidates, positions, ai_universe_daily_return,
                                   threshold_pct=AI_UNIVERSE_THRESHOLD_PCT):
    """乗り換え推奨の判定。以下の4条件をすべて満たした場合のみactive=Trueを返す。

    条件① 現在Q5を保有している: positionsが空でない。
    条件② AI市場が弱い日: ai_universe_daily_return <= threshold_pct。
    条件③ 新しいQ5 Day0が存在する: buy_candidatesの中にbuy_candidates由来の
      候補が存在する(buy_candidates自体が既存のis_day0判定+保有中銘柄除外
      済みのリストのため、ここで新しい判定は行わない)。
    条件④ prior_n>=10かつprior_mean 3.0%以上4.5%未満: 既存のq5_prior_mean_logic.
      get_prior_meanの戻り値(status/n/mean)をそのまま読むだけ(status=="ok"は
      その定義上prior_n>=MIN_PRIOR_N(=10)を満たした場合にのみ返る値なので、
      ここで改めてn>=10を計算し直す必要はない)。

    引数:
      buy_candidates: app._compute_daily_actionsが構築した、保有中銘柄を
        除外済み・is_day0のみ・watchlist順のリスト
        ([{"ticker","suggested_price","prior_mean":{"status","n","mean"}}])。
        truncate(free_slots)前のリストを渡すこと(乗り換え推奨は空き枠の
        有無とは無関係なため)。
      positions: 現在の保有銘柄dict(_compute_daily_actionsのpositionsを
        そのまま、{"<TICKER>": {...}}形式)。条件①の判定にのみ使う。
      ai_universe_daily_return: 当日のAI_UNIVERSE(既存の41銘柄定義)当日
        騰落率(%)。refresh._update_ai_market_decline_stateが既存の取得
        データ(ATH計算用に既に取得済みのcloses配列)から追加計算した値を
        そのまま使う(新規API呼び出しなし)。None(未計算・取得失敗・
        trading_dateとのズレ等)の場合は条件②を満たさないものとして常に
        active=Falseにする(架空の判定はしない)。
      threshold_pct: 条件②の閾値(既定-2.0、研究で選定済みの固定値)。

    戻り値: {"active": bool, "ai_universe_daily_return": float|None,
             "threshold_pct": float,
             "candidates": [{"ticker","prior_mean","prior_n"}, ...]
             (最大MAX_CANDIDATES件、buy_candidatesの順序通り=既存のQ5順位・
             Day0順位・watchlist順をそのまま使う、新しい順位付けはしない)}
    """
    if not positions:
        return _empty_result(ai_universe_daily_return, threshold_pct)
    if ai_universe_daily_return is None or ai_universe_daily_return > threshold_pct:
        return _empty_result(ai_universe_daily_return, threshold_pct)

    candidates = []
    for c in buy_candidates:
        pm = c.get("prior_mean") or {}
        if pm.get("status") != "ok":
            continue
        mean = pm.get("mean")
        if mean is None or not (PRIOR_MEAN_LO <= mean < PRIOR_MEAN_HI):
            continue
        candidates.append({"ticker": c.get("ticker"), "prior_mean": mean, "prior_n": pm.get("n")})
        if len(candidates) >= MAX_CANDIDATES:
            break

    return {
        "active": bool(candidates),
        "ai_universe_daily_return": ai_universe_daily_return,
        "threshold_pct": threshold_pct,
        "candidates": candidates,
    }
