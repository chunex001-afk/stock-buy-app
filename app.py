import os
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
MAX_TICKERS = 15
DATA_TTL = 20 * 60 * 60  # 20 hours
NEWS_TTL = 12 * 60 * 60
API_URL = "https://www.alphavantage.co/query"

DEFAULT_TICKERS = ["AXT", "NBIS", "AEHR", "MU", "SNDK", "BE", "IONQ", "CRDO"]

cache = {}
news_cache = {"ts": 0, "items": []}
lock = threading.Lock()
last_api_call = 0.0


def api_get(params):
    """Alpha Vantage free-plan friendly request: never burst requests."""
    global last_api_call
    with lock:
        wait = 1.05 - (time.time() - last_api_call)
        if wait > 0:
            time.sleep(wait)
        params = dict(params)
        params["apikey"] = API_KEY
        r = requests.get(API_URL, params=params, timeout=20)
        last_api_call = time.time()
    r.raise_for_status()
    return r.json()


def clean_error(data):
    if not isinstance(data, dict):
        return "取得エラー"
    for key in ("Error Message", "Note", "Information"):
        if data.get(key):
            return str(data[key])
    return None


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


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def fetch_price(ticker, force=False):
    now = time.time()
    old = cache.get(ticker)
    if old and not force and now - old["ts"] < DATA_TTL:
        return old["data"]

    if not API_KEY:
        return {"ticker": ticker, "error": "ALPHAVANTAGE_API_KEYが未設定です"}

    try:
        data = api_get({
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker,
            "outputsize": "compact",
        })
        err = clean_error(data)
        if err:
            return {"ticker": ticker, "error": err}

        series = data.get("Time Series (Daily)")
        if not series:
            return {"ticker": ticker, "error": "株価データを取得できませんでした"}

        rows = sorted(
            [(d, float(v["4. close"]), int(float(v.get("5. volume", 0))))
             for d, v in series.items()],
            key=lambda x: x[0]
        )
        dates = [x[0] for x in rows]
        closes = [x[1] for x in rows]
        volumes = [x[2] for x in rows]

        if len(closes) < 20:
            return {"ticker": ticker, "error": "データ不足"}

        latest = closes[-1]
        prev = closes[-2]
        one_month = closes[-22] if len(closes) >= 22 else closes[0]
        one_month_return = (latest / one_month - 1) * 100

        ma20 = sma(closes, 20)
        ma50 = sma(closes, 50)
        rsi = rsi14(closes)
        # Price position: recent high is only a secondary context signal.
        # The primary recovery signal is how far price has risen from the
        # recent bottom, not how far it remains below an old high.
        recent_high = max(closes[-63:]) if len(closes) >= 63 else max(closes)
        high_gap = (latest / recent_high - 1) * 100
        recent_low_20 = min(closes[-20:])
        rebound_from_low = (latest / recent_low_20 - 1) * 100 if recent_low_20 else 0

        # Check whether the recent pullback low is starting to rise.
        higher_low = False
        if len(closes) >= 15:
            prior_low = min(closes[-15:-5])
            latest_low = min(closes[-5:])
            higher_low = latest_low > prior_low

        avg_vol20 = sma(volumes, 20)
        vol_ratio = (volumes[-1] / avg_vol20) if avg_vol20 else 1

        # Neutral "buy now" score based only on observable market data.
        # No user preferences or hard-coded stock ranking are included.
        score = 50.0

        # Momentum / trend
        score += max(-12, min(12, one_month_return * 0.35))
        if ma20:
            score += 5 if latest > ma20 else -5
        if ma50:
            score += 5 if latest > ma50 else -5
        if ma20 and ma50:
            score += 5 if ma20 > ma50 else -4

        # RSI / overheating: strength and overheating are evaluated separately.
        # A strong uptrend should NOT be pushed down simply because RSI is high.
        # If trend + volume confirm strength, high RSI is treated as "strong but
        # slightly overheated; buyable on a pullback" rather than a sell signal.
        strong_trend = bool(
            ma20 and ma50 and latest > ma20 > ma50
            and one_month_return >= 8
        )
        volume_confirmed = vol_ratio >= 1.25
        pullback_zone = high_gap <= -3

        if rsi is not None:
            if 45 <= rsi <= 65:
                score += 6
            elif 65 < rsi <= 72:
                score += 3 if strong_trend else 2
            elif rsi > 72:
                # Do not over-penalize a confirmed strong trend.
                score -= 1 if strong_trend else 5
            elif 30 <= rsi < 45:
                score += 2
            elif rsi < 30:
                score += 5

        # PRIMARY recovery / rebound signal.
        # Do NOT award points merely because the stock is far below its old high.
        # Reward actual recovery from the recent low and improving lows.
        if rebound_from_low >= 15:
            score += 6
        elif rebound_from_low >= 8:
            score += 4
        elif rebound_from_low >= 3:
            score += 2
        elif rebound_from_low < 0:
            score -= 3

        if higher_low:
            score += 3

        # Old-high distance is only a secondary price-position / overheating signal.
        # Being far below the old high is NOT itself a buy signal.
        if high_gap > -3:
            score -= 1 if strong_trend else 3
        elif high_gap <= -3 and high_gap >= -12 and strong_trend:
            score += 2  # healthy pullback inside an established uptrend

        # Volume confirmation
        if vol_ratio >= 1.5:
            score += 3
        elif volume_confirmed and strong_trend:
            score += 1

        # "Strong but overheated" is a valid high-ranking state. The app should
        # favor a strong trend with a manageable pullback over a weak stock just
        # because the latter has a lower RSI.
        if strong_trend and (rsi is not None and rsi > 65):
            score += 2

        score = max(0, min(100, round(score)))
        if score >= 88:
            judgment = "今買う候補"
        elif score >= 80:
            judgment = "買い場候補"
        elif score >= 70:
            judgment = "監視"
        elif score >= 60:
            judgment = "良い会社でも今は待つ"
        else:
            judgment = "今は見送り"

        result = {
            "ticker": ticker,
            "date": dates[-1],
            "price": round(latest, 2),
            "change_pct": round((latest / prev - 1) * 100, 2),
            "month_return": round(one_month_return, 2),
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma50": round(ma50, 2) if ma50 else None,
            "high_gap": round(high_gap, 2),
            "recent_low_20": round(recent_low_20, 2),
            "rebound_from_low": round(rebound_from_low, 2),
            "higher_low": higher_low,
            "volume_ratio": round(vol_ratio, 2),
            "score": score,
            "judgment": judgment,
            "error": None,
            "source_note": "日足の実データから算出。企業業績・時価総額はこのAPI呼び出し回数制限のため自動取得対象外。",
        }
        cache[ticker] = {"ts": now, "data": result}
        return result

    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def fetch_news(tickers, force=False):
    global news_cache
    now = time.time()
    if news_cache["items"] and not force and now - news_cache["ts"] < NEWS_TTL:
        return news_cache["items"]

    if not API_KEY:
        return []

    try:
        # One news request for the whole candidate set.
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ",".join(tickers),
            "sort": "LATEST",
            "limit": 50,
        }
        data = api_get(params)
        err = clean_error(data)
        if err:
            return []

        items = []
        for x in data.get("feed", []):
            published = x.get("time_published", "")
            # Only surface very recent news (roughly previous market day / last 36h).
            try:
                dt = datetime.strptime(published[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - dt
                if age > timedelta(hours=36):
                    continue
            except Exception:
                pass

            matched = []
            for ts in x.get("ticker_sentiment", []):
                t = ts.get("ticker")
                if t in tickers:
                    matched.append(t)
            if not matched:
                continue

            items.append({
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "source": x.get("source", ""),
                "published": published,
                "tickers": matched,
            })

        news_cache = {"ts": now, "items": items[:30]}
        return news_cache["items"]
    except Exception:
        return []


def top_rank_reason(rows):
    """Explain why the #1 stock ranks above the next few names.
    This is deliberately comparative and uses only data the app actually has.
    """
    valid = [r for r in rows if not r.get("error")]
    if not valid:
        return ""
    top = valid[0]
    others = valid[1:4]

    ticker = top.get("ticker", "")
    score = top.get("score", 0)
    rebound = top.get("rebound_from_low", 0) or 0
    month = top.get("month_return", 0) or 0
    rsi = top.get("rsi14")
    higher_low = top.get("higher_low", False)
    ma20 = top.get("ma20")
    ma50 = top.get("ma50")
    price = top.get("price")
    vol = top.get("volume_ratio", 1) or 1

    trend = bool(price and ma20 and ma50 and price > ma20 > ma50)
    strong = trend and month >= 8
    pullback = (top.get("high_gap", 0) or 0) <= -3

    # Compare the top score and the three nearest competitors.
    if others:
        best_other = others[0]
        gap = score - (best_other.get("score", 0) or 0)
        competitor_text = f"{best_other.get('ticker','')}より" if gap > 0 else "上位銘柄の中でも"
    else:
        competitor_text = "登録銘柄の中で"

    if strong and rebound >= 8 and higher_low:
        line1 = f"業績などを推測せず、株価データだけで見ると、{ticker}は直近安値から+{rebound:.1f}%戻し、安値も切り上がる強い上昇基調。"
    elif strong and rebound >= 8:
        line1 = f"{ticker}は1か月+{month:.1f}%と上昇モメンタムが強く、直近安値からも+{rebound:.1f}%回復。現在の上昇力を高く評価。"
    elif rebound >= 12 and higher_low:
        line1 = f"{ticker}は直近安値から+{rebound:.1f}%上昇し、安値も切り上がっているため、調整後の回復力を高く評価。"
    elif pullback and trend:
        line1 = f"{ticker}は上昇トレンドを維持しながら直近高値から一服しており、強さを保った押し目として評価。"
    else:
        line1 = f"{ticker}は現在の株価モメンタム、移動平均、直近安値からの回復を総合してスコア{score}点で首位。"

    if rsi is not None and rsi > 65 and strong:
        line2 = f"RSIは{rsi:.1f}でやや過熱感はあるものの、強いトレンドを確認できるため過度に減点せず、「強いが押し目で買いやすい」と判断。"
    elif rsi is not None and 45 <= rsi <= 65 and rebound >= 8:
        line2 = f"RSIは{rsi:.1f}で極端な過熱ではなく、直近の反発と合わせて今から入る際のバランスを評価。"
    elif vol >= 1.25:
        line2 = f"直近出来高は20日平均の{vol:.2f}倍で、上昇に参加する売買の強さも確認できる。"
    else:
        line2 = "最高値から何％下かではなく、直近安値からの回復と現在の上昇継続性を優先して評価。"

    names = [o.get("ticker") for o in others if o.get("ticker")]
    if names:
        compare = "・".join(names)
        if gap > 0:
            line3 = f"{competitor_text}スコアで{gap}点上回り、{compare}と比べても「現在の上昇力＋株価位置」の組み合わせが最も優位と判断。"
        else:
            line3 = f"{compare}との差は小さいため、1位の決め手は直近の反発・トレンド・過熱感のバランス。数値が変われば順位も入れ替わり得る。"
    else:
        line3 = "登録銘柄の中で現在の株価データの組み合わせが最も強く、1位と判断。"

    return {
        "title": f"🥇 {ticker}が1位の理由（{score}点）",
        "lines": [line1, line2, line3],
    }


def reason_for(ticker, data, news):
    if data.get("error"):
        return data["error"]
    related = [n for n in news if ticker in n.get("tickers", [])]
    if related:
        return f"ニュース: {related[0]['title']}"
    rsi = data.get("rsi14")
    month = data.get("month_return", 0) or 0
    high_gap = data.get("high_gap", 0) or 0
    rebound = data.get("rebound_from_low", 0) or 0
    higher_low = data.get("higher_low", False)
    ma20 = data.get("ma20")
    ma50 = data.get("ma50")
    price = data.get("price")
    strong_trend = bool(ma20 and ma50 and price and price > ma20 > ma50 and month >= 8)
    if strong_trend and rsi is not None and rsi > 65:
        if rebound >= 8 or higher_low:
            return "強い上昇トレンドを維持。直近安値からの反発も確認でき、やや過熱でも押し目では買いやすい"
        return "強い上昇トレンドを維持。短期的な過熱には注意"
    if rebound >= 12 and higher_low:
        return "直近安値からの反発が強く、安値も切り上がり始めている。上昇継続を評価"
    if rebound >= 5:
        return "直近安値から反発中。最高値からの下落率より、現在の上昇モメンタムを重視"
    if month >= 15:
        return "1か月上昇率が高く、上昇モメンタムが強い"
    if rsi is not None and rsi > 72:
        return "RSIが高く短期的な過熱感あり。ただしトレンドの強さも確認"
    if rsi is not None and rsi < 35:
        return "RSIが低く、反発余地を確認したい局面"
    return "株価トレンド・RSI・移動平均から判定"


HTML = r"""
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111827">
<title>今買うべき銘柄ランキング</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#f4f6f8;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
.wrap{max-width:1200px;margin:auto;padding:18px}.title{font-size:28px;font-weight:800;margin-bottom:5px}.sub{color:#68748a;margin-bottom:18px}
.card{background:white;border-radius:22px;padding:18px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.controls{display:flex;gap:8px;flex-wrap:wrap}.controls input{flex:1;min-width:150px;padding:13px;border:1px solid #ccd2db;border-radius:13px;font-size:16px}
button{border:0;border-radius:13px;padding:12px 16px;font-weight:800;font-size:15px;cursor:pointer;background:#172033;color:white}
button.secondary{background:#eef1f5;color:#172033}.small{font-size:12px;color:#758096;margin-top:10px}
table{width:100%;border-collapse:collapse} th,td{padding:13px 8px;border-bottom:1px solid #e7eaf0;text-align:left;vertical-align:top} th{font-size:13px;color:#667085} td{font-size:14px}
.rank{font-weight:900;font-size:18px}.score{font-size:21px;font-weight:900}.pill{display:inline-block;padding:6px 10px;border-radius:999px;background:#edf3ff;font-weight:800}.err{background:#fff0e8;color:#a84b18}.up{color:#087443;font-weight:800}.down{color:#b42318;font-weight:800}.reason{max-width:430px;line-height:1.45}
@media(max-width:760px){.wrap{padding:12px}.title{font-size:23px}table{min-width:760px}.tablebox{overflow-x:auto}.card{border-radius:18px;padding:14px}.controls button{width:auto}.hide-mobile{display:none}}
</style>
</head>
<body>
<div class="wrap">
<div class="title">🏆 今買うべき銘柄ランキング</div>
<div class="sub">実データ版｜最大15銘柄｜開くと更新｜前日の重要ニュースがある場合だけ表示</div>

<div class="card">
  <div class="controls">
    <button onclick="updateRanking(true)">🔄 最新データに更新</button>
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()">＋追加</button>
  </div>
  <div id="status" class="small">読み込み中…</div>
</div>

<div class="card">
<div class="tablebox">
<table>
<thead><tr><th>順位</th><th>銘柄</th><th>スコア</th><th>判定</th><th>前日比</th><th>1か月</th><th>RSI14</th><th>主な理由</th><th></th></tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
</div>

<div class="card" id="topReasonCard" style="display:none">
  <b id="topReasonTitle"></b>
  <div id="topReasonBody" style="line-height:1.7;margin-top:8px"></div>
</div>

<div class="card">
<b>🎯 1か月で最も起こりやすい上昇幅</b>
<div id="range" style="font-size:22px;font-weight:900;margin-top:8px">—</div>
<div class="small">直近の日足データから候補銘柄の1か月リターンを参考に表示。将来の確率を保証するものではありません。</div>
</div>

<div class="card">
<b>🧠 判定ロジック</b>
<div class="small" style="line-height:1.7">
株価モメンタム、MA20/MA50、RSI14、直近安値からの反発率、安値の切り上がり、出来高を中心に0〜100で算出。<b>最高値から何％下落しているかは主判定にしません。</b>直近安値からの上昇と上昇継続性を優先し、強い上昇トレンド中の高RSIは過度に減点せず、「強いがやや過熱・押し目で買いやすい」と評価します。<br>
企業業績・時価総額を小さいだけで加点するような処理はしていません。ユーザー個人の保有銘柄や考え方もスコアには入れていません。
</div>
</div>
</div>

<script>
const DEFAULTS={{ defaults|tojson }};
let tickers=JSON.parse(localStorage.getItem("buy_app_tickers")||"null")||DEFAULTS;
let previous=JSON.parse(localStorage.getItem("buy_app_prev_rank")||"{}");

function save(){localStorage.setItem("buy_app_tickers",JSON.stringify(tickers));}
function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]));}

async function updateRanking(force=false){
  if(tickers.length===0){document.getElementById("status").textContent="銘柄を追加してください";return}
  document.getElementById("status").textContent="データ取得中…（初回は15銘柄で少し時間がかかります）";
  try{
    const r=await fetch("/api/ranking?tickers="+encodeURIComponent(tickers.join(","))+(force?"&force=1":""));
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"取得失敗");
    render(j.rows,j.news,j.top_reason);
    document.getElementById("status").textContent="最終更新："+j.updated_at+"｜無料API対策：取得データを20時間キャッシュ";
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
}
function addTicker(){
  const el=document.getElementById("ticker"), t=el.value.trim().toUpperCase().replace(/[^A-Z0-9.\-]/g,"");
  if(!t)return;
  if(tickers.includes(t)){el.value="";return}
  if(tickers.length>=15){alert("登録できる銘柄は最大15銘柄です");return}
  tickers.push(t);save();el.value="";updateRanking(false);
}
function delTicker(t){
  tickers=tickers.filter(x=>x!==t);save();updateRanking(false);
}
function render(rows,news,topReason){
  const rank={}; rows.forEach((x,i)=>rank[x.ticker]=i+1);
  const tb=document.getElementById("tbody");
  tb.innerHTML=rows.map((x,i)=>{
    const old=previous[x.ticker];
    let move="NEW", cls="";
    if(old){const d=old-(i+1); if(d>0){move="↑"+d;cls="up"} else if(d<0){move="↓"+Math.abs(d);cls="down"} else move="→"}
    const err=x.error;
    const reason=esc(x.reason||"");
    return `<tr>
      <td class="rank">${i+1}</td>
      <td><b>${esc(x.ticker)}</b><div class="small">${esc(x.date||"")}</div></td>
      <td class="score">${err?"—":x.score}</td>
      <td><span class="pill ${err?"err":""}">${esc(err?"取得エラー":x.judgment)}</span></td>
      <td class="${x.change_pct>0?'up':x.change_pct<0?'down':''}">${err?"—":(x.change_pct>0?"+":"")+x.change_pct+"%"}</td>
      <td>${err?"—":(x.month_return>0?"+":"")+x.month_return+"%"}</td>
      <td>${err?"—":(x.rsi14??"—")}</td>
      <td class="reason">${reason}</td>
      <td><button class="secondary" onclick="delTicker('${esc(x.ticker)}')">削除</button></td>
    </tr>`
  }).join("");
  previous=rank;localStorage.setItem("buy_app_prev_rank",JSON.stringify(previous));
  const topCard=document.getElementById("topReasonCard");
  if(topReason && topReason.lines){
    document.getElementById("topReasonTitle").textContent=topReason.title;
    document.getElementById("topReasonBody").innerHTML=topReason.lines.map(x=>`<div>${esc(x)}</div>`).join("");
    topCard.style.display="block";
  } else {
    topCard.style.display="none";
  }
  const good=rows.filter(x=>!x.error).map(x=>x.month_return).sort((a,b)=>b-a);
  document.getElementById("range").textContent=good.length?((good[Math.min(2,good.length-1)]>=0?"+":"")+good[Math.min(2,good.length-1)]+"%前後"):"—";
}
updateRanking(false);
</script>
</body>
</html>
"""

@app.get("/")
def index():
    return render_template_string(HTML, defaults=DEFAULT_TICKERS)

@app.get("/api/ranking")
def ranking():
    raw = request.args.get("tickers", "")
    tickers = [x.strip().upper() for x in raw.split(",") if x.strip()]
    # Remove duplicates while preserving order and enforce max 15.
    tickers = list(dict.fromkeys(tickers))[:MAX_TICKERS]
    if not tickers:
        tickers = DEFAULT_TICKERS
    force = request.args.get("force") == "1"

    rows = []
    for t in tickers:
        rows.append(fetch_price(t, force=force))

    # One combined news request, cached separately.
    news = fetch_news(tickers, force=force)

    for row in rows:
        row["reason"] = reason_for(row["ticker"], row, news)

    rows.sort(key=lambda x: (x.get("score", -1) if not x.get("error") else -1), reverse=True)
    top_reason = top_rank_reason(rows)

    return jsonify({
        "ok": True,
        "updated_at": datetime.now().astimezone().strftime("%Y/%-m/%-d %H:%M:%S"),
        "rows": rows,
        "news": news,
        "top_reason": top_reason,
        "max_tickers": MAX_TICKERS,
    })

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "stock-buy-app", "max_tickers": MAX_TICKERS})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
