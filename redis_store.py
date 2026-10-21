"""Upstash Redis (REST API) への薄いアクセスラッパー。

Renderの無料Webサービスはローカルディスクが永続化されないため、
「銘柄一覧」「各銘柄の最新判定結果」「本日のAPI呼び出し回数」「直近の
自動更新サマリー」はすべてここを経由してUpstash Redisに保存する。

接続断・認証エラーなど想定外の事態でも呼び出し元（app.py / refresh.py）を
クラッシュさせないよう、失敗時は例外を投げずに None / False を返す。
"""

import json
import os
import sys

import requests

_TIMEOUT = 10


def _base_url():
    return os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")


def _token():
    return os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def is_configured():
    return bool(_base_url() and _token())


def _headers():
    return {"Authorization": f"Bearer {_token()}"}


def _request(method, path, **kwargs):
    if not is_configured():
        print("redis_store: UPSTASH_REDIS_REST_URL/TOKENが未設定です", file=sys.stderr)
        return None
    url = f"{_base_url()}/{path}"
    try:
        r = requests.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"redis_store: Upstashへの接続に失敗しました ({path}): {e}", file=sys.stderr)
        return None
    except ValueError:
        print(f"redis_store: Upstashから想定外のレスポンスを受け取りました ({path})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 低レベルコマンド
# ---------------------------------------------------------------------------

def get(key):
    res = _request("GET", f"get/{key}")
    return res.get("result") if res else None


def set_(key, value, ex=None):
    path = f"set/{key}"
    if ex:
        path += f"?EX={ex}"
    res = _request("POST", path, data=value.encode("utf-8"))
    return res is not None


def incrby(key, amount=1):
    res = _request("POST", f"incrby/{key}/{amount}")
    return res.get("result") if res else None


def expire(key, seconds):
    res = _request("POST", f"expire/{key}/{seconds}")
    return res is not None


def set_nx(key, value, ex=None):
    """SET key value NX(キーが未設定の場合のみ書き込む)。
    戻り値: 書き込みに成功した(=キーが未設定だった)ならTrue、
    既にキーが存在して書き込まれなかった場合はFalse。
    Upstash/Redisに到達できない場合もFalse(呼び出し元を待たせない・
    誤って「ロックを取れた」と判定しないためのフェイルセーフ)。"""
    path = f"set/{key}?NX=true"
    if ex:
        path += f"&EX={ex}"
    res = _request("POST", path, data=value.encode("utf-8"))
    if res is None:
        return False
    return res.get("result") == "OK"


# ---------------------------------------------------------------------------
# JSON値のget/set
# ---------------------------------------------------------------------------

def get_json(key, default=None):
    raw = get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def set_json(key, value, ex=None):
    return set_(key, json.dumps(value, ensure_ascii=False), ex=ex)


# ---------------------------------------------------------------------------
# アプリ固有のヘルパー
# ---------------------------------------------------------------------------

def get_watchlist():
    return get_json("watchlist")


def set_watchlist(tickers):
    return set_json("watchlist", list(tickers))


def get_ticker_record(ticker):
    return get_json(f"ticker:{ticker.upper()}")


def set_ticker_record(ticker, record):
    return set_json(f"ticker:{ticker.upper()}", record)


def get_api_budget(date_str):
    v = get(f"api_budget:{date_str}")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def incr_api_budget(date_str, by=1):
    key = f"api_budget:{date_str}"
    new_val = incrby(key, by)
    # 2日でキーを自動失効させ、Redis上に無限に日付キーが溜まらないようにする
    expire(key, 2 * 24 * 3600)
    return new_val


def get_last_refresh():
    return get_json("last_refresh")


def set_last_refresh(summary):
    return set_json("last_refresh", summary)


def get_last_manual_refresh():
    return get("last_manual_refresh")


def set_last_manual_refresh(iso_timestamp):
    return set_("last_manual_refresh", iso_timestamp)


# ---------------------------------------------------------------------------
# Q1〜Q5判定(Twelve Data、quintile_logic.py)専用のキー。
# IMPLEMENTATION_DESIGN_quintile_q1q5.md 2-7節の設計通り、生の株価履歴は
# 保存しない(pred_score等の軽量な計算結果のみ)。既存キー・既存関数は
# 一切変更していない(追加のみ)。
# ---------------------------------------------------------------------------

def get_refpool_score(ticker):
    """参照母集団(REFERENCE_UNIVERSE)1銘柄の最新pred_score等を取得する。
    戻り値: {"pred_score": float, "last_updated": "YYYY-MM-DD", "features": {...}} または None。"""
    return get_json(f"quintile:refpool:{ticker.upper()}")


def set_refpool_score(ticker, data):
    return set_json(f"quintile:refpool:{ticker.upper()}", data)


def get_pool_history():
    """直近252営業日分の参照母集団プール履歴を取得する。
    戻り値: {"dates": [...], "scores_by_date": {"YYYY-MM-DD": [float, ...]}} または None
    (Noneの場合、quintile_logic.update_pool_historyが新規作成する)。"""
    return get_json("quintile:pool_history")


def set_pool_history(data):
    return set_json("quintile:pool_history", data)


def get_td_api_budget(date_str):
    """Twelve Data専用の当日消費credits数を取得する(Alpha Vantage用の
    get_api_budgetとはキーのprefixが異なる別カウンタ、完全に独立)。"""
    v = get(f"td_api_budget:{date_str}")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def incr_td_api_budget(date_str, by=1):
    key = f"td_api_budget:{date_str}"
    new_val = incrby(key, by)
    expire(key, 2 * 24 * 3600)
    return new_val


def get_quintile_state(ticker):
    """ユーザー監視銘柄1件のQ状態履歴を取得する。
    戻り値: {"current_q": "Q3", "previous_q": "Q4", "history": [...]} または None。"""
    return get_json(f"quintile:state:{ticker.upper()}")


def set_quintile_state(ticker, data):
    return set_json(f"quintile:state:{ticker.upper()}", data)


def get_ai_market_env_state():
    """AI関連一斉調整パネル(design 2026-10-14、バックテスト検証済み)の状態を
    取得する。戻り値: {"active": bool, "start_date": "YYYY-MM-DD",
    "f_history": [{"date":..., "f_score":...}, ...], "last_updated": ...} または
    None(未計算時)。既存のQ1〜Q5判定・購入/売却判定とは完全に独立した表示専用
    データで、quintile:state:<TICKER>とは別キーに保存する。"""
    return get_json("ai_market_env:state")


def set_ai_market_env_state(data):
    return set_json("ai_market_env:state", data)


def get_riskoff_state():
    """Risk-off判定(design 2026-10-16、バックテスト検証済み)の状態を取得する。
    戻り値: {"active": bool, "below_streak": int, "above_streak": int,
    "spy_close": float, "sma200": float, "ratio": float, "last_updated": ...}
    または None(未計算時)。既存のQ1〜Q5判定・購入/売却判定・AI一斉調整判定
    とは完全に独立した表示専用データで、他のキーとは別キーに保存する。"""
    return get_json("riskoff:state")


def set_riskoff_state(data):
    return set_json("riskoff:state", data)


def get_portfolio_state():
    """日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md)
    のポートフォリオ記録(ユーザーが「購入を記録」した結果)を取得する。
    戻り値: {"positions": {"<TICKER>": {"entry_date": "YYYY-MM-DD",
    "entry_price": float, "shares": float or None(design 2026-10-18、+50%利益
    100%利確ルール用の任意入力。未入力ならNoneのまま、$ベースの利確計算は行わない)、
    "profit_taken": bool, "profit_taken_date": ... or None,
    "profit_take_sold_shares"/"profit_take_sold_value"/"profit_take_price":
    利確実行時のみ記録(それ以外はキー自体が存在しない)}}, "last_updated": ...}
    または None(未記録時)。既存のQ1〜Q5判定・Day0判定・Risk-off判定とは完全に
    独立したデータで、他のキーとは別キーに保存する。実際の証券口座とは連携しない
    (ユーザーが確認ボタンで記録した内容のみ)。"""
    return get_json("portfolio:state")


def set_portfolio_state(data):
    return set_json("portfolio:state", data)


# ---------------------------------------------------------------------------
# Q5 48銘柄(quintile_logic.ROTATION_UNIVERSE)の将来成績をforward-onlyで
# 蓄積する専用機能(design 2026-10-21)。既存のQ1〜Q5判定・BUY/SELL判定・
# +50%利確・Risk-off・AI一斉調整・portfolio:state・watchlistとは完全に
# 独立した別キーで、ここに追加する関数も既存関数には一切影響しない。
# 新しい別ジョブ(refresh_q5_universe.py)専用の読み書きのみ。
# ---------------------------------------------------------------------------

def get_q5universe_state():
    """48銘柄の将来成績蓄積状態を取得する。
    戻り値: {"prev_q": {"<TICKER>": "Q4", ...}(前回判定時のQ、Day0検出の
    起点。未知のティッカーはキー自体が存在しない=過去を推定しない),
    "entries": [{"ticker","day0_date","day0_price",
    "day5_date","day5_price","day20_date","day20_price",
    "day60_date","day60_price"}, ...](Day0イベントの配列、株数は持たない。
    同一tickerでも(ticker,day0_date)ごとに別イベント),
    "last_processed_date": "YYYY-MM-DD"(この日付までの処理が完全に成功した
    ことを示す。未完了日はこの値を更新しない),
    "latest_price": {"<TICKER>": float, ...}(design 2026-10-21追加、48銘柄
    の当日終値スナップショット。「Q5直近勢い」=day0からの途中経過リターン
    算出専用、entries自体には追加フィールドを持たせない),
    "latest_price_date": "YYYY-MM-DD"(latest_priceの基準日)}
    または None(未初期化時)。latest_price/latest_price_dateが存在しない
    古いstate(本機能追加前に保存されたもの)でも、.get()で安全にNone/空dict
    として扱われる想定(呼び出し側で対応、マイグレーション不要)。

    prev_q・entries・last_processed_date・latest_price・latest_price_dateを
    1つのJSONにまとめているのは、48銘柄全件の取得・判定が完全に成功した
    場合だけ、これらを同時に(1回のRedis書き込みで)確定させるため(design:
    「全件取得・判定→検証→一括確定」、途中書き込みによる不整合を避ける)。"""
    return get_json("q5universe:state")


def set_q5universe_state(data):
    return set_json("q5universe:state", data)


# ---------------------------------------------------------------------------
# Web Push通知(design 2026-10-22: Renderデプロイ検知→Windowsデスクトップ通知)。
# push_notify.pyからのみ使用する。Q1〜Q5・Q5シグナル・株価API・既存の
# watchlist/portfolio/riskoff等のキーには一切関与しない、完全に独立した
# 名前空間(push:*)。
# ---------------------------------------------------------------------------

def get_push_dispatched_commit():
    """サーバーが最後にpush送信を試みた(dispatchした)commit。
    multi-worker/再起動時の「同じcommitへの二重送信」防止に使う。"""
    return get("push:dispatched_commit")


def set_push_dispatched_commit(commit):
    return set_("push:dispatched_commit", commit)


def try_acquire_push_dispatch_lock(commit, ttl_seconds=120):
    """同一commitに対する同時dispatchを1ワーカーだけに限定するための短命ロック。"""
    return set_nx(f"push:dispatch_lock:{commit}", "1", ex=ttl_seconds)


def get_push_notified_commit():
    """ユーザーが実際に通知を目にした(と確認できた)最後のcommit。
    Service Workerのpushイベント確認(ack)、またはフォアグラウンド起動時の
    フォールバック通知のどちらかが発生した時点で更新される。dispatchedとは
    別の値(送信を試みた≠実際に表示された、を区別するため)。"""
    return get("push:notified_commit")


def set_push_notified_commit(commit):
    return set_("push:notified_commit", commit)


def get_push_subscriptions():
    return get_json("push:subscriptions", default=[])


def set_push_subscriptions(subs):
    return set_json("push:subscriptions", subs)
