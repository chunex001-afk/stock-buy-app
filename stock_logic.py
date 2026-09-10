"""Alpha Vantage取得・指標計算・購入判定ロジックの共通モジュール。

app.py（Webサービス）と refresh.py（日次自動更新ジョブ）の両方から import される。
"""

import time
import threading
from datetime import datetime, timezone, timedelta

import requests

API_URL = "https://www.alphavantage.co/query"
DEFAULT_TICKERS = ["AXT", "NBIS", "AEHR", "MU", "SNDK", "BE", "IONQ", "CRDO"]
MAX_TICKERS = 15

# Alpha Vantage無料枠は25 requests/dayだが、リトライ・手動更新の余裕を
# 残すため、アプリ側ではさらに厳しい自己申告の上限を設ける。
DAILY_API_BUDGET = 22

MIN_CALL_INTERVAL = 1.05  # 無料プランのバースト制限対策（秒）

_lock = threading.Lock()
_last_call_ts = 0.0


class ApiError(Exception):
    """Alpha Vantage呼び出しに関する分類済みエラー。"""

    def __init__(self, error_type, message):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


def _throttle():
    global _last_call_ts
    with _lock:
        wait = MIN_CALL_INTERVAL - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def classify_error(data):
    """Alpha Vantageのレスポンス本体からエラー種別を判定する。

    戻り値: (error_type, message) 正常時は (None, None)
    """
    if not isinstance(data, dict):
        return "UNKNOWN", "想定外のAPIレスポンス形式です"
    if data.get("Note"):
        return "RATE_LIMIT", str(data["Note"])
    if data.get("Information"):
        info = str(data["Information"])
        lowered = info.lower()
        if "rate limit" in lowered or "frequency" in lowered or "per day" in lowered or "premium" in lowered:
            return "RATE_LIMIT", info
        return "UNKNOWN", info
    if data.get("Error Message"):
        return "INVALID_SYMBOL", str(data["Error Message"])
    return None, None


def api_get(params, api_key, retry_network=True):
    """Alpha Vantageへの1コール。ネットワーク系エラーのみ最大1回リトライする。"""
    if not api_key:
        raise ApiError("UNKNOWN", "ALPHAVANTAGE_API_KEYが未設定です")

    p = dict(params)
    p["apikey"] = api_key
    attempts = 2 if retry_network else 1
    last_exc = None

    for attempt in range(attempts):
        _throttle()
        try:
            r = requests.get(API_URL, params=p, timeout=20)
            r.raise_for_status()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(2)
                continue
            raise ApiError("NETWORK", f"通信エラー: {e}")
        except requests.RequestException as e:
            raise ApiError("NETWORK", f"通信エラー: {e}")

        try:
            data = r.json()
        except ValueError:
            raise ApiError("UNKNOWN", "Alpha VantageからJSONを受け取れませんでした")

        err_type, err_msg = classify_error(data)
        if err_type:
            raise ApiError(err_type, err_msg)
        return data

    # ここには到達しない想定だが、念のため
    raise ApiError("NETWORK", f"通信エラー: {last_exc}")


def fetch_daily_series(ticker, api_key):
    """日足の終値・出来高を古い順に取得する。"""
    data = api_get(
        {"function": "TIME_SERIES_DAILY", "symbol": ticker, "outputsize": "compact"},
        api_key,
    )
    series = data.get("Time Series (Daily)")
    if not series or not isinstance(series, dict):
        raise ApiError("NO_DATA", "株価データを取得できませんでした")

    rows = []
    for d, v in series.items():
        try:
            close = float(v["4. close"])
            volume = int(float(v.get("5. volume", 0)))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((d, close, volume))
    rows.sort(key=lambda x: x[0])

    if len(rows) < 20:
        raise ApiError("NO_DATA", "データ不足のため判定できません")

    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    volumes = [r[2] for r in rows]
    return dates, closes, volumes


def fetch_news(tickers, api_key):
    """対象銘柄群についてのニュースをまとめて1コールで取得する。失敗時は空リスト。"""
    if not tickers:
        return []
    try:
        data = api_get(
            {
                "function": "NEWS_SENTIMENT",
                "tickers": ",".join(tickers),
                "sort": "LATEST",
                "limit": 50,
            },
            api_key,
            retry_network=False,
        )
    except ApiError:
        return []

    items = []
    for x in data.get("feed", []) if isinstance(data, dict) else []:
        published = x.get("time_published", "")
        try:
            dt = datetime.strptime(published[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - dt > timedelta(hours=36):
                continue
        except Exception:
            pass

        matched = [ts.get("ticker") for ts in x.get("ticker_sentiment", []) if ts.get("ticker") in tickers]
        if not matched:
            continue

        items.append(
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "source": x.get("source", ""),
                "published": published,
                "tickers": matched,
            }
        )
    return items[:30]


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi14(closes):
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    n = 14
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _ma_slope(values, n, lookback=5):
    """直近lookback営業日でのn日移動平均の傾き（現在MA - lookback日前のMA）。"""
    if len(values) < n + lookback:
        return None
    cur = sma(values, n)
    prev = sma(values[: len(values) - lookback], n)
    if cur is None or prev is None:
        return None
    return cur - prev


def compute_indicators(dates, closes, volumes):
    latest = closes[-1]
    prev = closes[-2]
    one_month = closes[-22] if len(closes) >= 22 else closes[0]
    window = closes[-63:] if len(closes) >= 63 else closes

    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    ma20_slope = _ma_slope(closes, 20, 5)
    rsi = rsi14(closes)

    recent_high = max(window)
    recent_low = min(window)
    high_gap = (latest / recent_high - 1) * 100
    low_gap = (latest / recent_low - 1) * 100

    avg_vol20 = sma(volumes, 20)
    vol_ratio = (volumes[-1] / avg_vol20) if avg_vol20 else None

    return {
        "last_trade_date": dates[-1],
        "price": round(latest, 2),
        "change_pct": round((latest / prev - 1) * 100, 2),
        "month_return": round((latest / one_month - 1) * 100, 2),
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma50": round(ma50, 2) if ma50 is not None else None,
        "ma20_slope": ma20_slope,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "high_gap": round(high_gap, 2),
        "low_gap": round(low_gap, 2),
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
    }


# ---------------------------------------------------------------------------
# 底打ち状態 / 局面 / スコア / 最終判定
# ---------------------------------------------------------------------------

def compute_bottom_status(ind):
    price, ma20, ma50 = ind["price"], ind["ma20"], ind["ma50"]
    ma20_slope, rsi, low_gap = ind["ma20_slope"], ind["rsi"], ind["low_gap"]

    if price is None or ma20 is None or rsi is None or ma20_slope is None:
        return None

    if price > ma20 and ma20_slope > 0 and 45 <= rsi <= 65:
        return "底打ち確認"

    below_ma = price < ma20 or (ma50 is not None and price < ma50)
    if below_ma and 30 <= rsi < 45 and ma20_slope >= 0:
        return "底打ち途中"

    if low_gap is not None and low_gap <= 8 and ma20_slope < 0 and rsi < 40:
        return "底打ち未確認"

    return "底打ち未確認" if price < ma20 else "底打ち途中"


def compute_phase(ind, bottom_status):
    rsi, high_gap = ind["rsi"], ind["high_gap"]
    month_return, change_pct = ind["month_return"], ind["change_pct"]

    if (
        high_gap is not None and high_gap >= -3
        and rsi is not None and rsi > 70
        and month_return is not None and month_return >= 25
    ):
        return "天井局面"

    if bottom_status in ("底打ち途中", "底打ち確認") and change_pct is not None and change_pct > 0:
        return "反発局面"

    return None


def compute_upside_score(ind, bottom_status, phase):
    score = 50.0
    high_gap = ind["high_gap"]
    if high_gap is not None:
        score += max(0.0, min(25.0, -high_gap * 0.8))

    ma20, ma50 = ind["ma20"], ind["ma50"]
    if ma20 is not None and ma50 is not None:
        score += 8 if ma20 > ma50 else -5

    rsi = ind["rsi"]
    if rsi is not None:
        if 45 <= rsi <= 65:
            score += 10
        elif 30 <= rsi < 45:
            score += 6
        elif rsi > 70:
            score -= 10

    if bottom_status == "底打ち確認":
        score += 12
    elif bottom_status == "底打ち途中":
        score += 6

    if phase == "反発局面":
        score += 8
    elif phase == "天井局面":
        score -= 30

    return max(0, min(100, round(score)))


def compute_overheat_score(ind, phase):
    score = 0.0
    rsi = ind["rsi"]
    if rsi is not None:
        if rsi > 75:
            score += 35
        elif rsi > 70:
            score += 20
        elif rsi > 65:
            score += 8

    high_gap = ind["high_gap"]
    if high_gap is not None and high_gap >= -3:
        score += 20

    month_return = ind["month_return"]
    if month_return is not None:
        if month_return >= 25:
            score += 20
        elif month_return >= 15:
            score += 10

    if phase == "天井局面":
        score += 25

    return max(0, min(100, round(score)))


JUDGMENTS = (
    "強い買い候補",
    "買い候補",
    "先回り候補",
    "底打ち待ち",
    "過熱のため待つ",
    "天井圏のため見送り",
    "判定不可",
)


def compute_judgment(bottom_status, phase, upside, overheat):
    if bottom_status is None:
        return "判定不可"

    if phase == "天井局面" or overheat >= 70:
        return "天井圏のため見送り"

    if overheat >= 50:
        return "過熱のため待つ"

    if bottom_status == "底打ち未確認":
        return "底打ち待ち"

    if bottom_status == "底打ち途中":
        return "先回り候補" if upside >= 65 else "底打ち待ち"

    if bottom_status == "底打ち確認":
        if upside >= 75:
            return "強い買い候補"
        if upside >= 55:
            return "買い候補"
        return "底打ち待ち"

    return "判定不可"


def build_comment(judgment, ind, related_news=None):
    rsi = ind.get("rsi")
    high_gap = ind.get("high_gap")
    rsi_s = f"{rsi:.1f}" if rsi is not None else "-"
    hg_s = f"{high_gap:.1f}" if high_gap is not None else "-"

    if related_news:
        return f"ニュース: {related_news[0].get('title', '')}"

    if judgment in ("強い買い候補", "買い候補"):
        return f"底打ち確認。RSIは健全圏（{rsi_s}）で、過熱感が低く上昇余地が大きい。"
    if judgment == "先回り候補":
        return "底打ちの途中段階だが、反発の兆しがあり上昇余地も大きい。確認前のため一部先回りで検討。"
    if judgment == "底打ち待ち":
        return "底打ちはまだ確認できない。下落トレンドが止まるまで待ち。"
    if judgment in ("過熱のため待つ", "天井圏のため見送り"):
        return f"上昇トレンドは強いが、RSI（{rsi_s}）と高値乖離（{hg_s}%）から過熱感が強く、今は追わない方がよい。"
    return "データが不足しているため判定できません。"


def build_result(ticker, dates, closes, volumes, news_items=None):
    """指標計算〜最終判定までをまとめて実行し、Redisに保存する形のレコードを返す。"""
    ind = compute_indicators(dates, closes, volumes)
    bottom_status = compute_bottom_status(ind)
    phase = compute_phase(ind, bottom_status)
    upside = compute_upside_score(ind, bottom_status, phase)
    overheat = compute_overheat_score(ind, phase)
    judgment = compute_judgment(bottom_status, phase, upside, overheat)

    related_news = [n for n in (news_items or []) if ticker in n.get("tickers", [])]
    comment = build_comment(judgment, ind, related_news)

    return {
        "ticker": ticker,
        "last_trade_date": ind["last_trade_date"],
        "price": ind["price"],
        "change_pct": ind["change_pct"],
        "month_return": ind["month_return"],
        "rsi14": ind["rsi"],
        "ma20": ind["ma20"],
        "ma50": ind["ma50"],
        "high_gap": ind["high_gap"],
        "volume_ratio": ind["volume_ratio"],
        "upside_score": upside,
        "overheat_score": overheat,
        "bottom_status": bottom_status,
        "phase": phase,
        "judgment": judgment,
        "comment": comment,
        "news": related_news[:3],
    }
