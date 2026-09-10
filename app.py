import os
import re
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string

import redis_store as store
import stock_logic as logic
import refresh

app = Flask(__name__)

API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()

# 手動更新のクールダウン。1日の自己申告予算(stock_logic.DAILY_API_BUDGET)に
# 対して余裕が小さいため、自動更新より長めの間隔を空ける。
MANUAL_REFRESH_COOLDOWN = 3 * 60 * 60  # 3時間

TICKER_RE = re.compile(r"[^A-Z0-9.\-]")


def _sanitize_ticker(raw):
    return TICKER_RE.sub("", (raw or "").strip().upper())


def _freshness(record):
    """画面表示用の鮮度情報を組み立てる。"""
    if not record or not record.get("last_trade_date"):
        return {
            "status": "none",
            "label": "⚪ データなし",
            "last_trade_date": None,
            "fetched_at": None,
            "last_error_label": None,
        }

    is_stale = bool(record.get("is_stale"))
    if not is_stale:
        return {
            "status": "fresh",
            "label": "🟢 最新",
            "last_trade_date": record.get("last_trade_date"),
            "fetched_at": record.get("fetched_at"),
            "last_error_label": None,
        }

    err_type = record.get("last_error")
    return {
        "status": "stale",
        "label": "🟡 前回データ",
        "last_trade_date": record.get("last_trade_date"),
        "fetched_at": record.get("fetched_at"),
        "last_error_label": logic.ERROR_LABELS.get(err_type, err_type or "不明なエラー"),
    }


def _build_row(ticker):
    record = store.get_ticker_record(ticker)
    freshness = _freshness(record)

    if not record or not record.get("last_trade_date"):
        return {
            "ticker": ticker,
            "price": None, "change_pct": None, "month_return": None,
            "rsi14": None, "ma20": None, "ma50": None,
            "high_gap": None, "volume_ratio": None,
            "upside_score": None, "overheat_score": None,
            "bottom_status": None, "phase": None,
            "judgment": "判定不可",
            "comment": "まだ一度もデータを取得できていません。次回の自動更新をお待ちください。",
            "freshness": freshness,
        }

    return {
        "ticker": ticker,
        "price": record.get("price"),
        "change_pct": record.get("change_pct"),
        "month_return": record.get("month_return"),
        "rsi14": record.get("rsi14"),
        "ma20": record.get("ma20"),
        "ma50": record.get("ma50"),
        "high_gap": record.get("high_gap"),
        "volume_ratio": record.get("volume_ratio"),
        "upside_score": record.get("upside_score"),
        "overheat_score": record.get("overheat_score"),
        "bottom_status": record.get("bottom_status"),
        "phase": record.get("phase"),
        "judgment": record.get("judgment", "判定不可"),
        "comment": record.get("comment", ""),
        "freshness": freshness,
    }


def _get_watchlist():
    tickers = store.get_watchlist()
    if not tickers:
        return list(logic.DEFAULT_TICKERS)
    return [t for t in tickers if t][: logic.MAX_TICKERS]


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
.wrap{max-width:1300px;margin:auto;padding:18px}.title{font-size:28px;font-weight:800;margin-bottom:5px}.sub{color:#68748a;margin-bottom:18px}
.card{background:white;border-radius:22px;padding:18px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.controls{display:flex;gap:8px;flex-wrap:wrap}.controls input{flex:1;min-width:150px;padding:13px;border:1px solid #ccd2db;border-radius:13px;font-size:16px}
button{border:0;border-radius:13px;padding:12px 16px;font-weight:800;font-size:15px;cursor:pointer;background:#172033;color:white}
button:disabled{opacity:.5;cursor:not-allowed}
button.secondary{background:#eef1f5;color:#172033}.small{font-size:12px;color:#758096;margin-top:10px;line-height:1.6}
table{width:100%;border-collapse:collapse} th,td{padding:11px 7px;border-bottom:1px solid #e7eaf0;text-align:left;vertical-align:top} th{font-size:12px;color:#667085} td{font-size:13px}
.rank{font-weight:900;font-size:18px}.score{font-size:18px;font-weight:900}
.pill{display:inline-block;padding:6px 10px;border-radius:999px;background:#edf3ff;font-weight:800;font-size:12px;white-space:nowrap}
.j-strong_buy{background:#087443;color:#fff}.j-buy{background:#e7f6ed;color:#087443}.j-early{background:#e6f0ff;color:#1d4ed8}
.j-wait_bottom{background:#eef1f5;color:#475467}.j-overheat{background:#fff5cc;color:#8a6500}.j-top{background:#ffeadf;color:#b54708}.j-unknown{background:#f2f2f2;color:#98a2b3;font-style:italic}
.up{color:#087443;font-weight:800}.down{color:#b42318;font-weight:800}.reason{max-width:340px;line-height:1.5}
.fresh{color:#087443;font-weight:700}.stalebadge{color:#8a6500;font-weight:700}.nonebadge{color:#98a2b3;font-weight:700}
.freshbox{font-size:11px;line-height:1.6;white-space:nowrap}
.tablebox{overflow-x:auto}
@media(max-width:760px){.wrap{padding:12px}.title{font-size:23px}table{min-width:1180px}.card{border-radius:18px;padding:14px}}
</style>
</head>
<body>
<div class="wrap">
<div class="title">🏆 今買うべき銘柄ランキング</div>
<div class="sub">実データ版｜最大15銘柄｜1日1回サーバー側で自動更新（GitHub Actions）</div>

<div class="card">
  <div class="controls">
    <button onclick="manualRefresh()" id="refreshBtn">🔄 未取得/失敗分だけ今すぐ再取得</button>
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()">＋追加</button>
  </div>
  <div id="status" class="small">読み込み中…</div>
</div>

<div class="card">
<div class="tablebox">
<table>
<thead><tr>
<th>順位</th><th>銘柄</th><th>株価</th><th>前日比</th><th>1ヶ月</th><th>RSI14</th><th>MA20</th><th>MA50</th>
<th>高値乖離</th><th>出来高比</th><th>上昇余地</th><th>過熱リスク</th><th>底打ち状態</th><th>局面</th>
<th>最終判定</th><th>一言コメント</th><th>データ鮮度</th><th></th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
</div>

<div class="card">
<b>🧠 判定ロジック（概要）</b>
<div class="small">
底打ち状態（未確認／途中／確認）・局面（反発局面／天井局面）・上昇余地スコア・過熱リスクスコアの4軸を、優先順位付きの決定表で統合して最終判定します。<br>
天井局面や過熱リスクが非常に高い場合は、上昇余地スコアが高くても「見送り」側を優先します。単純に値上がり中の銘柄を高評価する設計ではありません。<br>
株価データは1日1回、GitHub Actionsによる自動ジョブがAlpha Vantageから取得しUpstash Redisに保存します。このページはRedisを読むだけで新規API呼び出しは行いません（右上のボタンのみ、未取得・失敗銘柄に限定して例外的に再取得します）。
</div>
</div>
</div>

<script>
const JBADGE = {
  "強い買い候補":"j-strong_buy","買い候補":"j-buy","先回り候補":"j-early",
  "底打ち待ち":"j-wait_bottom","過熱のため待つ":"j-overheat","天井圏のため見送り":"j-top","判定不可":"j-unknown"
};
let previous = JSON.parse(localStorage.getItem("buy_app_prev_rank")||"{}");

function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]));}
function fmt(v,suf){return (v===null||v===undefined)?"—":((v>0&&suf==="%")?"+":"")+v+(suf||"");}
function fmtDt(iso){
  if(!iso) return "—";
  try{const d=new Date(iso);return d.toLocaleString("ja-JP",{year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});}catch(e){return iso;}
}
function fmtDate(s){
  if(!s) return "—";
  return s.replaceAll("-","/");
}

async function updateRanking(){
  document.getElementById("status").textContent="読み込み中…";
  try{
    const r=await fetch("/api/ranking");
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"取得失敗");
    render(j.rows);
    const lr=j.last_refresh;
    const budget=j.budget||{};
    let statusMsg = lr
      ? `自動更新: ${lr.success_count}件成功／${lr.failed_count}件失敗（${fmtDt(lr.run_at)}実行）`
      : "自動更新はまだ実行されていません";
    statusMsg += ` ｜ 本日のAPI使用: ${budget.used??"—"}/${budget.limit??"—"}`;
    document.getElementById("status").textContent = statusMsg;
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
}

async function manualRefresh(){
  const btn=document.getElementById("refreshBtn");
  btn.disabled=true;
  document.getElementById("status").textContent="未取得・失敗銘柄を再取得中…";
  try{
    const r=await fetch("/api/refresh",{method:"POST"});
    const j=await r.json();
    if(!j.ok){document.getElementById("status").textContent="更新できません："+j.error;}
    else{document.getElementById("status").textContent=j.message||"更新しました";}
    await updateRanking();
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
  finally{btn.disabled=false;}
}

async function addTicker(){
  const el=document.getElementById("ticker"), t=el.value.trim().toUpperCase().replace(/[^A-Z0-9.\-]/g,"");
  if(!t)return;
  try{
    const r=await fetch("/api/watchlist",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticker:t})});
    const j=await r.json();
    if(!j.ok){alert(j.error||"追加できませんでした");return;}
    el.value="";
    await updateRanking();
  }catch(e){alert("追加エラー: "+e.message)}
}

async function delTicker(t){
  try{
    const r=await fetch("/api/watchlist/"+encodeURIComponent(t),{method:"DELETE"});
    const j=await r.json();
    if(!j.ok){alert(j.error||"削除できませんでした");return;}
    await updateRanking();
  }catch(e){alert("削除エラー: "+e.message)}
}

function render(rows){
  const rank={}; rows.forEach((x,i)=>rank[x.ticker]=i+1);
  const tb=document.getElementById("tbody");
  tb.innerHTML=rows.map((x,i)=>{
    const f=x.freshness||{};
    let freshHtml;
    if(f.status==="fresh"){
      freshHtml=`<div class="freshbox"><span class="fresh">${esc(f.label)}</span><br>最終取引日：${fmtDate(f.last_trade_date)}<br>取得日時：${fmtDt(f.fetched_at)}</div>`;
    }else if(f.status==="stale"){
      freshHtml=`<div class="freshbox"><span class="stalebadge">${esc(f.label)}</span><br>最終取引日：${fmtDate(f.last_trade_date)}<br>前回取得：${fmtDt(f.fetched_at)}<br>今回更新：${esc(f.last_error_label||"エラー")}</div>`;
    }else{
      freshHtml=`<div class="freshbox"><span class="nonebadge">${esc(f.label)}</span></div>`;
    }
    const cls=JBADGE[x.judgment]||"j-unknown";
    return `<tr>
      <td class="rank">${i+1}</td>
      <td><b>${esc(x.ticker)}</b></td>
      <td class="score">${fmt(x.price)}</td>
      <td class="${x.change_pct>0?'up':x.change_pct<0?'down':''}">${fmt(x.change_pct,"%")}</td>
      <td>${fmt(x.month_return,"%")}</td>
      <td>${fmt(x.rsi14)}</td>
      <td>${fmt(x.ma20)}</td>
      <td>${fmt(x.ma50)}</td>
      <td>${fmt(x.high_gap,"%")}</td>
      <td>${fmt(x.volume_ratio)}</td>
      <td>${fmt(x.upside_score)}</td>
      <td>${fmt(x.overheat_score)}</td>
      <td>${esc(x.bottom_status||"—")}</td>
      <td>${esc(x.phase||"—")}</td>
      <td><span class="pill ${cls}">${esc(x.judgment)}</span></td>
      <td class="reason">${esc(x.comment||"")}</td>
      <td>${freshHtml}</td>
      <td><button class="secondary" onclick="delTicker('${esc(x.ticker)}')">削除</button></td>
    </tr>`
  }).join("");
  previous=rank;localStorage.setItem("buy_app_prev_rank",JSON.stringify(previous));
}

updateRanking();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/api/watchlist")
def get_watchlist():
    try:
        return jsonify({"ok": True, "tickers": _get_watchlist()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/watchlist")
def add_watchlist():
    try:
        body = request.get_json(silent=True) or {}
        ticker = _sanitize_ticker(body.get("ticker", ""))
        if not ticker:
            return jsonify({"ok": False, "error": "銘柄コードを入力してください"}), 400

        tickers = _get_watchlist()
        if ticker in tickers:
            return jsonify({"ok": False, "error": "すでに登録されています"}), 400
        if len(tickers) >= logic.MAX_TICKERS:
            return jsonify({"ok": False, "error": f"登録できる銘柄は最大{logic.MAX_TICKERS}銘柄です"}), 400

        tickers.append(ticker)
        if not store.set_watchlist(tickers):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502

        return jsonify({"ok": True, "tickers": tickers})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.delete("/api/watchlist/<ticker>")
def delete_watchlist(ticker):
    try:
        ticker = _sanitize_ticker(ticker)
        tickers = [t for t in _get_watchlist() if t != ticker]
        if not store.set_watchlist(tickers):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502
        return jsonify({"ok": True, "tickers": tickers})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/ranking")
def ranking():
    try:
        tickers = _get_watchlist()
        rows = [_build_row(t) for t in tickers]
        rows.sort(key=lambda x: (
            logic.JUDGMENT_ORDER.get(x["judgment"], 99),
            -(x["upside_score"] if x["upside_score"] is not None else -1),
        ))

        last_refresh = store.get_last_refresh()
        last_refresh_view = None
        if last_refresh:
            last_refresh_view = {
                "run_at": last_refresh.get("run_at"),
                "success_count": len(last_refresh.get("success", [])),
                "failed_count": len(last_refresh.get("failed", [])),
                "skipped_count": len(last_refresh.get("skipped", [])),
            }

        budget_left, used = refresh.remaining_budget()

        return jsonify({
            "ok": True,
            "rows": rows,
            "last_refresh": last_refresh_view,
            "budget": {"used": used, "limit": logic.DAILY_API_BUDGET, "remaining": budget_left},
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"予期しないエラーが発生しました: {e}"}), 200


@app.post("/api/refresh")
def manual_refresh():
    try:
        if not API_KEY:
            return jsonify({"ok": False, "error": "ALPHAVANTAGE_API_KEYが未設定です"}), 200
        if not store.is_configured():
            return jsonify({"ok": False, "error": "Redis接続が未設定のため更新できません"}), 200

        last_manual = store.get_last_manual_refresh()
        if last_manual:
            try:
                last_dt = datetime.fromisoformat(last_manual)
                elapsed = (datetime.now(timezone.utc).astimezone() - last_dt).total_seconds()
                if elapsed < MANUAL_REFRESH_COOLDOWN:
                    wait_min = int((MANUAL_REFRESH_COOLDOWN - elapsed) / 60) + 1
                    return jsonify({
                        "ok": False,
                        "error": f"手動更新は前回から一定時間空ける必要があります（あと約{wait_min}分）",
                    }), 200
            except ValueError:
                pass

        budget_left, used = refresh.remaining_budget()
        if budget_left <= 0:
            return jsonify({
                "ok": False,
                "error": f"本日のAPI利用予算（{logic.DAILY_API_BUDGET}回）に達しています。翌日の自動更新をお待ちください。",
            }), 200

        tickers = _get_watchlist()
        targets = []
        for t in tickers:
            rec = store.get_ticker_record(t)
            if not rec or not rec.get("last_trade_date") or rec.get("is_stale"):
                targets.append(t)

        if not targets:
            return jsonify({"ok": True, "message": "更新の必要はありません（全銘柄が最新です）"})

        result = refresh.run_refresh(targets, API_KEY)
        store.set_last_manual_refresh(datetime.now(timezone.utc).astimezone().isoformat())

        msg = f"{len(result['success'])}件更新しました"
        if result["failed"]:
            msg += f"（{len(result['failed'])}件は取得失敗のため前回データのままです）"
        if result["skipped"]:
            msg += f"（{len(result['skipped'])}件はAPI予算切れのため未処理）"
        return jsonify({"ok": True, "message": msg, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": f"予期しないエラーが発生しました: {e}"}), 200


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "stock-buy-app",
        "max_tickers": logic.MAX_TICKERS,
        "api_key_configured": bool(API_KEY),
        "redis_configured": store.is_configured(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
