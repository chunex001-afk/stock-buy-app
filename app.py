import os
import re
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string

import quintile_logic
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


# Q1〜Q5の表示ラベル(design 13の方針: Q3=発見・Q4=準備・Q5=購入判断。
# Q5→Q4を「失敗」「売却」等の否定的な意味にしない、既存の購入判定
# upside_score/judgment等とは完全に独立した別軸の表示であることに注意)。
QUINTILE_LABELS = {
    "Q1": "Q1（低調）", "Q2": "Q2（弱含み）", "Q3": "Q3（発見）",
    "Q4": "Q4（準備）", "Q5": "Q5（購入判断）",
}


def _build_quintile_view(ticker):
    """Q1〜Q5表示用データを組み立てる。Redisの`quintile:state:<TICKER>`を
    読むだけで、Twelve Dataへのライブ呼び出しは一切行わない(design 12)。
    まだ日次バッチで一度も判定されていない銘柄は「判定待ち」として表示する。"""
    state = store.get_quintile_state(ticker)
    if not state or not state.get("current_q"):
        return {
            "status": "pending",
            "current_q": None, "current_q_label": None,
            "previous_q": None, "last_updated": None,
            "history": [], "q5_stats": None,
            "message": "Q判定は次回日次更新後に反映されます。",
        }

    current_q = state.get("current_q")
    view = {
        "status": "ready",
        "current_q": current_q,
        "current_q_label": QUINTILE_LABELS.get(current_q, current_q),
        "previous_q": state.get("previous_q"),
        "last_updated": state.get("last_updated"),
        "history": state.get("history", []),
        "q5_stats": None,
        "message": None,
    }
    if current_q == "Q5":
        try:
            view["q5_stats"] = quintile_logic.load_q5_stats()
        except Exception:
            view["q5_stats"] = None
    return view


def _build_row(ticker):
    record = store.get_ticker_record(ticker)
    freshness = _freshness(record)
    quintile_view = _build_quintile_view(ticker)

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
            "quintile": quintile_view,
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
        "quintile": quintile_view,
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
<title>保有銘柄のQ1〜Q5状態</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#f4f6f8;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
.wrap{max-width:880px;margin:auto;padding:18px}
@media(min-width:760px){.wrap{max-width:1120px}}
.title{font-size:24px;font-weight:800;margin-bottom:5px}
.sub{color:#68748a;margin-bottom:18px;font-size:13px;line-height:1.6}
.card{background:white;border-radius:22px;padding:18px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.controls{display:flex;gap:8px;flex-wrap:wrap}
.controls input{flex:1;min-width:150px;padding:13px;border:1px solid #ccd2db;border-radius:13px;font-size:16px}
button{border:0;border-radius:13px;padding:12px 16px;font-weight:800;font-size:15px;cursor:pointer;background:#172033;color:white}
button:disabled{opacity:.5;cursor:not-allowed}
button.secondary{background:#eef1f5;color:#172033;padding:8px 14px;font-size:13px}
.small{font-size:12px;color:#758096;margin-top:10px;line-height:1.6;min-height:14px}

details.opinfo{margin-top:6px}
details.opinfo summary{cursor:pointer;font-size:11px;color:#98a2b3;font-weight:700;list-style:none}
details.opinfo summary::-webkit-details-marker{display:none}
details.opinfo summary::before{content:"▸ "}
details.opinfo[open] summary::before{content:"▾ "}
details.opinfo .opbody{font-size:11px;color:#98a2b3;margin-top:6px;line-height:1.7}

details.logicinfo summary{cursor:pointer;font-size:15px;list-style:none;padding:2px 0}
details.logicinfo summary::-webkit-details-marker{display:none}
details.logicinfo summary::before{content:"▸ ";color:#758096}
details.logicinfo[open] summary::before{content:"▾ ";color:#758096}
details.logicinfo .small{margin-top:10px}

.zerobanner{background:#eef1f5;color:#475467;font-weight:800;padding:14px 18px;border-radius:16px;margin-bottom:14px;font-size:15px;line-height:1.5}
.zerobanner .sub2{display:block;font-weight:600;font-size:12px;color:#758096;margin-top:3px}

/* Q5該当銘柄のハイライト表示(design方針: Q1〜Q5をメインの判定として扱う)。
   旧upside_score/judgment基準の「今日の注目銘柄」表示は2026-09-16のUI修正で
   置き換えた(Q1〜Q5と混在して矛盾して見えることを避けるため)。 */
.hero{border-radius:22px;padding:20px 22px;margin-bottom:18px;box-shadow:0 4px 20px #0000001a}
.hero-buy{background:linear-gradient(135deg,#0c7a49,#0a5c38);color:#fff}
.herolabel{font-size:12px;font-weight:800;opacity:.9;margin-bottom:10px;letter-spacing:.02em}
.heroline{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.heroticker{font-size:26px;font-weight:900;letter-spacing:.01em}
.herocomment{font-size:14px;line-height:1.75;background:rgba(255,255,255,.16);border-radius:14px;padding:14px 16px}

.list{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:760px){.list{grid-template-columns:repeat(2,1fr)}}

.tcard{background:white;border-radius:20px;padding:18px 20px;box-shadow:0 2px 14px #0000000c;border-left:5px solid #e5e7eb}
.tcard.cq-q5{border-left-color:#087443}
.tcard.cq-q4{border-left-color:#c98a00}
.tcard.cq-q3{border-left-color:#175cd3}
.tcard.cq-q2{border-left-color:#c9cfd8}
.tcard.cq-q1{border-left-color:#c9cfd8}
.tcard.cq-pending{border-left-color:#e5e7eb}
.rankline{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:2px}
.tickerbig{font-size:20px;font-weight:900;letter-spacing:.01em}
.rankdetail{font-size:12px;color:#758096;margin-bottom:12px}

.statrow{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #eef1f5}
.stat{min-width:60px}
.statlabel{font-size:11px;color:#98a2b3;font-weight:700;margin-bottom:2px}
.statval{font-size:16px;font-weight:800;white-space:nowrap}
.statval.up{color:#087443}.statval.down{color:#b42318}
.captag{display:block;font-size:10px;color:#98a2b3;font-weight:600;margin-top:1px}

.newsblock{font-size:13px;line-height:1.7;margin-bottom:10px}
.newsblock a{color:#175cd3;text-decoration:none}
.newsblock a:hover{text-decoration:underline}
.newsblock .nonews{color:#98a2b3}

.cardfoot{display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:10px;flex-wrap:wrap}
.fresh{color:#087443;font-weight:700}.stalebadge{color:#8a6500;font-weight:700}.nonebadge{color:#98a2b3;font-weight:700}
.freshtag{font-size:11px}

/* Q1〜Q5(参照母集団内の相対的な状態、既存judgmentとは別軸の情報。
   design 13: Q3=発見・Q4=準備・Q5=購入判断。Q5→Q4を否定的な色にしない) */
.qbadge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:800;font-size:11px;white-space:nowrap}
.q-q1{background:#f2f2f2;color:#98a2b3}.q-q2{background:#eef1f5;color:#758096}
.q-q3{background:#eaf2ff;color:#175cd3}.q-q4{background:#fff1db;color:#9a6a00}
.q-q5{background:#087443;color:#fff}.q-pending{background:#f2f2f2;color:#98a2b3;font-style:italic}
.qdaily-wrap{overflow-x:auto;margin:8px 0;-webkit-overflow-scrolling:touch}
.qdaily-table{border-collapse:collapse;background:#f7f9fc;border-radius:13px;width:100%}
.qdaily-table th,.qdaily-table td{padding:7px 8px;text-align:center;min-width:50px;white-space:nowrap}
.qdaily-table th{font-size:10px;color:#98a2b3;font-weight:700;border-bottom:1px solid #e5e7eb}
.qdaily-table td{font-size:14px;font-weight:800;color:#172033}
.qcont{margin:6px 2px 0;font-size:13px;font-weight:800;color:#087443}
.q5stats{background:#f0f9f4;border-radius:13px;padding:12px 14px;font-size:12px;line-height:1.8;margin-top:8px}
.q5stats .q5title{font-weight:800;color:#087443;margin-bottom:4px}
.q5stats .q5note{color:#758096;font-size:11px;margin-top:6px}

@media(max-width:480px){.wrap{padding:12px}.title{font-size:20px}.tcard{padding:14px 16px}.tickerbig{font-size:18px}.statrow{gap:14px}.heroticker{font-size:26px}}
</style>
</head>
<body>
<div class="wrap">
<div class="title">📊 保有銘柄のQ1〜Q5状態</div>
<div class="sub">実データ版｜最大15銘柄｜毎日サーバー側で自動更新｜各銘柄が現在Q1〜Q5のどの状態かを確認できます</div>

<div class="card">
  <div class="controls">
    <button onclick="manualRefresh()" id="refreshBtn">🔄 未取得/失敗分だけ今すぐ再取得</button>
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()" id="addBtn">＋追加（即時取得）</button>
  </div>
  <div id="status" class="small">読み込み中…</div>
  <details class="opinfo"><summary>運用情報</summary><div id="opinfo" class="opbody"></div></details>
</div>

<div id="hero"></div>
<div class="list" id="list"></div>

<div class="card">
<details class="logicinfo">
<summary><b>🧠 Q1〜Q5判定について（概要）</b></summary>
<div class="small">
複数の市場データ・テクニカル指標から、各銘柄の現在の状態をQ1〜Q5の5段階で判定します。<br>
Q3＝発見、Q4＝準備、Q5＝購入判断という位置づけです。Q5→Q4への変化は「失敗」「売却」等を意味するものではありません。<br>
Q5に表示される過去実績統計（60日/120日最大上昇率・到達率）は、過去にQ5と判定された局面の統計的な実績であり、この銘柄が将来同じように上がることを予測するものではありません。<br>
状態履歴には、日々の判定結果（今日・昨日・直近数日のQ状態、Q5継続日数）を表示します。記録がない日は「—」と表示され、過去データを推測で補うことはしていません。<br>
Q1〜Q5判定は1日1回、GitHub Actionsによる自動ジョブがTwelve Dataから取得しUpstash Redisに保存します。銘柄を「＋追加」した際は、次回の自動更新以降にQ1〜Q5判定が反映されます。
</div>
</details>
</div>
</div>

<script>
// メイン画面はQ1〜Q5の情報のみで構成する(2026-09-16のUI修正で旧judgment/
// 旧コメント/upside_score等の従来指標の表示を完全に削除した)。
const QBADGE = {"Q1":"q-q1","Q2":"q-q2","Q3":"q-q3","Q4":"q-q4","Q5":"q-q5"};

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
  document.getElementById("status").textContent = extraMsg || "読み込み中…";
  try{
    const r=await fetch("/api/ranking");
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"取得失敗");
    render(j.rows);
    renderOpInfo(j);
    document.getElementById("status").textContent = extraMsg || "";
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
}

function renderOpInfo(j){
  const lr=j.last_refresh;
  const budget=j.budget||{};
  let msg = lr
    ? `自動更新: ${lr.success_count}件成功／${lr.failed_count}件失敗（${fmtDt(lr.run_at)}実行）`
    : "自動更新はまだ実行されていません";
  msg += ` ｜ 本日のAPI使用: ${budget.used??"—"}/${budget.limit??"—"}`;
  document.getElementById("opinfo").textContent = msg;
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

// Q1〜Q5(参照母集団内での相対的な状態)。既存のupside_score/judgmentとは
// 完全に独立した別軸の情報であり、混同されないよう別バッジとして表示する。
function qBadgeHtml(q){
  q = q || {};
  if(q.status !== "ready"){
    return `<span class="qbadge q-pending">Q判定：次回日次更新後に反映</span>`;
  }
  return `<span class="qbadge ${QBADGE[q.current_q]||"q-pending"}">${esc(q.current_q_label)}</span>`;
}

// 同じ日付のエントリが連続する場合(同日に複数回バッチが走った場合など)、
// その日の最新の状態だけを残す。日付をまたいだ本来の状態推移(例: 9/15 Q5 →
// 9/16 Q2)はそのまま表示する(2026-09-16のUI修正で追加、表示層のみの対応)。
function dedupeHistoryByDate(history){
  const out = [];
  for(const h of history){
    if(out.length && out[out.length-1].date === h.date){
      out[out.length-1] = h;
    }else{
      out.push(h);
    }
  }
  return out;
}

// dateStr("YYYY-MM-DD")にnDays日を加算した日付文字列を返す(負数で過去方向)。
function addDaysToDateStr(dateStr, nDays){
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + nDays);
  return d.toISOString().slice(0, 10);
}

function daysBetweenDateStr(fromStr, toStr){
  const a = new Date(fromStr + "T00:00:00Z");
  const b = new Date(toStr + "T00:00:00Z");
  return Math.round((b - a) / 86400000);
}

// historyの保存方式(Qが変化した日だけ記録)はそのまま前提とし、表示層だけで
// 「直近の確定した状態」を前方補完(carry-forward)する。historyはdate昇順
// (dedupeHistoryByDate適用後)である前提で、targetDate以前の最新エントリを
// 探す。それより前に一件もエントリがない日は「未確定」として「—」を返す
// (過去のデータそのものを書き換えたり推測で作ったりはしない、表示上の補完のみ)。
function resolveQForDate(history, targetDate){
  let result = null;
  for(const h of history){
    if(h.date <= targetDate) result = h.q;
    else break;
  }
  return result;
}

// 今日・昨日・2〜5日前(計6日分)のQ状態と、Q5継続日数を横長の表で表示する。
// バックエンド(history保存方式・refresh.py・quintile_logic.py・Q1〜Q5判定
// ロジック)は一切変更しない、表示層のみの対応。2026-09-17のUI修正で
// 5日分→6日分・縦並びの補完なし表示→横長テーブル+前方補完表示に変更。
function qDailyBreakdownHtml(q){
  if(!q || q.status !== "ready" || !q.last_updated) return "";
  const history = dedupeHistoryByDate(q.history || []);
  const anchor = q.last_updated;
  const labels = ["今日", "昨日", "2日前", "3日前", "4日前", "5日前"];

  const values = labels.map((label, i) => {
    // 今日は必ずcurrent_q(最新の実際の判定結果)を使う。
    if(i === 0) return q.current_q;
    const targetDate = addDaysToDateStr(anchor, -i);
    return resolveQForDate(history, targetDate);
  });

  const headCells = labels.map(l => `<th>${esc(l)}</th>`).join("");
  const valCells = values.map(v => `<td>${v ? esc(v) : "—"}</td>`).join("");

  let cont = "";
  if(q.current_q === "Q5" && history.length){
    const lastEntry = history[history.length - 1];
    const days = daysBetweenDateStr(lastEntry.date, anchor) + 1;
    cont = `<div class="qcont">Q5継続：${days}日</div>`;
  }

  return `<div class="qdaily-wrap"><table class="qdaily-table"><thead><tr>${headCells}</tr></thead>`
    + `<tbody><tr>${valCells}</tr></tbody></table></div>${cont}`;
}

// Q5の過去実績統計。あくまで「過去の類似状態における統計」であり、
// 将来この銘柄が同じように上がると予測するものではないことを明記する(design 14)。
function q5StatsHtml(q){
  if(!q || q.status !== "ready" || q.current_q !== "Q5" || !q.q5_stats) return "";
  const s = q.q5_stats;
  const events = (s.events||[]).map(e=>
    `${e.within_days}日以内+${e.threshold_pct}%到達: ${e.reach_rate_pct}%(n=${e.n})`
  ).join("　");
  return `<div class="q5stats">
    <div class="q5title">📊 Q5該当銘柄の過去の類似状態における実績統計</div>
    <div>60日最大上昇率 中央値: ${s.h60_median_pct}%(n=${s.h60_n})　120日: ${s.h120_median_pct}%(n=${s.h120_n})</div>
    <div>${events}</div>
    <div class="q5note">※将来の予測ではなく、過去にQ5と判定された局面の統計的な実績です。${esc(s.note||"")}</div>
  </div>`;
}

// Q1〜Q5をメインの判定として扱う(design方針)。旧upside_score/judgment基準の
// 「今日の注目銘柄」ランキング表示はここでは使わない(Q1〜Q5と旧判定が同じ画面で
// 矛盾して見えることを避けるため、2026-09-16のUI修正で全面的に置き換えた)。
function renderHero(rows){
  const heroEl = document.getElementById("hero");
  if(!rows || !rows.length){ heroEl.innerHTML=""; return; }

  const ready = rows.filter(x=>x.quintile && x.quintile.status==="ready");
  const q5 = ready.filter(x=>x.quintile.current_q==="Q5");

  if(q5.length){
    const names = q5.map(x=>esc(x.ticker)).join("　");
    heroEl.innerHTML = `
      <div class="hero hero-buy">
        <div class="herolabel">📌 現在Q5（購入判断）の銘柄</div>
        <div class="heroline"><span class="heroticker">${names}</span></div>
        <div class="herocomment">💬 過去の類似Q5状態の実績統計は、各銘柄カード内をご確認ください。「必ず上がる」という意味ではありません。</div>
      </div>
    `;
    return;
  }

  if(ready.length){
    heroEl.innerHTML = `<div class="zerobanner">📋 現在Q5（購入判断）の銘柄はありません<span class="sub2">Q1〜Q5判定は各銘柄カードでご確認いただけます</span></div>`;
    return;
  }

  heroEl.innerHTML = `<div class="zerobanner">⏳ Q1〜Q5判定はまだありません<span class="sub2">次回の日次更新（GitHub Actions）後に反映されます</span></div>`;
}

function render(rows){
  renderHero(rows);
  const list=document.getElementById("list");
  list.innerHTML = rows.map((x,i)=>{
    const q = x.quintile || {};
    const cardCls = q.status==="ready" ? (QBADGE[q.current_q]||"q-pending").replace("q-","cq-") : "cq-pending";
    return `<div class="tcard ${cardCls}">
      <div class="rankline">
        <span class="tickerbig">${esc(x.ticker)}</span>
        ${qBadgeHtml(x.quintile)}
      </div>
      ${qDailyBreakdownHtml(x.quintile)}
      ${q5StatsHtml(x.quintile)}

      <div class="statrow">
        <div class="stat"><div class="statlabel">株価</div><div class="statval">${fmt(x.price)}</div></div>
        <div class="stat"><div class="statlabel">前日比</div><div class="statval ${x.change_pct>0?'up':x.change_pct<0?'down':''}">${fmt(x.change_pct,"%")}</div></div>
        <div class="stat"><div class="statlabel">1ヶ月</div><div class="statval ${x.month_return>0?'up':x.month_return<0?'down':''}">${fmt(x.month_return,"%")}</div></div>
        <div class="stat"><div class="statlabel">RSI14</div><div class="statval">${fmt(x.rsi14)}</div></div>
        <div class="stat"><div class="statlabel">時価総額</div><div class="statval">${esc(x.market_cap_text||"—")}${x.market_cap_label?`<span class="captag">${esc(x.market_cap_label)}</span>`:""}</div></div>
      </div>

      ${newsHtml(x.news)}

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


@app.get("/api/ranking")
def ranking():
    try:
        tickers = _get_watchlist()
        rows = [_build_row(t) for t in tickers]
        rows = logic.sort_rows(rows)

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
