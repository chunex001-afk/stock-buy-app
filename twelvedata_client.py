"""Twelve Data取得の共通モジュール(Q1〜Q5判定専用)。

既存の`stock_logic.py`(Alpha Vantage、既存の購入判定=upside_score等)とは
完全に独立したモジュール。APIキー・予算管理・レート制限は一切共有しない。
`refresh.py`のQ1〜Q5処理からのみ呼ばれる想定で、`app.py`からは呼ばない
(Render側でTwelve Dataを直接呼ばない、日次バッチのみで完結させる方針)。

Twelve Data Basicプラン: 800 credits/day、8 credits/minute(2026-09-16、
公式ドキュメント twelvedata.com/docs・/pricing で確認)。
`/time_series`は「1シンボル=1 credit」で、outputsize・adjust・interval
パラメータはcredit消費に影響しない(同日実測・公式ドキュメント両方で確認済み)。
"""
import os
import re
import threading
import time

import requests

_APIKEY_RE = re.compile(r"apikey=[^&\s'\")]+", re.IGNORECASE)


def _redact(text):
    """通信エラーのメッセージにAPIキー付きURLがそのまま含まれることがあるため、
    ログ・エラーメッセージに出す前に必ず伏字にする。"""
    return _APIKEY_RE.sub("apikey=***", str(text))


API_URL = "https://api.twelvedata.com/time_series"

# Q1〜Q5判定に必要な履歴日数(MA200=200日+バッファ)を満たす最小限のoutputsize。
# 日次本番取得ではこれで十分(過去5年分のバックテスト検証で使ったoutputsize=1500は
# 再検証専用であり、日次取得には不要)。
OUTPUTSIZE = 300
ADJUST = "splits"  # 分割のみ調整、配当調整はしない(本番仕様確定・2026-09-16)

# Alpha Vantage側の`stock_logic.MIN_CALL_INTERVAL`とは完全に独立した、
# Twelve Data専用のスロットル間隔。8 credits/minute制限に対し実績のある
# 約8秒間隔を初期値とする(環境変数で上書き可能、過剰な待機を避けるため)。
TWELVEDATA_MIN_CALL_INTERVAL = float(os.environ.get("TWELVEDATA_MIN_CALL_INTERVAL", "8.0"))

# Basicプラン実枠は800 credits/dayだが、日次バッチの想定消費(SPY 1 + ユーザー銘柄
# 最大15 + ローテーション3〜4 = 19〜20/日)に対して十分な安全マージンを残しつつ、
# 想定外の暴走呼び出しを検知できる自己申告の上限(stock_logic.DAILY_API_BUDGET=22
# と同じ考え方だが、Twelve Data用に完全に別立て)。
DAILY_API_BUDGET = 700

_lock = threading.Lock()
_last_call_ts = 0.0


class TdApiError(Exception):
    """Twelve Data呼び出しに関する分類済みエラー。

    attempts: このエラーに至るまでに実際にTwelve Dataへ送信したHTTPリクエスト回数。
    呼び出し元はこれを使ってTwelve Data専用の予算カウンタを加算する
    (Alpha Vantage側の`stock_logic.ApiError`とは別系統)。
    """

    def __init__(self, error_type, message, attempts=1):
        self.error_type = error_type
        self.message = message
        self.attempts = attempts
        super().__init__(message)


def _throttle():
    global _last_call_ts
    with _lock:
        wait = TWELVEDATA_MIN_CALL_INTERVAL - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def classify_error(data):
    """Twelve Dataのレスポンス本体からエラー種別を判定する。

    Twelve Dataのエラーレスポンスは {"code": int, "message": str, "status": "error"} 形式
    (2026-09-16、実際のAPIレスポンスで確認済み: 404=シンボル不正、401=APIキー不正)。
    戻り値: (error_type, message)。正常時は (None, None)。
    """
    if not isinstance(data, dict):
        return "UNKNOWN", "想定外のAPIレスポンス形式です"
    if data.get("status") != "error":
        return None, None
    code = data.get("code")
    message = str(data.get("message", ""))
    if code == 429:
        return "RATE_LIMIT", message
    if code in (401, 403):
        return "AUTH", message
    if code in (400, 404):
        return "INVALID_SYMBOL", message
    return "UNKNOWN", message


def api_get(params, api_key, retry_network=True):
    """Twelve Dataへの1コール。ネットワーク系エラーのみ最大1回リトライする。

    戻り値: (data, attempts)。attemptsは実際にTwelve Dataへ送信したHTTPリクエスト回数。
    """
    if not api_key:
        raise TdApiError("UNKNOWN", "TWELVEDATA_API_KEYが未設定です", attempts=0)

    p = dict(params)
    p["apikey"] = api_key
    max_attempts = 2 if retry_network else 1
    last_exc = None

    for attempt in range(max_attempts):
        _throttle()
        attempts_made = attempt + 1
        try:
            r = requests.get(API_URL, params=p, timeout=20)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            raise TdApiError("NETWORK", f"通信エラー: {_redact(e)}", attempts=attempts_made)
        except requests.RequestException as e:
            raise TdApiError("NETWORK", f"通信エラー: {_redact(e)}", attempts=attempts_made)

        # Twelve Dataはエラー時もHTTPステータスコード(404/401/429等)とあわせて
        # {"code","message","status":"error"}形式のJSON本体を返すため、
        # raise_for_status()で早期に弾かず、まずJSON本体からの分類を試みる。
        try:
            data = r.json()
        except ValueError:
            raise TdApiError(
                "UNKNOWN", f"Twelve DataからJSONを受け取れませんでした(HTTP {r.status_code})",
                attempts=attempts_made,
            )

        err_type, err_msg = classify_error(data)
        if err_type:
            raise TdApiError(err_type, _redact(err_msg), attempts=attempts_made)
        return data, attempts_made

    # ここには到達しない想定だが、念のため
    raise TdApiError("NETWORK", f"通信エラー: {_redact(last_exc)}", attempts=max_attempts)


def fetch_daily_series(ticker, api_key, outputsize=OUTPUTSIZE):
    """日足の終値・出来高を古い順に取得する(adjust=splits、配当調整なし)。
    戻り値: (dates, closes, volumes, attempts)。attemptsは実際のAPI呼び出し回数。
    stock_logic.fetch_daily_seriesと同じ戻り値の形にして、quintile_logic側の
    利用コードをAlpha Vantage/Twelve Dataで共通化できるようにしている。"""
    data, attempts = api_get(
        {"symbol": ticker, "interval": "1day", "outputsize": outputsize, "adjust": ADJUST},
        api_key,
    )
    values = data.get("values")
    if not values or not isinstance(values, list):
        raise TdApiError("NO_DATA", "株価データを取得できませんでした", attempts=attempts)

    rows = []
    for v in values:
        try:
            d = v["datetime"]
            close = float(v["close"])
            volume = int(float(v.get("volume", 0)))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((d, close, volume))
    rows.sort(key=lambda x: x[0])  # 古い順

    if len(rows) < 20:
        raise TdApiError("NO_DATA", "データ不足のため判定できません", attempts=attempts)

    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    volumes = [r[2] for r in rows]
    return dates, closes, volumes, attempts
