"""Alpha Vantage取得・指標計算・購入判定ロジックの共通モジュール。

app.py（Webサービス）と refresh.py（日次自動更新ジョブ）の両方から import される。
"""

import re
import time
import threading
from datetime import datetime, timezone, timedelta

import requests

_APIKEY_RE = re.compile(r"apikey=[^&\s'\")]+", re.IGNORECASE)


def _redact(text):
    """requestsの通信エラーはURL（apikey付き）をそのまま文字列化することがあるため、
    ログ・エラーメッセージに出す前に必ずAPIキーを伏字にする。"""
    return _APIKEY_RE.sub("apikey=***", str(text))

API_URL = "https://www.alphavantage.co/query"
DEFAULT_TICKERS = ["AXTI", "NBIS", "AEHR", "MU", "SNDK", "BE", "IONQ", "CRDO"]
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
            raise ApiError("NETWORK", f"通信エラー: {_redact(e)}")
        except requests.RequestException as e:
            raise ApiError("NETWORK", f"通信エラー: {_redact(e)}")

        try:
            data = r.json()
        except ValueError:
            raise ApiError("UNKNOWN", "Alpha VantageからJSONを受け取れませんでした")

        err_type, err_msg = classify_error(data)
        if err_type:
            raise ApiError(err_type, _redact(err_msg))
        return data

    # ここには到達しない想定だが、念のため
    raise ApiError("NETWORK", f"通信エラー: {_redact(last_exc)}")


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
    """対象銘柄群についてのニュース（センチメント付き）をまとめて1コールで取得する。

    失敗時は空リスト。各アイテムには一致した銘柄ごとの
    {"score": float, "relevance": float, "label": str} を持つ "sentiment" 辞書を含める。
    """
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

        sentiment = {}
        for ts in x.get("ticker_sentiment", []):
            tk = ts.get("ticker")
            if tk not in tickers:
                continue
            try:
                score = float(ts.get("ticker_sentiment_score", 0))
            except (TypeError, ValueError):
                score = 0.0
            try:
                relevance = float(ts.get("relevance_score", 0))
            except (TypeError, ValueError):
                relevance = 0.0
            sentiment[tk] = {
                "score": score,
                "relevance": relevance,
                "label": ts.get("ticker_sentiment_label", ""),
            }
        if not sentiment:
            continue

        items.append(
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "source": x.get("source", ""),
                "published": published,
                "tickers": list(sentiment.keys()),
                "sentiment": sentiment,
            }
        )
    return items[:30]


def aggregate_sentiment(ticker, news_items):
    """関連度で重み付けした平均センチメントスコア（おおむね-1〜1）を返す。データが無ければNone。"""
    total_w = 0.0
    total = 0.0
    for n in news_items or []:
        s = (n.get("sentiment") or {}).get(ticker)
        if not s:
            continue
        w = max(s.get("relevance", 0) or 0, 0.05)
        total += (s.get("score", 0) or 0) * w
        total_w += w
    if total_w == 0:
        return None
    return round(total / total_w, 3)


def _clean_overview_field(v):
    """Alpha VantageのOVERVIEWは値がない項目を文字列"None"で返すことがあるため空文字に正規化する。"""
    v = (v or "").strip()
    return "" if v in ("", "None", "-", "N/A") else v


def fetch_overview(ticker, api_key):
    """OVERVIEWエンドポイントから時価総額・セクター・業種を取得する（日次では最大1銘柄のみ呼ぶ）。"""
    data = api_get({"function": "OVERVIEW", "symbol": ticker}, api_key, retry_network=False)
    if not isinstance(data, dict) or not data.get("Symbol"):
        raise ApiError("NO_DATA", "企業情報を取得できませんでした")

    cap_raw = data.get("MarketCapitalization")
    try:
        market_cap = int(cap_raw) if cap_raw not in (None, "", "None") else None
    except (TypeError, ValueError):
        market_cap = None

    return {
        "market_cap": market_cap,
        "name": _clean_overview_field(data.get("Name")),
        "sector": _clean_overview_field(data.get("Sector")),
        "industry": _clean_overview_field(data.get("Industry")),
    }


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
# 底打ち状態 / 局面 / スコア（内部判定材料。最終判定そのものではない）
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


# ---------------------------------------------------------------------------
# 時価総額
# ---------------------------------------------------------------------------

_MARKET_CAP_TIERS = (
    (200_000_000_000, "超大型株"),
    (10_000_000_000, "大型株"),
    (2_000_000_000, "中型株"),
    (300_000_000, "小型株"),
)


def classify_market_cap(market_cap):
    if not market_cap or market_cap <= 0:
        return None
    for threshold, label in _MARKET_CAP_TIERS:
        if market_cap >= threshold:
            return label
    return "超小型株"


def format_market_cap(market_cap):
    if not market_cap or market_cap <= 0:
        return None
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.1f}B"
    return f"${market_cap / 1_000_000:.0f}M"


_SECTOR_THEME_JA = {
    "TECHNOLOGY": "テクノロジー",
    "LIFE SCIENCES": "ライフサイエンス",
    "MANUFACTURING": "製造業",
    "ENERGY & TRANSPORTATION": "エネルギー・輸送",
    "FINANCE": "金融",
    "TRADE & SERVICES": "商業・サービス",
    "REAL ESTATE & CONSTRUCTION": "不動産・建設",
}

# 主要銘柄については、OVERVIEWのSector/Industryより具体的で自然な事業テーマ文言を優先する。
# 未収録の銘柄はOVERVIEW由来のsector/industryにフォールバックする（theme_label参照）。
_TICKER_THEMES = {
    "MU": "メモリ・DRAM・HBM（AI向け高帯域メモリ）需要",
    "SNDK": "NAND型フラッシュ・ストレージ需要",
    "NBIS": "AIデータセンター・GPUインフラ",
    "BE": "燃料電池・水素発電・データセンター向け電力",
    "IONQ": "量子コンピューティング",
    "CRDO": "AIデータセンター向け光接続・高速インターコネクト",
    "AXTI": "化合物半導体基板（GaAs/InP）",
    "AEHR": "半導体バーンイン・テスト装置（SiC/AI関連）",
    "NVDA": "AI向けGPU・データセンター半導体",
    "AMD": "AI/サーバー向けCPU・GPU",
    "TSM": "先端半導体ファウンドリ（AI/HPC向け）",
    "SMCI": "AIサーバー・データセンター向けサーバー機器",
    "PLTR": "AI・データ分析ソフトウェア基盤",
    "ARM": "半導体設計IP（モバイル・AI向け）",
    "SOUN": "音声認識・会話型AI",
    "VRT": "データセンター向け電力・冷却インフラ",
    "OKLO": "小型モジュール原子炉（データセンター電力向け）",
    "CCJ": "ウラン採掘・原子力燃料",
    "AAPL": "iPhone・サービス収益中心の総合テック",
    "MSFT": "クラウド(Azure)・AI・エンタープライズソフトウェア",
    "GOOGL": "検索広告・クラウド・AI(Gemini)",
    "AMZN": "EC・クラウド(AWS)",
    "META": "SNS広告・AI基盤投資",
    "TSLA": "EV・自動運転・エネルギー貯蔵",
}


def theme_label(sector, industry):
    industry = (industry or "").strip()
    sector = (sector or "").strip()
    ja_sector = _SECTOR_THEME_JA.get(sector.upper()) if sector else None
    if industry:
        return f"{ja_sector}／{industry}" if ja_sector else industry
    if sector:
        return ja_sector or sector
    return None


def adjust_scores_for_context(upside, overheat, sentiment_score, cap_label):
    """ニュースセンチメントと時価総額規模で、テクニカル由来のスコアを小幅に補正する。
    いずれか単体で判定が決まらないよう、影響量は控えめに留める。"""
    if sentiment_score is not None:
        if sentiment_score >= 0.35:
            upside += 6
        elif sentiment_score >= 0.15:
            upside += 3
        elif sentiment_score <= -0.35:
            upside -= 8
            overheat += 5
        elif sentiment_score <= -0.15:
            upside -= 4

    if cap_label in ("超小型株", "小型株"):
        overheat += 5
        upside -= 2
    elif cap_label in ("超大型株", "大型株"):
        overheat -= 3
        upside += 2

    return max(0, min(100, round(upside))), max(0, min(100, round(overheat)))


# ---------------------------------------------------------------------------
# 最終判定（4種類）
# ---------------------------------------------------------------------------

JUDGMENTS = (
    "強く買いたい",
    "買い候補",
    "まだ買わない",
    "過熱のため買わない",
    "判定不可",
)

# ランキング表示順（数字が小さいほど上位）
JUDGMENT_ORDER = {
    "強く買いたい": 0,
    "買い候補": 1,
    "まだ買わない": 2,
    "過熱のため買わない": 3,
    "判定不可": 4,
}

OVERHEAT_JUDGMENT_THRESHOLD = 55

ERROR_LABELS = {
    "RATE_LIMIT": "APIレート制限",
    "NETWORK": "通信エラー",
    "INVALID_SYMBOL": "無効な銘柄コード",
    "NO_DATA": "データ取得不可",
    "UNKNOWN": "不明なエラー",
}


def compute_final_judgment(bottom_status, phase, upside, overheat):
    """最終判定は「強く買いたい／買い候補／まだ買わない／過熱のため買わない」の4種類のみ。
    上昇しすぎている（過熱）場合は、底打ち状態に関わらず必ず「過熱のため買わない」を優先する。"""
    if bottom_status is None:
        return "判定不可"

    if phase == "天井局面" or overheat >= OVERHEAT_JUDGMENT_THRESHOLD:
        return "過熱のため買わない"

    if bottom_status == "底打ち確認" and upside >= 72:
        return "強く買いたい"

    if bottom_status in ("底打ち確認", "底打ち途中") and upside >= 52:
        return "買い候補"

    return "まだ買わない"


def sort_rows(rows):
    """最終判定順→上昇余地スコア降順でソートする。app.py（当日順位）とrefresh.py
    （前日順位スナップショット）の両方から同一基準で使われる。"""
    return sorted(
        rows,
        key=lambda x: (
            JUDGMENT_ORDER.get(x.get("judgment"), 99),
            -(x.get("upside_score") if x.get("upside_score") is not None else -1),
        ),
    )


# ---------------------------------------------------------------------------
# 一言コメント生成（銘柄ごとに材料の異なる文章を組み立てる）
# ---------------------------------------------------------------------------

_CONCLUSION_TEXT = {
    "強く買いたい": "総合的に強く買いたい局面",
    "買い候補": "総合的に買い候補と判断",
    "まだ買わない": "総合的にまだ買い時ではない",
    "過熱のため買わない": "過熱感が強く今は買わないほうが無難",
}


def _shorten(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _news_clause(related_news, sentiment_score):
    """ニュースが取得できている場合の最優先クローズ。件名をそのまま埋め込むため、
    銘柄ごとに内容が必ず変わる（定型文の使い回しにならない）。センチメントの強弱で
    表現の温度感も変える。"""
    if not related_news:
        return None
    title = _shorten(related_news[0].get("title", ""), 40)
    if not title:
        return None
    if sentiment_score is not None:
        if sentiment_score >= 0.35:
            return f"「{title}」など強い好材料が支え"
        if sentiment_score >= 0.15:
            return f"「{title}」など好材料が追い風"
        if sentiment_score <= -0.35:
            return f"「{title}」など強い悪材料が重石"
        if sentiment_score <= -0.15:
            return f"「{title}」など懸念材料が重石"
    return f"「{title}」が材料視されている"


def _theme_clause(ticker, sector, industry):
    """ニュースが無い場合に使う、銘柄固有の事業テーマ。主要銘柄は具体的な文言（MU/SNDK/NBIS等）を、
    それ以外はOVERVIEW由来のsector/industryを使う。"""
    label = _TICKER_THEMES.get(ticker) or theme_label(sector, industry)
    if not label:
        return None
    return f"{_shorten(label, 26)}が事業テーマ"


def _bottom_clause(bottom_status, phase, change_pct):
    if phase == "天井局面":
        return "高値圏で上昇一服の兆し"
    if bottom_status == "底打ち確認":
        return "底打ち後の反発局面に入っている" if (change_pct or 0) > 0 else "底打ちを確認済み"
    if bottom_status == "底打ち途中":
        return "底打ちの途中段階でまだ確証はない"
    if bottom_status == "底打ち未確認":
        return "下落トレンドが続き底打ちは未確認"
    return None


def _rsi_clause(rsi):
    if rsi is None:
        return None
    if rsi >= 80:
        return f"RSI{rsi:.0f}は極端な過熱圏"
    if rsi >= 75:
        return f"RSI{rsi:.0f}は明確な過熱圏"
    if rsi >= 65:
        return f"RSI{rsi:.0f}はやや過熱気味"
    if rsi >= 55:
        return f"RSI{rsi:.0f}は底堅く推移"
    if rsi >= 45:
        return f"RSI{rsi:.0f}は中立圏で方向感に乏しい"
    if rsi >= 30:
        return f"RSI{rsi:.0f}はやや弱含み"
    return f"RSI{rsi:.0f}は売られ過ぎ圏"


def _trend_clause(ma20, ma50):
    if ma20 is None or ma50 is None:
        return None
    return "MA20がMA50を上回り上昇基調を維持" if ma20 > ma50 else "MA20がMA50を下回り軟調な基調"


def _highgap_clause(high_gap):
    if high_gap is None:
        return None
    if high_gap >= -2:
        return "直近高値圏まで値を戻している"
    if high_gap <= -25:
        return f"直近高値から{abs(high_gap):.0f}%下押しした水準"
    if high_gap <= -12:
        return f"直近高値から{abs(high_gap):.0f}%ほど調整した水準"
    return None


def _volume_clause(volume_ratio):
    if volume_ratio is None:
        return None
    if volume_ratio >= 2.5:
        return "出来高が急増し関心が急激に高まっている"
    if volume_ratio >= 1.8:
        return "出来高も急増しており関心が高い"
    if volume_ratio <= 0.6:
        return "出来高は細く商いは閑散"
    return None


def _month_clause(month_return):
    if month_return is None:
        return None
    if month_return >= 40:
        return f"1か月で{month_return:.0f}%超と急騰し値動きが極めて速い"
    if month_return >= 30:
        return f"1か月で{month_return:.0f}%超急騰しており値動きが速い"
    if month_return >= 15:
        return f"1か月で{month_return:.0f}%上昇と勢いが強い"
    if month_return <= -30:
        return f"1か月で{abs(month_return):.0f}%超下落し下げが加速"
    if month_return <= -15:
        return f"1か月で{abs(month_return):.0f}%下落している"
    return None


def _daychange_clause(change_pct):
    """当日の値動きが大きい場合のみ言及する（前日比が地味な日は省略して情報過多を避ける）。"""
    if change_pct is None:
        return None
    if change_pct >= 8:
        return f"本日は+{change_pct:.1f}%と急伸"
    if change_pct >= 5:
        return f"本日は+{change_pct:.1f}%高"
    if change_pct <= -8:
        return f"本日は{change_pct:.1f}%と急落"
    if change_pct <= -5:
        return f"本日は{change_pct:.1f}%安"
    return None


def _capsize_clause(cap_label):
    if cap_label in ("超小型株", "小型株"):
        return f"{cap_label}のため値動きが荒くなりやすい"
    if cap_label in ("超大型株", "大型株"):
        return f"{cap_label}で値動きは比較的安定的"
    return None


def _ranked_support_clauses(judgment, ind, bottom_c, rsi_c, trend_c, hg_c, vol_c, month_c, day_c, cap_c):
    """テクニカル材料候補を「その銘柄にとってどれだけ特徴的か」でスコア付けし、
    重要度順に並べ替える。固定の文面パターンを繰り返すのではなく、実際の数値が
    突出している指標ほど採用されやすくすることで、似た判定同士でも銘柄ごとに
    異なる材料が前面に出るようにする。"""
    rsi = ind.get("rsi")
    month_return = ind.get("month_return")
    change_pct = ind.get("change_pct")

    candidates = []

    def add(text, weight):
        if text:
            candidates.append((weight, text))

    # 底打ち状態・局面は「なぜこの判定か」の中核情報。過熱判定では相対的に重要度を下げる。
    add(bottom_c, 25 if judgment in ("強く買いたい", "買い候補", "まだ買わない") else 8)

    rsi_weight = abs((rsi if rsi is not None else 50) - 50) * 0.5
    if judgment == "過熱のため買わない":
        rsi_weight *= 1.6
    add(rsi_c, rsi_weight)

    month_weight = abs(month_return or 0) * 0.6
    if judgment == "過熱のため買わない":
        month_weight *= 1.3
    add(month_c, month_weight)

    add(day_c, abs(change_pct or 0) * 1.2)
    add(hg_c, 16)
    add(vol_c, 11)
    add(trend_c, 7)
    add(cap_c, 4)

    candidates.sort(key=lambda c: -c[0])
    ranked = []
    for _, text in candidates:
        if text not in ranked:
            ranked.append(text)
    return ranked


def build_comment(ticker, judgment, ind, bottom_status, phase, cap_label, sector, industry,
                   related_news, sentiment_score):
    """銘柄ごとに材料の異なる一言コメントを組み立てる。

    ニュースが取得できていれば見出しとセンチメントを先頭材料として最優先で使い、
    無ければ銘柄固有の事業テーマ（MU=メモリ/HBM、SNDK=NAND、NBIS=AIデータセンター等）を
    先頭材料にする。後ろに続くテクニカル材料は固定パターンではなく、その銘柄の数値が
    実際にどれだけ突出しているかで動的に選ぶため、「RSIが○○なので買い候補」のような
    使い回しの定型文になりにくい。
    """
    rsi = ind.get("rsi")
    ma20, ma50 = ind.get("ma20"), ind.get("ma50")
    high_gap = ind.get("high_gap")
    volume_ratio = ind.get("volume_ratio")
    month_return = ind.get("month_return")
    change_pct = ind.get("change_pct")

    news_c = _news_clause(related_news, sentiment_score)
    theme_c = _theme_clause(ticker, sector, industry)
    bottom_c = _bottom_clause(bottom_status, phase, change_pct)
    rsi_c = _rsi_clause(rsi)
    trend_c = _trend_clause(ma20, ma50)
    hg_c = _highgap_clause(high_gap)
    vol_c = _volume_clause(volume_ratio)
    month_c = _month_clause(month_return)
    day_c = _daychange_clause(change_pct)
    cap_c = _capsize_clause(cap_label)

    ranked = _ranked_support_clauses(judgment, ind, bottom_c, rsi_c, trend_c, hg_c, vol_c, month_c, day_c, cap_c)

    if news_c:
        # ニュースがある場合は必ず先頭材料（コメントの中心）とし、判定理由のテクニカル材料を後ろに続ける。
        parts = [news_c] + ranked[:2]
    elif theme_c:
        # ニュースが無い場合は銘柄固有の事業テーマを先頭材料にする。
        parts = [theme_c] + ranked[:2]
    else:
        parts = ranked[:3]

    parts = [p for p in dict.fromkeys(parts) if p][:3]  # 順序を保ったまま重複除去
    if not parts:
        parts = [c for c in [rsi_c, trend_c] if c] or ["データが限定的"]

    body = "、".join(parts)
    return f"{body}。{_CONCLUSION_TEXT.get(judgment, judgment)}。"


def build_result(ticker, dates, closes, volumes, news_items=None,
                  market_cap=None, sector="", industry="", company_name=""):
    """指標計算〜最終判定までをまとめて実行し、Redisに保存する形のレコードを返す。"""
    ind = compute_indicators(dates, closes, volumes)
    bottom_status = compute_bottom_status(ind)
    phase = compute_phase(ind, bottom_status)
    upside = compute_upside_score(ind, bottom_status, phase)
    overheat = compute_overheat_score(ind, phase)

    related_news = [n for n in (news_items or []) if ticker in n.get("tickers", [])]
    related_news.sort(key=lambda n: n.get("published", ""), reverse=True)
    sentiment_score = aggregate_sentiment(ticker, related_news)

    cap_label = classify_market_cap(market_cap)
    upside, overheat = adjust_scores_for_context(upside, overheat, sentiment_score, cap_label)

    judgment = compute_final_judgment(bottom_status, phase, upside, overheat)
    comment = build_comment(ticker, judgment, ind, bottom_status, phase, cap_label, sector, industry,
                             related_news, sentiment_score)

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
        "market_cap": market_cap,
        "market_cap_label": cap_label,
        "company_name": company_name,
        "sector": sector,
        "industry": industry,
        "news_sentiment_score": sentiment_score,
        "judgment": judgment,
        "comment": comment,
        "news": related_news[:3],
    }
