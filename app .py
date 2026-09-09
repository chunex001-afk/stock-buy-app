import os
import time
import threading
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, Response, request, jsonify

app = Flask(__name__)
API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
# Free Alpha Vantage: avoid repeatedly consuming the daily quota.
DATA_CACHE_TTL = 12 * 60 * 60
NEWS_CACHE_TTL = 6 * 60 * 60
cache = {}
api_lock = threading.Lock()
last_api_request = 0.0
DEFAULT_TICKERS = ["AXT", "NBIS", "AEHR", "MU", "SNDK", "BE", "IONQ", "CRDO"]


def av(params):
    if not API_KEY:
        raise RuntimeError("Alpha Vantage APIキーがRenderに設定されていません")
    p = dict(params)
    p["apikey"] = API_KEY
    # Free Alpha Vantage keys require requests to be spaced out.
    # Serialize requests so 8 tickers do not hit the 1-request/second burst limit.
    global last_api_request
    with api_lock:
        wait = 1.15 - (time.time() - last_api_request)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get("https://www.alphavantage.co/query", params=p, timeout=25)
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Alpha Vantage通信エラー: {e}") from e
        except ValueError as e:
            raise RuntimeError("Alpha VantageからJSONデータを受け取れませんでした") from e
        finally:
            last_api_request = time.time()

    # Alpha Vantage can return quota / service messages without HTTP errors.
    if "Error Message" in d:
        raise RuntimeError(f"Alpha Vantageエラー: {d['Error Message']}")
    if "Note" in d:
        raise RuntimeError("Alpha Vantageの利用上限に達しています。しばらく待ってから更新してください")
    if "Information" in d:
        raise RuntimeError(f"Alpha Vantage: {d['Information']}")
    return d


def cached(key, fn, ttl):
    now = time.time()
    item = cache.get(key)
    if item and now - item[0] < ttl:
        return item[1]
    value = fn()
    cache[key] = (now, value)
    return value


def daily(ticker):
    return cached(
        "daily:" + ticker,
        lambda: av({
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker,
            "outputsize": "compact",
        }),
        DATA_CACHE_TTL,
    )


def rows(data):
    series = data.get("Time Series (Daily)") or {}
    result = []
    for day, v in series.items():
        try:
            result.append((day, float(v["4. close"]), float(v["5. volume"])))
        except (KeyError, TypeError, ValueError):
            pass
    return sorted(result, reverse=True)


def sma(c, n):
    return sum(c[:n]) / n if len(c) >= n else None


def rsi14(c):
    # Wilder RSI(14), matching the intended app methodology.
    if len(c) < 15:
        return None
    x = list(reversed(c))
    gains, losses = [], []
    for i in range(1, len(x)):
        ch = x[i] - x[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    ag = sum(gains[:14]) / 14
    al = sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        ag = (ag * 13 + gains[i]) / 14
        al = (al * 13 + losses[i]) / 14
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def technical_score(a):
    c = [x[1] for x in a]
    v = [x[2] for x in a]
    if not c:
        raise RuntimeError("株価データが空です")
    p = c[0]
    m20 = sma(c, 20)
    m50 = sma(c, 50)
    r = rsi14(c)
    ret = (p / c[20] - 1) * 100 if len(c) > 20 else None
    s = 50
    if m20 is not None:
        s += 10 if p > m20 else -8
    if m50 is not None:
        s += 8 if p > m50 else -8
    if m20 is not None and m50 is not None:
        s += 7 if m20 > m50 else -5
    if ret is not None:
        s += max(-10, min(10, ret * 0.5))
    if r is not None:
        if 45 <= r <= 68:
            s += 5
        elif r > 75:
            s -= 5
        elif r < 30:
            s += 2
    vr = None
    if len(v) >= 20:
        v5 = sum(v[:5]) / 5
        v20 = sum(v[:20]) / 20
        vr = v5 / v20 if v20 else None
        if vr and vr > 1.2:
            s += 3
    return max(0, min(100, round(s))), {
        "rsi": r,
        "ma20": m20,
        "ma50": m50,
        "ret20": ret,
        "volume_ratio": vr,
        "price": p,
    }


def get_news(tickers):
    # News is deliberately cached so a page refresh does not consume another API call.
    try:
        data = cached(
            "news:" + ",".join(sorted(tickers)),
            lambda: av({
                "function": "NEWS_SENTIMENT",
                "tickers": ",".join(tickers),
                "sort": "LATEST",
                "limit": 100,
            }),
            NEWS_CACHE_TTL,
        )
    except Exception:
        return {t: [] for t in tickers}

    # Previous US market day is approximated as yesterday in UTC for now.
    target = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    out = {t: [] for t in tickers}
    for a in data.get("feed", []):
        try:
            day = datetime.strptime(a.get("time_published", "")[:8], "%Y%m%d").date()
        except Exception:
            continue
        if day != target:
            continue
        for q in a.get("ticker_sentiment", []):
            t = q.get("ticker")
            if t in out and len(out[t]) < 2:
                out[t].append({"title": a.get("title", ""), "url": a.get("url", "")})
    return out


def make_ranking(tickers):
    news = get_news(tickers)
    result = []
    for t in tickers:
        try:
            data = daily(t)
            rr = rows(data)
            score, metrics = technical_score(rr)
            judge = (
                "今買いたい" if score >= 85 else
                "買い候補" if score >= 78 else
                "買いたくなるが、まだ" if score >= 70 else
                "今は買わない"
            )
            result.append({
                "ticker": t,
                "score": score,
                "judge": judge,
                "metrics": metrics,
                "news": news.get(t, []),
                "reason": (
                    "前日の重要ニュースを確認。"
                    if news.get(t) else
                    "前日の重要ニュースなし。価格・テクニカルを中心に更新。"
                ),
            })
        except Exception as e:
            result.append({
                "ticker": t,
                "score": None,
                "judge": "取得エラー",
                "metrics": {},
                "news": news.get(t, []),
                "reason": str(e),
            })
    return sorted(
        result,
        key=lambda x: x["score"] if x["score"] is not None else -1,
        reverse=True,
    )


HTML = r'''
<!doctype html><html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#17202a"><title>今買うべき銘柄</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
.w{max-width:1100px;margin:auto;padding:12px}.p{background:#fff;border-radius:15px;padding:14px;margin:10px 0;box-shadow:0 2px 10px #0001}
h1{font-size:22px;margin:2px 0 5px}.small{font-size:12px;color:#667085;line-height:1.55}
button,input{padding:10px;border-radius:9px;border:1px solid #ccd2d8;font-weight:700}
button{background:#17202a;color:white;border:0;cursor:pointer}input{text-transform:uppercase;width:125px}
.tab{overflow-x:auto}table{width:100%;min-width:930px;border-collapse:collapse;font-size:13px}
th,td{padding:9px 7px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{background:#f7f8fa}
.sc{font-size:17px;font-weight:800}.tag{padding:5px 8px;border-radius:99px;font-size:11px;font-weight:800;white-space:nowrap}
.g{background:#e7f6ed;color:#087443}.y{background:#fff5cc;color:#8a6500}.o{background:#ffeadf;color:#b54708}.news{font-weight:700}.news a{color:#175cd3;text-decoration:none}
.err{color:#b42318;font-weight:700}.ok{color:#087443;font-weight:700}
</style></head><body><div class="w">
<h1>🏆 今買うべき銘柄ランキング</h1>
<div class="small">実データ版｜開くと更新｜前日の重要ニュースがある場合だけ表示</div>
<div class="p"><button onclick="load(true)">🔄 最新データに更新</button>
<input id="t" placeholder="例 NVDA"><button onclick="add()">➕ 追加</button>
<div id="st" class="small" style="margin-top:8px"></div></div>
<div class="p"><div class="tab"><table><thead><tr>
<th>順位</th><th>銘柄</th><th>スコア</th><th>判定</th><th>昨日の重要ニュース</th><th>主な理由</th><th>テクニカル</th><th>削除</th>
</tr></thead><tbody id="r"></tbody></table></div></div>
<div class="p small"><b>判定：</b>🟢 今買いたい　🟡 買い候補　🟠 買いたくなるが、まだ　🔴 今は買わない<br>
<b>成長余地：</b>時価総額が小さいだけでは加点しない。企業規模に対して業績成長・ガイダンス・需要が強い場合だけ評価。<br>
<b>ニュース：</b>前日の重要ニュースがある場合だけ表示。なければ無理に入れない。</div></div>
<script>
let ts=JSON.parse(localStorage.getItem("buyTickers")||'["AXT","NBIS","AEHR","MU","SNDK","BE","IONQ","CRDO"]');
function save(){localStorage.setItem("buyTickers",JSON.stringify(ts))}
function add(){let t=document.getElementById("t").value.trim().toUpperCase();if(!t)return;if(ts.includes(t))return alert("すでに登録されています");if(ts.length>=20)return alert("最大20銘柄です");ts.push(t);save();document.getElementById("t").value="";load(true)}
function del(t){if(confirm(t+"を削除しますか？")){ts=ts.filter(x=>x!==t);save();load(true)}}
function esc(s){return String(s??"").replace(/[&<>\"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\':'&#92;','"':'&quot;'}[m]))}
async function load(force=false){document.getElementById("st").textContent=force?"最新データを確認中…":"データを読み込み中…";try{
let z=await fetch("/api/ranking?tickers="+encodeURIComponent(ts.join(","))),j=await z.json();
if(j.error) throw new Error(j.error);
document.getElementById("r").innerHTML=j.items.map((x,i)=>{
let c=x.score>=85?"g":x.score>=78?"y":"o";
let n=x.news&&x.news.length?x.news.map(a=>`📰 <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>`).join("<br>"):"<span class=small>前日の重要ニュースなし</span>";
let m=x.metrics||{},tech=`RSI14 ${m.rsi==null?"-":m.rsi.toFixed(1)} / MA20 ${m.ma20==null?"-":m.ma20.toFixed(2)} / MA50 ${m.ma50==null?"-":m.ma50.toFixed(2)} / 1か月 ${m.ret20==null?"-":m.ret20.toFixed(1)+"%"}`;
let reason=x.judge==="取得エラー"?`<span class="err">${esc(x.reason)}</span>`:esc(x.reason);
return `<tr><td>${i<3?["🥇","🥈","🥉"][i]:i+1}</td><td><b>${esc(x.ticker)}</b></td><td class=sc>${x.score??"-"}</td><td><span class="tag ${c}">${esc(x.judge)}</span></td><td>${n}</td><td>${reason}</td><td class=small>${tech}</td><td><button onclick="del('${esc(x.ticker)}')">削除</button></td></tr>`}).join("");
document.getElementById("st").textContent="最終更新："+new Date().toLocaleString("ja-JP")+"（データはサーバー側で最大12時間キャッシュ）"
}catch(e){document.getElementById("st").textContent="更新失敗："+e.message}}
load();
</script></body></html>
'''


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/api/ranking")
def api_ranking():
    ts = [x.strip().upper() for x in request.args.get("tickers", "").split(",") if x.strip()][:20]
    items = make_ranking(ts or DEFAULT_TICKERS)
    return jsonify({"items": items, "updated": datetime.now(timezone.utc).isoformat()})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "api_key_configured": bool(API_KEY), "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
