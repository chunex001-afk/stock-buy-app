import bisect
import os
import re
from datetime import date, timedelta

from flask import Flask, jsonify, request, render_template_string

import beta_logic
import quintile_logic
import redis_store as store
import stock_logic as logic
import refresh
import twelvedata_client as td

app = Flask(__name__)

# 2026-09-17: Alpha Vantage完全撤去・Twelve Data一本化。app.pyからTwelve Data
# へ直接アクセスすることはなく、常にrefresh.backfill_quintile_history_for_new_ticker
# 経由で呼び出す(design方針「Twelve Dataをapp.pyから直接呼ばない」を維持)。
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

TICKER_RE = re.compile(r"[^A-Z0-9.\-]")


def _sanitize_ticker(raw):
    return TICKER_RE.sub("", (raw or "").strip().upper())


def _freshness(record):
    """画面表示用の鮮度情報を組み立てる。

    2026-09-17(SKHY/AXT障害調査を受けて): last_trade_dateが無い(=旧指標を
    一度も正常計算できていない)場合でも、last_error/last_error_messageが
    Redisに記録されていればそれをそのまま返すようにした。「Q1〜5はready
    なのに旧指標は判定不可で原因不明」という状態をなくすことが目的で、
    表示ロジックの追加のみ。Q1〜Q5判定・Q5シグナル等には一切関係しない。"""
    if not record or not record.get("last_trade_date"):
        last_error = (record or {}).get("last_error")
        last_error_message = (record or {}).get("last_error_message")
        return {
            "status": "none",
            "label": "⚪ データなし" if not last_error else "🔴 取得失敗",
            "last_trade_date": None,
            "fetched_at": None,
            "last_error_label": logic.ERROR_LABELS.get(last_error, last_error) if last_error else None,
            "last_error_message": last_error_message,
        }

    is_stale = bool(record.get("is_stale"))
    if not is_stale:
        return {
            "status": "fresh",
            "label": "🟢 最新",
            "last_trade_date": record.get("last_trade_date"),
            "fetched_at": record.get("fetched_at"),
            "last_error_label": None,
            "last_error_message": None,
        }

    err_type = record.get("last_error")
    return {
        "status": "stale",
        "label": "🟡 前回データ",
        "last_trade_date": record.get("last_trade_date"),
        "fetched_at": record.get("fetched_at"),
        "last_error_label": logic.ERROR_LABELS.get(err_type, err_type or "不明なエラー"),
        "last_error_message": record.get("last_error_message"),
    }


# Q1〜Q5の表示ラベル(design 13の方針: Q3=発見・Q4=準備・Q5=購入判断。
# Q5→Q4を「失敗」「売却」等の否定的な意味にしない、既存の購入判定
# upside_score/judgment等とは完全に独立した別軸の表示であることに注意)。
QUINTILE_LABELS = {
    "Q1": "Q1（低調）", "Q2": "Q2（弱含み）", "Q3": "Q3（発見）",
    "Q4": "Q4（準備）", "Q5": "Q5（購入判断）",
}

# Q5シグナル(新規購入シグナル)の有効期限。2026-09-17のバックテスト検証
# (fork「aca4da7」によるQ5→Q4/Q3/Q2/Q1遷移・経過日数・状態遷移モデルの検証)
# で、Q5から離脱すると優位性の大半は1日目で失われるが、8日目あたりまでは
# 「直近Q5履歴なし」の水準との差がまだ残ることが確認された。この検証結果に
# 基づきユーザーが確定した仕様(2026-09-17)：最後にQ5になった日から8日間を
# 新規購入シグナルの有効期間とする。既存のQ1〜Q5判定ロジック(quintile_logic.py
# の9特徴量計算・pred_score・分位境界・assign_quintile)には一切触れない。
#
# 2026-09-22改定: 単位を「8暦日」から「8取引日」に変更した(土日・米国市場
# 休場日調査の結果、Q5 Day0〜Day5(5取引日)・Q5警告(最大40取引日)は元々
# 取引日ベースの設計だったのに対し、このQ5シグナルだけが暦日差で実装されて
# おり土日を挟むたびに有効期間が実質的に縮む不整合があったため、ユーザー確定
# 仕様として取引日ベースに統一する)。土日・米国市場休場日を8日にカウント
# しない。
Q5_SIGNAL_EXPIRY_DAYS = 8


def _days_between(from_date_str, to_date_str):
    """2つの"YYYY-MM-DD"文字列の間の暦日差(to - from)を返す。
    2026-09-22以降、_compute_q5_signalの主経路では使わない(下記
    _trading_days_betweenに置き換えた)。取引日カレンダーが取得できない
    起動直後などの縮退時フォールバックとしてのみ残す。"""
    return (date.fromisoformat(to_date_str) - date.fromisoformat(from_date_str)).days


def _trading_days_calendar():
    """quintile:pool_historyに蓄積されている「実際に処理された取引日」の
    一覧(refresh.run_quintile_refreshが実取引日ベース化(2026-09-22)された
    ことにより、新しい取引日を検出した時にしか追加されなくなった)を、
    Q5シグナルの8取引日判定用カレンダーとして再利用する。新規のRedisキーは
    追加しない(design: 既存のpool_historyだけで正確に取引日を数えられる)。
    直近252営業日分(POOL_WINDOW_DAYS)しか保持されないが、8取引日の判定には
    十分な範囲。取得できない場合は空リストを返す(呼び出し側は暦日フォール
    バックに切り替える)。"""
    pool_history = store.get_pool_history()
    if not pool_history:
        return []
    return sorted(pool_history.get("dates", []))


def _trading_days_between(calendar_dates, from_date_str, to_date_str):
    """calendar_dates(ソート済み"YYYY-MM-DD"文字列リスト)のうち、
    from_date_str以上to_date_str以下(両端含む)の件数を返す。
    「離脱日を1取引日目として数えた、anchor_dateまでの経過取引日数」に使う
    (from_date_str==to_date_strなら1を返す、旧_days_between+1の取引日版)。"""
    lo = bisect.bisect_left(calendar_dates, from_date_str)
    hi = bisect.bisect_right(calendar_dates, to_date_str)
    return hi - lo


def _last_trading_day_before(calendar_dates, date_str):
    """calendar_dates中でdate_strより前の最後の取引日を返す(表示用の
    day0_date算出のみに使う、見つからなければNone)。"""
    idx = bisect.bisect_left(calendar_dates, date_str) - 1
    return calendar_dates[idx] if idx >= 0 else None


def _dedupe_history_by_date(history):
    """同じ日付のエントリが連続する場合、その日の最終状態だけを残す。
    JS側のdedupeHistoryByDate(今日〜5日前テーブルの表示に使用)と全く同じ
    正規化をPython側でも行う(design: 2026-09-17本番確認で発覚した不整合の
    修正)。同日中にQ1〜5バッチが複数回実行され、その都度プール境界が変わって
    Q値が複数回変化した場合でも(例: 同日中にQ5→Q2→Q3のように記録された場合)、
    「その日に実際にQ5だったこと」にはならない(最終的にQ5でなかった以上、
    その日をQ5として扱うと架空の実績になってしまうため)。既存のhistory保存
    方式(refresh.py)・表示テーブルのロジックは一切変更しない、表示専用の
    正規化。"""
    out = []
    for h in history:
        if out and out[-1].get("date") == h.get("date"):
            out[-1] = h
        else:
            out.append(h)
    return out


def _compute_q5_signal(current_q, history, anchor_date, trading_calendar=None):
    """Q5シグナル(新規購入シグナル)の状態を計算する(design: 上記
    Q5_SIGNAL_EXPIRY_DAYS参照)。既存のhistory(Qが変化した日だけを記録する
    変化ログ、redis_store/refresh.py無変更)から導出するだけの表示専用ロジック
    であり、Redisへの新規書き込みは行わない。

    trading_calendar: _trading_days_calendar()が返す、実際に処理された取引日
    の一覧(ソート済み)。渡された場合、経過日数は暦日差ではなく取引日数で
    数える(2026-09-22改定)。空/Noneの場合のみ、旧来の暦日差にフォールバック
    する(起動直後でpool_historyが空、等の縮退時のみを想定)。

    仕様(ユーザー確定、2026-09-17。日数の単位は2026-09-22に暦日→取引日へ改定):
    - 判定の前に、まずhistoryを_dedupe_history_by_dateで正規化する(同一日付に
      複数のQが記録されている場合、その日の最終状態だけを採用する。2026-09-17
      の本番確認で、同日中の複数回バッチ実行により実在しない日付をDay0として
      表示してしまう不整合が見つかったため追加した正規化ステップ)。
    - 「Q5シグナルDay 0」= 正規化後のhistoryにおける「最後にQ5だった日」。
      現在Q5ならそのQ5エントリの日付。Q5から外れている場合は、historyが
      「変化した日だけ」を記録する仕様のため、Q5エントリの直後にある
      「Q5から変化した日」の前日を「最後にQ5だった日」として逆算する(Q5が
      複数日連続した場合、historyにはQ5開始日しか残らないため、これを
      そのままDay0にすると、長くQ5が続いた銘柄ほど離脱直後から不当に失効
      扱いになってしまうバグがあり、この逆算で回避する)。正規化後の
      historyにQ5エントリが1件も残っていなければ(=その日の最終状態としては
      一度もQ5になっていない)、架空の日付は一切生成せずNoneを返す。
    - 現在Q5なら status="ok"(経過日数によらず常に「購入OK」)。
    - Q5から外れている場合、上記Day0からanchor_date(通常は当日=last_updated)
      までの経過日数を数え、Q5_SIGNAL_EXPIRY_DAYS(8日)未満ならstatus="active"
      (シグナルまだ有効)、8日以上でstatus="expired"(失効)。
    - 8日が経過する前に再びQ5になった場合、正規化後のhistoryには新しい
      q=="Q5"エントリが追加されるため、このロジックは自動的にその新しい
      日付をDay0として扱う(Q5→Q4→Q3→Q4→Q5のように途中で複数回Q3/Q4を
      経由しても、直近のQ5エントリだけを見るため、再度Q5になった時点で
      自動的に新しいシグナルへリセットされる。追加の状態保存は不要)。
    - 一度もQ5になったことがなければNoneを返す(シグナル自体が存在しない)。
    - Q5からの低下(Q4/Q3等)は売却シグナルとして扱わない(新規購入判断専用、
      既存保有分の売却判断はこの仕組みの対象外)。
    """
    if not history:
        return None

    history = _dedupe_history_by_date(history)
    if not history:
        return None

    last_q5_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("q") == "Q5":
            last_q5_idx = i
            break
    if last_q5_idx is None:
        return None

    if current_q == "Q5":
        return {"status": "ok", "day0_date": history[last_q5_idx]["date"], "days_elapsed": 0}

    if last_q5_idx + 1 >= len(history):
        # current_q!=Q5なのに直後の離脱エントリが存在しない状態で、
        # 通常の日次更新フローでは起こらないはずだが、念のため未確定として扱う。
        return None
    depart_date = history[last_q5_idx + 1]["date"]
    if trading_calendar:
        day0_date = _last_trading_day_before(trading_calendar, depart_date) or (
            date.fromisoformat(depart_date) - timedelta(days=1)
        ).isoformat()
        days_elapsed = _trading_days_between(trading_calendar, depart_date, anchor_date)
    else:
        # 取引日カレンダーが取得できない場合のみの縮退フォールバック(暦日差)。
        day0_date = (date.fromisoformat(depart_date) - timedelta(days=1)).isoformat()
        days_elapsed = _days_between(depart_date, anchor_date) + 1
    status = "expired" if days_elapsed >= Q5_SIGNAL_EXPIRY_DAYS else "active"
    return {"status": status, "day0_date": day0_date, "days_elapsed": days_elapsed}


# Q5「経過状態」表示(design 2026-09-18)。Q1〜Q5判定ロジック(quintile_logic.py)
# や既存のQ5シグナル(_compute_q5_signal)には一切関与しない、表示専用の追加機能。
# 「売却/除外/乗り換え」の判定ではなく、Q5 Day0以降の値動きの経過を確認する
# ためだけの情報(過去の検証で使った閾値+3%/-3%/-7.5%をそのまま使用)。
Q5_PROGRESS_BUCKETS = [
    (3.0, float("inf"), "recover", "🟢", "上昇"),
    (-3.0, 3.0, "flat", "⚪", "停滞"),
    (-7.5, -3.0, "decline", "🟠", "下落"),
    (float("-inf"), -7.5, "plunge", "🔴", "続落"),
]


def _progress_bucket(return_pct):
    for lo, hi, key, emoji, label in Q5_PROGRESS_BUCKETS:
        if lo <= return_pct < hi:
            return key, emoji, label
    return "flat", "⚪", "停滞"


def _compute_q5_progress(price_path):
    """refresh.py が積み上げるq5_price_path([{"date","price"}, ...]、
    Day0=price_path[0])から、表示用の"経過状態"を組み立てる(純粋関数、
    Redisアクセス・書き込みなし)。price_pathが空(=一度もQ5になっていない、
    または未デプロイ時点のデータ)ならNoneを返す。

    「上昇」「停滞」「下落」「続落」は、購入・売却・除外・乗り換えの判定では
    なく、Q5 Day0を基準にした累積騰落率を色分けしただけの状態表示である。"""
    if not price_path:
        return None
    day0_price = price_path[0].get("price")
    day0_date = price_path[0].get("date")
    if not day0_price:
        return None

    daily = []
    for i, entry in enumerate(price_path):
        p = entry.get("price")
        if not p:
            continue
        ret = (p / day0_price - 1) * 100
        key, emoji, label = _progress_bucket(ret)
        daily.append({
            "day": i, "date": entry.get("date"), "return_pct": round(ret, 2),
            "state_key": key, "state_emoji": emoji, "state_label": label,
        })
    if not daily:
        return None

    # 経過(状態が変化した日だけを記録、折りたたみ表示用のコンパクトな経路)
    path = []
    for d in daily:
        if not path or path[-1]["state_key"] != d["state_key"]:
            path.append(d)

    current = daily[-1]
    checkpoints = {}
    for k in (1, 3, 5, 10, 20):
        if k < len(daily):
            checkpoints[k] = daily[k]["return_pct"]

    return {
        "day0_date": day0_date,
        "days_elapsed": current["day"],
        "current_return_pct": current["return_pct"],
        "current_state_key": current["state_key"],
        "current_state_emoji": current["state_emoji"],
        "current_state_label": current["state_label"],
        "checkpoints": checkpoints,
        "path": [
            {"day": p["day"], "date": p["date"], "return_pct": p["return_pct"],
             "state_emoji": p["state_emoji"], "state_label": p["state_label"]}
            for p in path
        ],
    }


def _compute_q5_warning_view(warning):
    """refresh.pyのq5_warning(design 2026-09-18正式仕様: Q5クールのDay5時点の
    Q5起点騰落率が-7.5%以下の場合にのみ発生する注意喚起)を表示用に整形するだけ
    の純粋関数(Redisアクセスなし)。発生・保持・失効の判定はすべてrefresh.py側
    で完了しており、ここでは値の解釈・失効判定は一切行わない。売却/除外/
    購入不可などの判定には関与しない、補助的な警告表示専用。"""
    if not warning:
        return None
    return {
        "triggered_date": warning.get("triggered_date"),
        "triggered_return_pct": warning.get("triggered_return_pct"),
        "days_since_trigger": warning.get("days_since_trigger", 0),
    }


def _build_beta_view(state):
    """β(ベータ)の表示専用ビューを組み立てる(純粋関数、Redisアクセスなし)。
    state["beta"](refresh._update_quintile_stateが書き込む252営業日ローリングβ、
    quintile:state:<TICKER>の1フィールド)を読むだけで、Q1〜Q5判定
    (pred_score・percentile境界・assign_quintile)には一切関与しない。

    2026-09-25追加(ユーザー確定仕様): β<0.8低ベータ/0.8-1.3標準ベータ/
    1.3-2.5高ベータ/2.5以上超高ベータの4区分をbeta_logic.classify_betaで
    判定するだけの表示専用処理。252営業日分のデータが揃わずbeta=Noneの
    場合は「β —」「ベータ算出不可」として表示する(推測値は出さない)。
    超高ベータの場合のみ「Q1〜Q5の段階差は小さめ」という補足を追加する
    (「差なし」とは表示しない、Q1〜Q5判定の無効化・Q5除外はしない)。"""
    beta = (state or {}).get("beta")
    band_key, band_label = beta_logic.classify_beta(beta)
    return {
        "beta": round(beta, 2) if beta is not None else None,
        "beta_band": band_key,
        "beta_band_label": band_label,
        "beta_note": beta_logic.EXTREME_BETA_NOTE if band_key == "extreme" else None,
    }


def _build_quintile_view(ticker, trading_calendar=None):
    """Q1〜Q5表示用データを組み立てる。Redisの`quintile:state:<TICKER>`を
    読むだけで、Twelve Dataへのライブ呼び出しは一切行わない(design 12)。
    まだ日次バッチで一度も判定されていない銘柄は「判定待ち」として表示する。

    2026-09-22追加: refresh.py側がデータ不足(ma200_devを含む9特徴量が
    MIN_HISTORY_FOR_FULL_FEATURES件に満たない)銘柄について
    current_q=None・data_status="insufficient"・data_days/data_days_required
    を書き込むようになった(SKHYがQ1に固定表示される問題への対応)。この
    関数自体はcurrent_qが無い場合を元々「pending」として扱っていたため
    分岐構造は変えず、data_statusがあればメッセージだけをより具体的にする。"""
    state = store.get_quintile_state(ticker)
    if not state or not state.get("current_q"):
        if state and state.get("data_status") == "insufficient" and state.get("data_days_required"):
            message = (
                f"データ不足のためQ判定を保留しています"
                f"（{state.get('data_days', 0)}/{state['data_days_required']}営業日分のデータ）。"
                "必要な営業日数が蓄積され次第、通常のQ1〜Q5判定を開始します。"
            )
        else:
            message = "Q判定は次回日次更新後に反映されます。"
        return {
            "status": "pending",
            "current_q": None, "current_q_label": None,
            "previous_q": None, "last_updated": state.get("last_updated") if state else None,
            "history": [], "q5_stats": None, "q5_signal": None, "q5_progress": None,
            "q5_warning": None,
            **_build_beta_view(state),
            "message": message,
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
        **_build_beta_view(state),
        "message": None,
    }
    if current_q == "Q5":
        try:
            view["q5_stats"] = quintile_logic.load_q5_stats()
        except Exception:
            view["q5_stats"] = None
    view["q5_signal"] = _compute_q5_signal(current_q, view["history"], view["last_updated"], trading_calendar)
    view["q5_progress"] = _compute_q5_progress(state.get("q5_price_path", []))
    view["q5_warning"] = _compute_q5_warning_view(state.get("q5_warning"))
    return view


def _build_row(ticker, trading_calendar=None):
    record = store.get_ticker_record(ticker)
    freshness = _freshness(record)
    quintile_view = _build_quintile_view(ticker, trading_calendar)

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
<!-- PWA化(design 2026-09-17): 表示層の追加のみ。Q1〜Q5判定・Q5シグナル等の
     ロジック・API呼び出しには一切影響しない(/sw.jsはapi/*を明示的にキャッシュ対象外にする)。 -->
<link rel="manifest" href="/static/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Q1〜Q5状態">
<style>
*{box-sizing:border-box} body{margin:0;background:#f4f6f8;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
.wrap{max-width:880px;margin:auto;padding:18px}
@media(min-width:760px){.wrap{max-width:1120px}}
.title{font-size:24px;font-weight:800;margin-bottom:5px}
.tickercount{color:#175cd3;font-weight:700;font-size:13px;margin-bottom:4px}
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
.fresh{color:#087443;font-weight:700}.stalebadge{color:#8a6500;font-weight:700}.failbadge{color:#b42318;font-weight:700}.nonebadge{color:#98a2b3;font-weight:700}
.freshtag{font-size:11px}

/* Q1〜Q5(参照母集団内の相対的な状態、既存judgmentとは別軸の情報。
   design 13: Q3=発見・Q4=準備・Q5=購入判断。Q5→Q4を否定的な色にしない) */
.qbadge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:800;font-size:11px;white-space:nowrap}
.q-q1{background:#f2f2f2;color:#98a2b3}.q-q2{background:#eef1f5;color:#758096}
.q-q3{background:#eaf2ff;color:#175cd3}.q-q4{background:#fff1db;color:#9a6a00}
.q-q5{background:#087443;color:#fff}.q-pending{background:#f2f2f2;color:#98a2b3;font-style:italic}
/* β(ベータ)表示(design 2026-09-25、ユーザー確定仕様)。Q1〜Q5判定・
   購入判定スコアには一切関与しない、表示専用の補足情報。 */
.betaline{display:flex;align-items:baseline;gap:8px;margin:4px 0 2px;flex-wrap:wrap}
.betaval{font-weight:800;font-size:13px;color:#172033}
.betabadge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:800;font-size:11px;white-space:nowrap}
.beta-low{background:#eef1f5;color:#758096}
.beta-normal{background:#eaf2ff;color:#175cd3}
.beta-high{background:#fff1db;color:#9a6a00}
.beta-extreme{background:#fdeceb;color:#b42318}
.beta-na{background:#f2f2f2;color:#98a2b3;font-style:italic}
.betanote{color:#b42318;font-size:11px;font-weight:700;margin:0 0 6px}
.q5sig{display:flex;align-items:baseline;gap:8px;margin:4px 0 2px;flex-wrap:wrap}
.q5sig .q5sigmain{font-weight:800;font-size:13px}
.q5sig .q5sigsub{font-size:11px;font-weight:600;opacity:.85}
.q5sig-ok{color:#087443}.q5sig-active{color:#9a6a00}.q5sig-expired{color:#98a2b3}
.qdaily-wrap{overflow-x:auto;margin:8px 0;-webkit-overflow-scrolling:touch}
.qdaily-table{border-collapse:collapse;background:#f7f9fc;border-radius:13px;width:100%}
.qdaily-table th,.qdaily-table td{padding:7px 8px;text-align:center;min-width:50px;white-space:nowrap}
.qdaily-table th{font-size:10px;color:#98a2b3;font-weight:700;border-bottom:1px solid #e5e7eb}
.qdaily-table td{font-size:14px;font-weight:800;color:#172033}
.qcont{margin:6px 2px 0;font-size:13px;font-weight:800;color:#087443}
.q5stats{background:#f0f9f4;border-radius:13px;padding:10px 14px;font-size:12px;line-height:1.8;margin-top:8px}
.q5stats summary{cursor:pointer;font-weight:800;color:#087443;font-size:13px;list-style:none}
.q5stats summary::-webkit-details-marker{display:none}
.q5stats summary::before{content:"▸ "}
.q5stats[open] summary::before{content:"▾ "}
.q5stats .q5statsbody{margin-top:8px}
.q5stats .q5title{font-weight:800;color:#087443;margin-bottom:4px}
.q5stats .q5note{color:#758096;font-size:11px;margin-top:6px}

/* Q5「経過状態」表示(design 2026-09-18)。売却/除外/乗り換えの判定ではなく、
   Q5 Day0以降の値動きの経過を確認するための表示。 */
.q5prog{margin:6px 0 2px}
.q5progtop{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px}
.q5progday{font-weight:800;color:#172033}
.q5progstate{font-weight:800;padding:2px 8px;border-radius:999px;font-size:12px}
.q5progstate-recover{background:#e6f6ee;color:#087443}
.q5progstate-flat{background:#f2f2f2;color:#758096}
.q5progstate-decline{background:#fff1db;color:#9a6a00}
.q5progstate-plunge{background:#fde8e6;color:#b42318}
.q5progret{font-weight:700;font-size:12px}
.q5progret.up{color:#087443}.q5progret.down{color:#b42318}
.q5progpath{margin-top:4px;font-size:12px}
.q5progpath summary{cursor:pointer;color:#175cd3;font-weight:700;font-size:12px;list-style:none}
.q5progpath summary::-webkit-details-marker{display:none}
.q5progpath summary::before{content:"▸ "}
.q5progpath[open] summary::before{content:"▾ "}
.q5progpathbody{margin-top:6px;line-height:1.8;color:#344054}
.q5progcp{margin-top:2px;color:#344054}
.q5progroute{margin-top:2px;word-break:break-word}
.q5prognote{color:#758096;font-size:11px;margin-top:6px}

/* Q5 Day5注意喚起(design 2026-09-18正式実装)。購入不可等の判定には一切
   影響しない、補助的な警告表示のみ。将来の下落を断定する表現にはしない。 */
.q5warn{background:#fdeceb;border-radius:13px;padding:10px 14px;margin-top:6px}
.q5warntop{font-weight:800;color:#b42318;font-size:13px}
.q5warnnote{color:#8a3a30;font-size:11px;margin-top:4px;line-height:1.6}

@media(max-width:480px){.wrap{padding:12px}.title{font-size:20px}.tcard{padding:14px 16px}.tickerbig{font-size:18px}.statrow{gap:14px}.heroticker{font-size:26px}}
</style>
</head>
<body>
<div class="wrap">
<div class="title">📊 保有銘柄のQ1〜Q5状態</div>
<div id="tickerCount" class="tickercount"></div>
<div class="sub">実データ版｜最大15銘柄｜毎日サーバー側で自動更新｜各銘柄が現在Q1〜Q5のどの状態かを確認できます</div>

<div class="card">
  <div class="controls">
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()" id="addBtn">＋追加（即時取得）</button>
  </div>
  <div id="status" class="small">読み込み中…</div>
  <details class="opinfo"><summary>運用情報</summary><div id="opinfo" class="opbody"></div></details>
</div>

<div id="hero"></div>
<div class="list" id="list"></div>
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
    renderTickerCount(j);
    document.getElementById("status").textContent = extraMsg || "";
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
}

// 登録銘柄数の表示(design 2026-09-18)。/api/rankingが返すrows(=現在の
// watchlist)の件数と、既存のstock_logic.MAX_TICKERS(max_ticketsとして
// レスポンスに追加)をそのまま表示するだけの表示専用機能。Q1〜Q5判定・
// Q5経過状態・追加/削除の既存処理には一切変更を加えない。
function renderTickerCount(j){
  const el = document.getElementById("tickerCount");
  if(!el) return;
  const n = (j.rows||[]).length;
  const max = j.max_tickers ?? 15;
  el.textContent = `登録銘柄：${n}/${max}`;
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
  // 削除ボタンを押した直後に画面上から即時削除表示する(楽観的UI更新)。
  // サーバー側の削除が成功すればそのまま(updateRankingで最新状態を反映)、
  // 失敗した場合だけupdateRankingで最新状態(=まだ存在する)を再取得して
  // 元に戻す。Q1〜Q5判定ロジック・Redis保存・PWA等の既存処理には無関係、
  // /api/watchlist DELETEの呼び出し方自体は変更していない。
  const card = document.querySelector(`.tcard[data-ticker="${t}"]`);
  if(card) card.remove();
  try{
    const r=await fetch("/api/watchlist/"+encodeURIComponent(t),{method:"DELETE"});
    const j=await r.json();
    if(!j.ok){
      alert(j.error||"削除できませんでした");
      await updateRanking();
      return;
    }
    await updateRanking();
  }catch(e){
    alert("削除エラー: "+e.message);
    await updateRanking();
  }
}

function freshTagHtml(f){
  f = f||{};
  if(f.status==="fresh") return `<span class="freshtag fresh">🟢 最新（${fmtDate(f.last_trade_date)}取引分・${fmtDt(f.fetched_at)}取得）</span>`;
  const titleAttr = f.last_error_message ? ` title="${esc(f.last_error_message)}"` : "";
  if(f.status==="stale") return `<span class="freshtag stalebadge"${titleAttr}>🟡 前回データ（${esc(f.last_error_label||"エラー")}）</span>`;
  if(f.last_error_label) return `<span class="freshtag failbadge"${titleAttr}>🔴 取得失敗（${esc(f.last_error_label)}）</span>`;
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

// β(ベータ)表示(design 2026-09-25、ユーザー確定仕様)。Q1〜Q5判定ロジック・
// 購入判定スコアには一切関与しない、表示専用の補足情報。252営業日分のデータが
// 揃わずbeta===nullの場合は推測値を出さず「β —」「ベータ算出不可」と表示する。
// 超高ベータ(β>=2.5)の場合のみ「Q1〜Q5の段階差は小さめ」を追加表示する
// (「差なし」とは表示しない、Q1〜Q5判定の無効化・Q5除外は行わない)。
function betaHtml(q){
  q = q || {};
  const hasBeta = q.beta !== null && q.beta !== undefined;
  const betaText = hasBeta ? `β ${q.beta.toFixed(2)}` : "β —";
  const bandCls = q.beta_band ? `beta-${q.beta_band}` : "beta-na";
  const bandLabel = q.beta_band_label || "ベータ算出不可";
  let html = `<div class="betaline"><span class="betaval">${esc(betaText)}</span><span class="betabadge ${bandCls}">${esc(bandLabel)}</span></div>`;
  if(q.beta_note){
    html += `<div class="betanote">⚠ ${esc(q.beta_note)}</div>`;
  }
  return html;
}

// Q5シグナル(新規購入シグナル)の有効期限表示。Q1〜Q5判定ロジックには一切
// 関与しない、状態管理・表示専用(design: 2026-09-17のバックテスト検証に基づき
// 最後にQ5になった日から8日間を新規購入シグナルの有効期間とする)。
// Q5からの低下は売却シグナルではない(既存保有分の判断には使わない)。
function q5SignalHtml(q){
  if(!q || q.status !== "ready" || !q.q5_signal) return "";
  const sig = q.q5_signal;
  if(sig.status === "ok"){
    return `<div class="q5sig q5sig-ok"><span class="q5sigmain">🟢 購入OK</span><span class="q5sigsub">Q5 Day 0</span></div>`;
  }
  if(sig.status === "active"){
    return `<div class="q5sig q5sig-active"><span class="q5sigmain">前回Q5から${sig.days_elapsed}日</span><span class="q5sigsub">Q5シグナル有効</span></div>`;
  }
  return `<div class="q5sig q5sig-expired"><span class="q5sigmain">Q5シグナル失効</span></div>`;
}

// Q5「経過状態」表示(design 2026-09-18)。バックエンド(_compute_q5_progress、
// app.py)が組み立てたq5_progressをそのまま表示するだけ。Q1〜Q5判定ロジック・
// Q5シグナル(q5SignalHtml)には一切関与しない。current_qがQ5でなくなった後
// (Q4/Q3等に降格した後)でも、同じQ5サイクルの経過を追い続けるため表示する。
// 重要: これは購入/売却/除外/乗り換えの判定ではなく、Q5後の値動きの経過を
// 確認するためだけの表示。続落・下落と出ても「売却推奨」等は一切表示しない。
const Q5PROG_STATE_CLASS = {
  recover: "q5progstate-recover", flat: "q5progstate-flat",
  decline: "q5progstate-decline", plunge: "q5progstate-plunge",
};

function fmtSignedPct(v){
  if(v===null || v===undefined) return "";
  return (v>=0?"+":"") + v.toFixed(1) + "%";
}

function q5ProgressHtml(q){
  if(!q || q.status !== "ready" || !q.q5_progress) return "";
  const p = q.q5_progress;
  const stateCls = Q5PROG_STATE_CLASS[p.current_state_key] || "";
  const retCls = p.current_return_pct >= 0 ? "up" : "down";

  const cpParts = [1, 3, 5].filter(k => p.checkpoints && Object.prototype.hasOwnProperty.call(p.checkpoints, k))
    .map(k => `Day${k}: ${fmtSignedPct(p.checkpoints[k])}`);
  const cpLine = cpParts.length ? `<div class="q5progcp">${cpParts.join("　")}</div>` : "";

  const routeParts = (p.path || []).map(s => `${s.state_emoji}${esc(s.state_label)}`);
  const routeLine = routeParts.length ? routeParts.join(" → ") : "—";

  return `<div class="q5prog">
    <div class="q5progtop">
      <span class="q5progday">Q5 Day ${p.days_elapsed}</span>
      <span class="q5progstate ${stateCls}">${p.current_state_emoji} ${esc(p.current_state_label)}</span>
      <span class="q5progret ${retCls}">Q5起点 ${fmtSignedPct(p.current_return_pct)}</span>
    </div>
    <details class="q5progpath">
      <summary>経過を見る</summary>
      <div class="q5progpathbody">
        <div>Q5 Day0（${esc(fmtDate(p.day0_date))}）</div>
        ${cpLine}
        <div class="q5progroute">経過：${routeLine}</div>
        <div class="q5prognote">※これは購入・売却・除外・乗り換えの判定ではありません。Q5後の値動きの経過を確認するための表示です。</div>
      </div>
    </details>
  </div>`;
}

// Q5 Day5注意喚起(design 2026-09-18の過去データ検証結果に基づく正式実装)。
// Q5クールのDay5時点のQ5起点騰落率が-7.5%以下の場合にのみ発生し、以後は
// 現在のQ5クールの状態(新クール開始・クール②のDay5回復)とは無関係に、
// 発生から最大40営業日保持される(発生・保持・失効の判定はrefresh.py側で
// 完結済み、ここは表示専用)。購入不可等の判定には一切関与しない。
// 将来の下落を断定する表現(「〜になる確率」等)は使わない。
function q5WarningHtml(q){
  if(!q || q.status !== "ready" || !q.q5_warning) return "";
  return `<div class="q5warn">
    <div class="q5warntop">🔴 続落　⚠️ −20%程度までの下落に注意</div>
    <div class="q5warnnote">過去の類似ケースでは、Q5開始後5日目の下落が大きい局面で、その後−20%程度まで下落したケースが多く確認されています。</div>
  </div>`;
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
// 2026-09-17のUI整理で、常時表示から「📊 過去実績」ボタン(details/summary)
// クリックで展開する形式に変更。中身(数値・注意書き)は一切変更していない。
function q5StatsHtml(q){
  if(!q || q.status !== "ready" || q.current_q !== "Q5" || !q.q5_stats) return "";
  const s = q.q5_stats;
  const events = (s.events||[]).map(e=>
    `${e.within_days}日以内+${e.threshold_pct}%到達: ${e.reach_rate_pct}%(n=${e.n})`
  ).join("　");
  return `<details class="q5stats">
    <summary>📊 過去実績</summary>
    <div class="q5statsbody">
      <div class="q5title">📊 Q5該当銘柄の過去の類似状態における実績統計</div>
      <div>60日最大上昇率 中央値: ${s.h60_median_pct}%(n=${s.h60_n})　120日: ${s.h120_median_pct}%(n=${s.h120_n})</div>
      <div>${events}</div>
      <div class="q5note">※将来の予測ではなく、過去にQ5と判定された局面の統計的な実績です。${esc(s.note||"")}</div>
    </div>
  </details>`;
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

// カード表示順(表示専用)。サーバー側stock_logic.sort_rows(旧judgment基準)は
// 無変更のまま、ここでcurrent_qを基準にJS側だけで並べ替える(design 2026-09-17)。
// Array.prototype.sortは安定ソートのため、同じ優先度の銘柄同士は元の順番
// (=サーバーから届いた順)をできるだけ維持する。
const Q_SORT_PRIORITY = {"Q5":5,"Q4":4,"Q3":3,"Q2":2,"Q1":1};
function qSortPriority(x){
  const q = x.quintile;
  if(!q || q.status!=="ready" || !q.current_q) return 0; // 判定待ちは最後
  return Q_SORT_PRIORITY[q.current_q] || 0;
}

function render(rows){
  renderHero(rows);
  const sortedRows = rows.slice().sort((a,b)=>qSortPriority(b)-qSortPriority(a));
  const list=document.getElementById("list");
  list.innerHTML = sortedRows.map((x,i)=>{
    const q = x.quintile || {};
    const cardCls = q.status==="ready" ? (QBADGE[q.current_q]||"q-pending").replace("q-","cq-") : "cq-pending";
    return `<div class="tcard ${cardCls}" data-ticker="${esc(x.ticker)}">
      <div class="rankline">
        <span class="tickerbig">${esc(x.ticker)}</span>
        ${qBadgeHtml(x.quintile)}
      </div>
      ${betaHtml(x.quintile)}
      ${q5SignalHtml(x.quintile)}
      ${q5ProgressHtml(x.quintile)}
      ${q5WarningHtml(x.quintile)}
      ${qDailyBreakdownHtml(x.quintile)}
      ${q5StatsHtml(x.quintile)}

      <div class="statrow">
        <div class="stat"><div class="statlabel">株価</div><div class="statval">${fmt(x.price)}</div></div>
        <div class="stat"><div class="statlabel">前日比</div><div class="statval ${x.change_pct>0?'up':x.change_pct<0?'down':''}">${fmt(x.change_pct,"%")}</div></div>
        <div class="stat"><div class="statlabel">1ヶ月</div><div class="statval ${x.month_return>0?'up':x.month_return<0?'down':''}">${fmt(x.month_return,"%")}</div></div>
        <div class="stat"><div class="statlabel">RSI14</div><div class="statval">${fmt(x.rsi14)}</div></div>
      </div>

      <div class="cardfoot">
        ${freshTagHtml(x.freshness)}
        <button class="secondary" onclick="delTicker('${esc(x.ticker)}')">削除</button>
      </div>
    </div>`;
  }).join("");
}

updateRanking();

// PWA化(design 2026-09-17): ホーム画面追加・スタンドアロン起動のためのSW登録のみ。
// /sw.js側でAPI(/api/*)は明示的にキャッシュ対象外にしており、株価・Q1〜Q5・
// Q5シグナルの表示は従来通り毎回サーバーから取得する(挙動・API呼び出しは無変更)。
if("serviceWorker" in navigator){
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
}
</script>
</body>
</html>
"""


# PWA用Service Worker(design 2026-09-17)。/staticではなくルート直下(/sw.js)で
# 配信することで、制御範囲(scope)をアプリ全体(/)にする。
#
# キャッシュ戦略(最重要、株価・Q1〜Q5・Q5シグナルを古いキャッシュで
# 表示しないための設計):
# - /api/ 配下(watchlist・ranking・refresh等)は一切キャッシュ対象にしない。
#   fetchハンドラの先頭でパスを判定し、該当すればそのままreturnして処理せず、
#   ブラウザの通常の(Service Workerを介さない)ネットワーク取得に完全に委ねる。
# - それ以外(HTML本体・manifest・アイコン)はnetwork-first。オンライン時は
#   常にネットワークから取得し直し、取得できた場合だけキャッシュを更新する。
#   キャッシュを使うのはネットワーク取得が失敗した場合(オフライン時)のみ。
# - CACHE_NAMEにバージョン番号を含め、activateイベントで旧バージョンの
#   キャッシュを削除する。将来更新する際はこの番号を上げるだけでよい。
#
# Q1〜Q5判定ロジック・Q5シグナル・Twelve Data/Redis処理・watchlist処理・
# API予算/レート制限には一切関与しない(表示層の追加のみ)。
SW_JS = r"""
const CACHE_NAME = "q1q5-shell-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // /api/ 配下は絶対にキャッシュしない(株価・Q1〜Q5・Q5シグナルは常に最新を取得する)。
  if (url.pathname.startsWith("/api/")) {
    return;
  }
  // GET以外(POST/DELETE等)もキャッシュ対象にしない。
  if (event.request.method !== "GET") {
    return;
  }

  // HTML本体・manifest・アイコン等はnetwork-first。オンライン時は常に最新を
  // 取得し、取得できた場合だけキャッシュを更新する。取得に失敗した場合
  // (オフライン時)のみキャッシュへフォールバックする。
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const resClone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, resClone));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
"""


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/sw.js")
def service_worker():
    return app.response_class(SW_JS, mimetype="application/javascript")


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

        # 2026-09-17: Alpha Vantage完全撤去・Twelve Data一本化。追加直後にTwelve Data
        # を1回だけ取得し、その同じデータをrefresh.backfill_quintile_history_for_new_ticker
        # 内で旧指標(株価・RSI等)・Q1〜Q5の両方に使う(追加のAPI呼び出しは発生しない)。
        # これにより銘柄追加直後からpendingを経由せずready表示になる。
        fetch_note = None
        if not TD_API_KEY:
            fetch_note = "APIキー未設定のため、次回の自動更新までデータは表示されません。"
        elif not store.is_configured():
            fetch_note = "Redis未設定のため即時取得はできません。"
        else:
            try:
                result = refresh.backfill_quintile_history_for_new_ticker(ticker, TD_API_KEY)
            except Exception as e:
                result = None
                fetch_note = f"即時取得中にエラーが発生しました: {e}"

            if result is not None:
                # 2026-09-17(SKHY/AXT障害調査を受けて): backfillの戻り値が
                # {"ok","error_type","message"}になった。とくにerror_typeが
                # "INVALID_SYMBOL"の場合は「次回の自動更新を待てば直る」わけ
                # ではない(Twelve Dataがそもそも認識できないシンボルのため
                # 待っても解決しない)ので、待たせる文言にせず明確に伝える。
                # AXT→AXTIのような正式シンボルへの自動変換・自動登録は行わない
                # (ユーザー確認事項どおり、勝手な変換はしない)。
                if result["message"]:
                    fetch_note = result["message"]
                elif result["ok"]:
                    fetch_note = "最新データを取得しました。"
                elif result["error_type"] == "INVALID_SYMBOL":
                    fetch_note = (
                        f"銘柄コード「{ticker}」はTwelve Dataで認識できませんでした。"
                        "正式なティッカーシンボルをご確認のうえ、再度お試しください。"
                    )
                elif result["error_type"]:
                    label = logic.ERROR_LABELS.get(result["error_type"], result["error_type"])
                    fetch_note = f"データ取得中にエラーが発生しました（{label}）。次回の自動更新をお待ちください。"
                else:
                    fetch_note = "データを取得できませんでした。次回の自動更新をお待ちください。"

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
        # 取引日カレンダー(quintile:pool_history由来)はティッカー間で共通のため
        # ここで1回だけ取得し、_build_row→_build_quintile_view→_compute_q5_signal
        # へ使い回す(2026-09-22、Q5シグナル8取引日化に伴う追加、Redis読み込み
        # 回数を増やさないため)。
        trading_calendar = _trading_days_calendar()
        rows = [_build_row(t, trading_calendar) for t in tickers]
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

        budget_left, used = refresh.remaining_td_budget()

        return jsonify({
            "ok": True,
            "rows": rows,
            "last_refresh": last_refresh_view,
            "budget": {"used": used, "limit": td.DAILY_API_BUDGET, "remaining": budget_left},
            "max_tickers": logic.MAX_TICKERS,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"予期しないエラーが発生しました: {e}"}), 200


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "stock-buy-app",
        "max_tickers": logic.MAX_TICKERS,
        "api_key_configured": bool(TD_API_KEY),
        "redis_configured": store.is_configured(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
