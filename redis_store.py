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
