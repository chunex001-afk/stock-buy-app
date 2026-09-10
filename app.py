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

# Redisのキャッシュがこの時間を超えて古い場合のみ、手動更新の対象に含める
# （日次のGitHub Actionsジョブが何らかの理由で動かなかった場合の保険）。
CACHE_FRESH_SECONDS = 20 * 60 * 60  # 20時間

TICKER_RE = re.compile(r"[^A-Z0-9.\-]")


def _sanitize_ticker(raw):
    return TICKER_RE.sub("", (raw or "").strip().upper())


def _needs_refresh(record):
    """Redis上のレコードが「取得済みキャッシュとして十分新しいか」を判定する。"""
    if not record or not record.get("last_trade_date"):
        return True
    if record.get("is_stale"):
        return True
    fetched_at = record.get("fetched_at")
    if not fetched_at:
        return True
    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc).astimezone() - fetched_dt).total_seconds()
    return age > CACHE_FRESH_SECONDS


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
            "market_cap": None, "market_cap_label": None, "market_cap_text": None,
            "news": [],
            "judgment": "判定不可",
            "comment": "まだデータを取得できていません。追加直後は自動で取得を試みます。",
            "freshness": freshness,
        }

    market_cap = record.get("market_cap")
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
        "market_cap": market_cap,
        "market_cap_label": record.get("market_cap_label") or logic.classify_market_cap(market_cap),
        "market_cap_text": logic.format_market_cap(market_cap),
        "news": record.get("news", []),
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
.wrap{max-width:880px;margin:auto;padding:18px}
.title{font-size:24px;font-weight:800;margin-bottom:5px}
.sub{color:#68748a;margin-bottom:18px;font-size:13px;line-height:1.6}
.card{background:white;border-radius:22px;padding:18px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.controls{display:flex;gap:8px;flex-wrap:wrap}
.controls input{flex:1;min-width:150px;padding:13px;border:1px solid #ccd2db;border-radius:13px;font-size:16px}
button{border:0;border-radius:13px;padding:12px 16px;font-weight:800;font-size:15px;cursor:pointer;background:#172033;color:white}
button:disabled{opacity:.5;cursor:not-allowed}
button.secondary{background:#eef1f5;color:#172033;padding:8px 14px;font-size:13px}
.small{font-size:12px;color:#758096;margin-top:10px;line-height:1.6}

.list{display:flex;flex-direction:column;gap:14px}
.tcard{background:white;border-radius:20px;padding:18px 20px;box-shadow:0 2px 14px #0000000c}
.rankline{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:2px}
.rankbig{font-size:14px;font-weight:900;color:#98a2b3;white-space:nowrap}
.tickerbig{font-size:23px;font-weight:900;letter-spacing:.01em}
.rankarrow{font-size:15px;font-weight:900}
.judgebadge{margin-left:auto}
.rc-up{color:#087443}.rc-down{color:#b42318}.rc-same{color:#98a2b3}.rc-new{color:#98a2b3}
.rankdetail{font-size:12px;color:#758096;margin-bottom:12px}

.statrow{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #eef1f5}
.stat{min-width:60px}
.statlabel{font-size:11px;color:#98a2b3;font-weight:700;margin-bottom:2px}
.statval{font-size:16px;font-weight:800;white-space:nowrap}
.statval.up{color:#087443}.statval.down{color:#b42318}
.captag{display:block;font-size:10px;color:#98a2b3;font-weight:600;margin-top:1px}

.commentbox{background:#f7f9fc;border-radius:13px;padding:12px 14px;font-size:14px;line-height:1.65;margin-bottom:10px}
.newsblock{font-size:13px;line-height:1.7;margin-bottom:10px}
.newsblock a{color:#175cd3;text-decoration:none}
.newsblock a:hover{text-decoration:underline}
.newsblock .nonews{color:#98a2b3}

details.moredetail{margin-top:2px}
details.moredetail summary{cursor:pointer;font-size:12px;color:#475467;font-weight:700;list-style:none;padding:4px 0}
details.moredetail summary::-webkit-details-marker{display:none}
details.moredetail summary::before{content:"▸ "}
details.moredetail[open] summary::before{content:"▾ "}
.detailgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:10px 16px;margin:10px 0 6px;font-size:12px}
.detailgrid .dl{color:#98a2b3;margin-bottom:2px}
.detailgrid .dv{font-weight:700}

.cardfoot{display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:10px;flex-wrap:wrap}
.fresh{color:#087443;font-weight:700}.stalebadge{color:#8a6500;font-weight:700}.nonebadge{color:#98a2b3;font-weight:700}
.freshtag{font-size:11px}

.pill{display:inline-block;padding:6px 12px;border-radius:999px;font-weight:800;font-size:12px;white-space:nowrap}
.j-strong_buy{background:#087443;color:#fff}.j-buy{background:#e7f6ed;color:#087443}
.j-wait{background:#eef1f5;color:#475467}.j-overheat{background:#fff5cc;color:#8a6500}.j-unknown{background:#f2f2f2;color:#98a2b3;font-style:italic}

@media(max-width:480px){.wrap{padding:12px}.title{font-size:20px}.tcard{padding:14px 16px}.tickerbig{font-size:20px}.statrow{gap:14px}}
</style>
</head>
<body>
<div class="wrap">
<div class="title">🏆 今買うべき銘柄ランキング</div>
<div class="sub">実データ版｜最大15銘柄｜1日1回サーバー側で自動更新（GitHub Actions）｜銘柄追加時はその場で即時取得</div>

<div class="card">
  <div class="controls">
    <button onclick="manualRefresh()" id="refreshBtn">🔄 未取得/失敗分だけ今すぐ再取得</button>
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()" id="addBtn">＋追加（即時取得）</button>
  </div>
  <div id="status" class="small">読み込み中…</div>
</div>

<div class="list" id="list"></div>

<div class="card">
<b>🧠 判定ロジック（概要）</b>
<div class="small">
最終判定は「強く買いたい／買い候補／まだ買わない／過熱のため買わない」の4種類のみです。底打ち状態・局面・上昇余地スコア・過熱リスクスコアに加え、ニュースの内容（センチメント）と時価総額規模を補正材料として統合して決めます。<br>
RSIや高値からの乖離、1か月の上昇率が過大な場合は、底打ち後の反発局面であっても「過熱のため買わない」を優先します。単純に値上がり中の銘柄を高評価する設計ではありません。<br>
一言コメントはニュースがあれば最優先で反映し、無ければ銘柄固有の事業テーマとテクニカル指標から生成します。全銘柄で同じ文面にはなりません。<br>
順位変化は前日（直近の自動更新時点）のランキングとの比較です。カードの「詳細指標を見る」から、MA20/MA50・高値乖離・出来高比・上昇余地／過熱リスクスコア・底打ち状態などの内訳を確認できます。<br>
株価・ニュースは1日1回、GitHub Actionsによる自動ジョブがAlpha Vantageから取得しUpstash Redisに保存します。時価総額は変動が小さいため1回の自動更新につき最大1銘柄のみ取得し、API無料枠を圧迫しないようにしています。銘柄を「＋追加」した際はその銘柄のみ即時に取得します（右上のボタンは未取得・失敗銘柄限定の再取得です）。
</div>
</div>
</div>

<script>
const JBADGE = {
  "強く買いたい":"j-strong_buy","買い候補":"j-buy","まだ買わない":"j-wait","過熱のため買わない":"j-overheat","判定不可":"j-unknown"
};

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

async function updateRanking(extraMsg){
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
    if(j.rank_reference_date) statusMsg += ` ｜ 前日順位の基準日: ${fmtDate(j.rank_reference_date)}`;
    if(extraMsg) statusMsg = extraMsg + " ｜ " + statusMsg;
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
  const btn=document.getElementById("addBtn");
  btn.disabled=true;
  document.getElementById("status").textContent=`${t}を追加して即時取得中…`;
  try{
    const r=await fetch("/api/watchlist",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticker:t})});
    const j=await r.json();
    if(!j.ok){alert(j.error||"追加できませんでした");return;}
    el.value="";
    await updateRanking(j.fetch_note?`${t}: ${j.fetch_note}`:null);
  }catch(e){alert("追加エラー: "+e.message)}
  finally{btn.disabled=false;}
}

async function delTicker(t){
  try{
    const r=await fetch("/api/watchlist/"+encodeURIComponent(t),{method:"DELETE"});
    const j=await r.json();
    if(!j.ok){alert(j.error||"削除できませんでした");return;}
    await updateRanking();
  }catch(e){alert("削除エラー: "+e.message)}
}

function rankArrowHtml(rc){
  if(!rc) return "";
  if(rc.direction==="up") return `<span class="rankarrow rc-up">↑${rc.diff}</span>`;
  if(rc.direction==="down") return `<span class="rankarrow rc-down">↓${Math.abs(rc.diff)}</span>`;
  if(rc.direction==="same") return `<span class="rankarrow rc-same">→</span>`;
  return `<span class="rankarrow rc-new">NEW</span>`;
}

function newsHtml(news){
  if(!news || !news.length) return `<div class="newsblock"><span class="nonews">📰 直近の重要ニュースなし</span></div>`;
  return `<div class="newsblock">` + news.slice(0,2).map(n=>{
    const title = esc(n.title||"");
    return n.url
      ? `📰 <a href="${esc(n.url)}" target="_blank" rel="noopener">${title}</a>`
      : `📰 ${title}`;
  }).join("<br>") + `</div>`;
}

function freshTagHtml(f){
  f = f||{};
  if(f.status==="fresh") return `<span class="freshtag fresh">🟢 最新（${fmtDate(f.last_trade_date)}取引分・${fmtDt(f.fetched_at)}取得）</span>`;
  if(f.status==="stale") return `<span class="freshtag stalebadge">🟡 前回データ（${esc(f.last_error_label||"エラー")}）</span>`;
  return `<span class="freshtag nonebadge">⚪ データなし</span>`;
}

function render(rows){
  const list=document.getElementById("list");
  list.innerHTML = rows.map((x,i)=>{
    const cls = JBADGE[x.judgment]||"j-unknown";
    const rc = x.rank_change;
    return `<div class="tcard">
      <div class="rankline">
        <span class="rankbig">${i+1}位</span>
        <span class="tickerbig">${esc(x.ticker)}</span>
        ${rankArrowHtml(rc)}
        <span class="judgebadge pill ${cls}">${esc(x.judgment)}</span>
      </div>
      <div class="rankdetail">${rc?esc(rc.label):""}</div>

      <div class="statrow">
        <div class="stat"><div class="statlabel">株価</div><div class="statval">${fmt(x.price)}</div></div>
        <div class="stat"><div class="statlabel">前日比</div><div class="statval ${x.change_pct>0?'up':x.change_pct<0?'down':''}">${fmt(x.change_pct,"%")}</div></div>
        <div class="stat"><div class="statlabel">1ヶ月</div><div class="statval ${x.month_return>0?'up':x.month_return<0?'down':''}">${fmt(x.month_return,"%")}</div></div>
        <div class="stat"><div class="statlabel">RSI14</div><div class="statval">${fmt(x.rsi14)}</div></div>
        <div class="stat"><div class="statlabel">時価総額</div><div class="statval">${esc(x.market_cap_text||"—")}${x.market_cap_label?`<span class="captag">${esc(x.market_cap_label)}</span>`:""}</div></div>
      </div>

      <div class="commentbox">💬 ${esc(x.comment||"")}</div>
      ${newsHtml(x.news)}

      <details class="moredetail">
        <summary>詳細指標を見る</summary>
        <div class="detailgrid">
          <div><div class="dl">MA20</div><div class="dv">${fmt(x.ma20)}</div></div>
          <div><div class="dl">MA50</div><div class="dv">${fmt(x.ma50)}</div></div>
          <div><div class="dl">高値乖離</div><div class="dv">${fmt(x.high_gap,"%")}</div></div>
          <div><div class="dl">出来高比</div><div class="dv">${fmt(x.volume_ratio)}</div></div>
          <div><div class="dl">上昇余地</div><div class="dv">${fmt(x.upside_score)}</div></div>
          <div><div class="dl">過熱リスク</div><div class="dv">${fmt(x.overheat_score)}</div></div>
          <div><div class="dl">底打ち状態</div><div class="dv">${esc(x.bottom_status||"—")}</div></div>
          <div><div class="dl">局面</div><div class="dv">${esc(x.phase||"—")}</div></div>
        </div>
      </details>

      <div class="cardfoot">
        ${freshTagHtml(x.freshness)}
        <button class="secondary" onclick="delTicker('${esc(x.ticker)}')">削除</button>
      </div>
    </div>`;
  }).join("");
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

        # 追加直後にその銘柄だけ即時取得する。GitHub Actionsの日次更新は待たない。
        # 無駄なAPI呼び出しを避けるため、対象は今追加した1銘柄のみで、
        # 本日のAPI予算が残っていない場合は取得をスキップする（次回自動更新で反映）。
        fetch_note = None
        if not API_KEY:
            fetch_note = "APIキー未設定のため、次回の自動更新までデータは表示されません。"
        elif not store.is_configured():
            fetch_note = "Redis未設定のため即時取得はできません。"
        else:
            budget_left, _ = refresh.remaining_budget()
            if budget_left <= 0:
                fetch_note = "本日のAPI利用予算に達しているため、次回の自動更新でデータが反映されます。"
            else:
                try:
                    result = refresh.run_refresh([ticker], API_KEY)
                    if ticker in result["success"]:
                        fetch_note = "最新データを取得しました。"
                    elif result["failed"]:
                        err_type = result["failed"][0].get("type")
                        fetch_note = (
                            f"データ取得に失敗しました（{logic.ERROR_LABELS.get(err_type, err_type)}）。"
                            "次回の自動更新をお待ちください。"
                        )
                    else:
                        fetch_note = "データを取得できませんでした。次回の自動更新をお待ちください。"
                except Exception as e:
                    fetch_note = f"即時取得中にエラーが発生しました: {e}"

        return jsonify({"ok": True, "tickers": tickers, "fetch_note": fetch_note})
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


def _attach_rank_changes(rows):
    """RedisのRank_snapshot_prev（前日順位）と当日の順位を比較して各行に付与する。"""
    prev_snapshot = store.get_json("rank_snapshot_prev") or {}
    prev_ranks = prev_snapshot.get("ranks", {}) if isinstance(prev_snapshot, dict) else {}
    prev_date = prev_snapshot.get("date") if isinstance(prev_snapshot, dict) else None

    for i, row in enumerate(rows):
        today_rank = i + 1
        yesterday_rank = prev_ranks.get(row["ticker"])
        if yesterday_rank is None:
            row["rank_change"] = {
                "yesterday": None, "today": today_rank, "diff": None,
                "direction": "new", "label": f"今日{today_rank}位（前日データなし）",
            }
            continue
        diff = yesterday_rank - today_rank
        if diff > 0:
            direction, label = "up", f"昨日{yesterday_rank}位 → 今日{today_rank}位 ↑{diff}"
        elif diff < 0:
            direction, label = "down", f"昨日{yesterday_rank}位 → 今日{today_rank}位 ↓{abs(diff)}"
        else:
            direction, label = "same", f"昨日{yesterday_rank}位 → 今日{today_rank}位 →"
        row["rank_change"] = {
            "yesterday": yesterday_rank, "today": today_rank, "diff": diff,
            "direction": direction, "label": label,
        }
    return prev_date


@app.get("/api/ranking")
def ranking():
    try:
        tickers = _get_watchlist()
        rows = [_build_row(t) for t in tickers]
        rows = logic.sort_rows(rows)
        rank_reference_date = _attach_rank_changes(rows)

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
            "rank_reference_date": rank_reference_date,
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
        targets = [t for t in tickers if _needs_refresh(store.get_ticker_record(t))]

        if not targets:
            return jsonify({"ok": True, "message": "更新の必要はありません（全銘柄が20時間以内に取得済みです）"})

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
