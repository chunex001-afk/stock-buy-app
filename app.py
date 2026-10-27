import bisect
import os
import re
from datetime import date, datetime, timedelta, timezone

# Web Push実機テスト用マーカー(2026-10-22、コメントのみ・ロジック変更なし):
# このcommitがRenderにデプロイされた際、RENDER_GIT_COMMIT変化の検知→
# Web Push→Windowsデスクトップ通知が実際に届くかを確認するためのもの。

from flask import Flask, jsonify, request, render_template_string

import beta_logic
import comparison_logic
import market_regime_display
import push_notify
import q5_prior_mean_logic
import quintile_logic
import redis_store as store
import stock_logic as logic
import refresh
import switch_recommendation_logic
import twelvedata_client as td

app = Flask(__name__)

# Web Push通知(design 2026-10-22): Renderで新しいcommitが実際にLiveになった
# (=このプロセスが起動した)ことを検知し、Windowsデスクトップへ通知する機能。
# バックグラウンドスレッドで実行し、Flaskのリクエスト処理開始を一切ブロック
# しない。例外はpush_notify内部で握るため、ここで失敗してもアプリ起動には
# 影響しない。Q1〜Q5・Q5シグナル・BUY/SELL・Risk-off・実績比較・株価API取得
# ・GitHub Actionsには一切関与しない。
push_notify.run_deploy_check_async(store)

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


_JST = timezone(timedelta(hours=9))


def _today_jst_str():
    """日本時間(JST、UTC+9固定、DST無しのため固定オフセットで十分)での
    「今日」の日付文字列(design 2026-10-20)。_trading_days_calendar()[-1]
    (株価データの処理が追いついた最終取引日)とは別物で、株価データの取得
    タイミングに左右されない実時間の「今日」を表す。取得日の未来日判定
    (/api/portfolio/edit_entry)専用。"""
    return datetime.now(_JST).strftime("%Y-%m-%d")


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


def _trading_day_offset(calendar_dates, anchor_date, n):
    """calendar_dates(ソート済み)の中で、anchor_dateからn取引日前の日付を
    返す(n=0でanchor_date自身、2026-09-29追加: 「過去5日」表示を暦日ベース
    から取引日ベースに統一するための土台)。anchor_date自体がcalendar_dates
    に無い場合は、anchor_date以下の直近の取引日を起点として数える。
    該当する日がcalendar_dates内に無い(データ不足)場合はNoneを返す
    (架空の取引日は作らない)。"""
    idx = bisect.bisect_right(calendar_dates, anchor_date) - 1
    if idx < 0:
        return None
    target_idx = idx - n
    if target_idx < 0:
        return None
    return calendar_dates[target_idx]


def _trading_day_index(calendar_dates, date_str):
    """calendar_dates(ソート済み"YYYY-MM-DD"文字列リスト)の中でdate_strが
    何番目(0始まり)の取引日かを返す。見つからなければNone(日次アクション型
    UI(design 2026-10-16)の保有営業日数計算専用。架空の取引日は作らない)。"""
    idx = bisect.bisect_left(calendar_dates, date_str)
    if idx < len(calendar_dates) and calendar_dates[idx] == date_str:
        return idx
    return None


def _resolve_q_for_date(history, target_date):
    """historyから、target_date以前の最新のQを前方補完(carry-forward)で
    返す(2026-09-29追加: 従来JS側にあったresolveQForDateと同じロジックを
    Python側へ移設。過去のデータそのものを書き換えたり推測で作ったりはせず、
    表示上の補完のみを行う)。historyはdate昇順(_dedupe_history_by_date適用後)
    である前提。target_date以前に一件もエントリが無ければNoneを返す。"""
    result = None
    for h in history:
        if h["date"] <= target_date:
            result = h["q"]
        else:
            break
    return result


def _build_daily_breakdown(history, current_q, anchor_date, trading_calendar):
    """「今日・昨日・2〜5日前(計6日分)」のQ状態を、実際の取引日ベースで
    組み立てる(2026-09-29修正: 従来は暦日ベースの日付計算〈土日祝日も
    1日として数えてしまう〉だった不整合の修正、design: BEの監査で確認)。
    trading_calendarが空/取得できない場合、「今日」以外は算出できないため
    date/qともNoneを返す(暦日への安易なフォールバックはしない、architecture
    上「実際に存在した取引日」以外を推測で作らないため)。"""
    history = _dedupe_history_by_date(history or [])
    labels = ["今日", "昨日", "2日前", "3日前", "4日前", "5日前"]
    result = []
    for i, label in enumerate(labels):
        if i == 0:
            result.append({"label": label, "date": anchor_date, "q": current_q})
            continue
        if not trading_calendar:
            result.append({"label": label, "date": None, "q": None})
            continue
        target_date = _trading_day_offset(trading_calendar, anchor_date, i)
        q = _resolve_q_for_date(history, target_date) if target_date else None
        result.append({"label": label, "date": target_date, "q": q})
    return result


def _dedupe_history_by_date(history):
    """同じ日付のエントリが連続する場合、その日の最終状態だけを残す
    (design: 2026-09-17本番確認で発覚した不整合の修正。2026-09-29に、
    今日〜5日前テーブルの計算をJS側からPython側〈_build_daily_breakdown〉へ
    移設したことに伴い、この正規化も含めて完全にPython側へ一本化した)。
    同日中にQ1〜5バッチが複数回実行され、その都度プール境界が変わって
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


def _compute_q5_progress(price_path, current_price=None):
    """refresh.py が積み上げるq5_price_path([{"date","price"}, ...]、
    Day0=price_path[0])から、表示用の"経過状態"を組み立てる(純粋関数、
    Redisアクセス・書き込みなし)。price_pathが空(=一度もQ5になっていない、
    または未デプロイ時点のデータ)ならNoneを返す。

    「上昇」「停滞」「下落」「続落」は、購入・売却・除外・乗り換えの判定では
    なく、Q5 Day0を基準にした累積騰落率を色分けしただけの状態表示である。

    2026-10-14追加(design: AXTI等で確認されたQ5起点騰落率の凍結バグ修正)。
    q5_price_pathはDay0〜Day5(MAX_Q5_PRICE_PATH_DAYS件)に達すると
    refresh.py側でそれ以上追記されなくなる(Day5固定クール仕様、本関数の
    days_elapsed/checkpoints/pathの算出ロジックはこの仕様を前提に無変更)。
    しかし「Q5起点 +○%」(current_return_pct)はDay5以降も現在値で動き続ける
    べき表示のため、current_priceが渡された場合はprice_path[-1]の凍結値では
    なくcurrent_priceとday0_priceから常に再計算する(current_state_key/
    emoji/labelも同じ値から再判定し、数値と色の食い違いを防ぐ)。
    current_price未指定時は従来と完全に同じ結果を返す(後方互換)。"""
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

    live_return_pct = current["return_pct"]
    live_state_key = current["state_key"]
    live_state_emoji = current["state_emoji"]
    live_state_label = current["state_label"]
    if current_price:
        live_return_pct = round((current_price / day0_price - 1) * 100, 2)
        live_state_key, live_state_emoji, live_state_label = _progress_bucket(live_return_pct)

    return {
        "day0_date": day0_date,
        "days_elapsed": current["day"],
        "current_return_pct": live_return_pct,
        "current_state_key": live_state_key,
        "current_state_emoji": live_state_emoji,
        "current_state_label": live_state_label,
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


def _build_midline_halt_view(halt):
    """refresh.pyのq5_midline_halt(design 2026-10-02ユーザー確定仕様: Q5
    Day0〜Day7以内の中線〈SMA20〉割れによる「購入中断」補助表示)を表示用に
    整形するだけの純粋関数(Redisアクセスなし)。発生(Day0〜Day7以内の初回
    割れ)・解除(終値がSMA20以上を3取引日連続維持)の判定はすべてrefresh.py
    側で完結しており、ここでは値の解釈・判定は一切行わない。

    既存の購入判定・upside_score・ランキング・Q1〜Q5判定・Q5シグナル・
    「Q5勢い上位8」には一切関与しない、補助的な注意表示専用(該当銘柄を
    一覧やランキングから除外することはしない、状態が分かるように表示する
    だけ)。activeでない(=中断表示なし、または一度も割れていない)場合は
    Noneを返し、呼び出し側のJSは何も表示しない。"""
    if not halt or not halt.get("active"):
        return None
    return {
        "break_date": halt.get("break_date"),
        "recover_streak": halt.get("recover_streak", 0),
        "unlock_streak_required": halt.get("unlock_streak_required", 3),
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


def _build_quintile_view(ticker, trading_calendar=None, current_price=None):
    """Q1〜Q5表示用データを組み立てる。Redisの`quintile:state:<TICKER>`を
    読むだけで、Twelve Dataへのライブ呼び出しは一切行わない(design 12)。
    まだ日次バッチで一度も判定されていない銘柄は「判定待ち」として表示する。

    2026-10-14追加: current_priceは呼び出し元(_build_row)がすでに取得済みの
    record["price"](Redis上の最新終値、Twelve Dataへの新規呼び出しはしない)
    をそのまま渡すだけで、_compute_q5_progressの「Q5起点 +○%」ライブ再計算に
    使う。渡されない場合は従来通り(q5_price_pathの凍結値)。

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
            "q5_warning": None, "q5_midline_halt": None,
            "daily_breakdown": [], "q5_continuation_days": None,
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
    q5_progress = _compute_q5_progress(state.get("q5_price_path", []), current_price=current_price)
    # 2026-09-30監査対応: q5_signalが存在しない(一度もQ5になっていない)、
    # または失効(expired)している間はq5_progress(「Q5 Day N」表示)を出さない
    # (表示条件のみの変更。q5_progressの計算ロジック自体・q5_price_path/
    # history/q5_warningのデータはいずれも無変更。current_q==Q5(status=ok)や
    # 離脱直後でシグナルがまだactiveな間は従来通り表示する)。
    if view["q5_signal"] is None or view["q5_signal"]["status"] == "expired":
        q5_progress = None
    view["q5_progress"] = q5_progress
    view["q5_warning"] = _compute_q5_warning_view(state.get("q5_warning"))
    view["q5_midline_halt"] = _build_midline_halt_view(state.get("q5_midline_halt"))

    # 「過去5日」表示・Q5継続日数(2026-09-29修正: 暦日ベースから取引日ベースに
    # 統一)。trading_calendarが無い場合はdaily_breakdownの「今日」以外・
    # q5_continuation_daysともNoneのままとなり、フロント側は「—」として表示する
    # (暦日への安易なフォールバックはしない)。
    view["daily_breakdown"] = _build_daily_breakdown(
        view["history"], current_q, view["last_updated"], trading_calendar,
    )
    view["q5_continuation_days"] = None
    if current_q == "Q5" and view["history"] and trading_calendar:
        last_entry_date = _dedupe_history_by_date(view["history"])[-1]["date"]
        view["q5_continuation_days"] = _trading_days_between(
            trading_calendar, last_entry_date, view["last_updated"],
        )
    return view


def _build_row(ticker, trading_calendar=None):
    record = store.get_ticker_record(ticker)
    freshness = _freshness(record)
    quintile_view = _build_quintile_view(ticker, trading_calendar, current_price=(record or {}).get("price"))

    if not record or not record.get("last_trade_date"):
        return {
            "ticker": ticker,
            "price": None, "change_pct": None,
            "three_day_return": None, "week_return": None, "month_return": None,
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
        "three_day_return": record.get("three_day_return"),
        "week_return": record.get("week_return"),
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


def _compute_q5_momentum_ranking(rows):
    """「Q5勢い上位8」(design 2026-09-30、表示専用の補助ランキング。
    2026-10-05に表示件数を上位5件→上位8件に変更、判定ロジック自体は無変更。
    2026-10-06にq5_signal_status/q5_signal_days_elapsedを返り値に追加
    〈「Q5から経過日数」バッジ表示用〉、選定条件・順位付けは無変更)。

    既存のq5_signal(8取引日で失効する新規購入シグナル、_compute_q5_signal)と
    three_day_return/week_return(stock_logic.compute_indicators、latest close/
    3取引日前・5取引日前比較)をそのまま読むだけの純粋関数。Q1〜Q5判定
    (quintile_logic.py)・pred_score・購入判定・既存のランキング
    (stock_logic.sort_rows/JS側のqSortPriority)・q5_signal自体の計算には
    一切影響しない。新規のRedis書き込み・新規の日付計算も行わない。

    対象条件(ユーザー確定仕様、current_qは問わずQ1〜Q5すべて対象):
    - q5_signalが存在し、statusが"ok"(現在Q5)または"active"
      (Q5から離脱済みだが取引日8日未満)。"expired"・存在しない(None)は対象外。
    - three_day_return > 0 かつ week_return > 0(片方でも0以下・欠測なら対象外)。

    順位: momentum_score = three_day_return + week_return の降順。
    同点はthree_day_returnが高い方を上位。上位8件のみ返す(0件なら空配列、
    呼び出し側で「Q5勢い上位8」セクション自体を非表示にする)。"""
    candidates = []
    for row in rows:
        q = row.get("quintile") or {}
        if q.get("status") != "ready":
            continue
        signal = q.get("q5_signal")
        if not signal or signal.get("status") not in ("ok", "active"):
            continue
        three_day = row.get("three_day_return")
        week = row.get("week_return")
        if three_day is None or week is None:
            continue
        if not (three_day > 0 and week > 0):
            continue
        candidates.append({
            "ticker": row.get("ticker"),
            "current_q": q.get("current_q"),
            "current_q_label": q.get("current_q_label"),
            "three_day_return": three_day,
            "week_return": week,
            "momentum_score": round(three_day + week, 2),
            "q5_signal_status": signal.get("status"),
            "q5_signal_days_elapsed": signal.get("days_elapsed"),
        })
    candidates.sort(key=lambda c: (-c["momentum_score"], -c["three_day_return"]))
    return candidates[:8]


def _build_riskoff_view():
    """Risk-off判定(design 2026-10-16)の表示専用ビューを組み立てる。
    refresh.py側が書き込むriskoff:state(Redis)を読むだけの純粋関数。

    既存のQ1〜Q5判定・購入判定・売却判定・AI一斉調整判定には一切関与せず、
    ここで組み立てた値を他のロジックが参照することもない。本番には実際の
    購入・ポジション管理機能自体が存在しないため、ここでの「投資比率」は
    ユーザーへの推奨表示であり、アプリが自動的に売買・比率変更を行うことは
    ない。未計算(初回デプロイ直後等)の場合はactive=Falseの通常状態として
    表示する(非表示にはしない、常に画面最上部に状態を示す仕様のため)。"""
    state = store.get_riskoff_state()
    if not state:
        return {
            "active": False, "ratio": 1.0, "below_streak": 0, "above_streak": 0,
            "entry_days": refresh.RISKOFF_ENTRY_DAYS, "exit_days": refresh.RISKOFF_EXIT_DAYS,
            "spy_close": None, "sma200": None,
        }
    return {
        "active": bool(state.get("active")),
        "ratio": state.get("ratio", 1.0),
        "below_streak": state.get("below_streak", 0),
        "above_streak": state.get("above_streak", 0),
        "entry_days": refresh.RISKOFF_ENTRY_DAYS,
        "exit_days": refresh.RISKOFF_EXIT_DAYS,
        "spy_close": state.get("spy_close"),
        "sma200": state.get("sma200"),
    }


def _build_market_regime_view():
    """市場環境(SPY過熱度)パネル(design 2026-10-27、Candidate C研究の表示専用
    実装)の表示専用ビューを組み立てる。既存のstore.get_riskoff_state()が既に
    保持しているspy_close/sma200をそのまま再利用するだけで、新しい外部API
    取得は一切行わない。

    研究結果: Candidate C(SPY過熱時にQ5新規購入を止める、という仮の売買ルール)
    は2026年の完全ホールドアウトOOSで失敗したため、本番の自動判定(Q1〜Q5判定・
    Q5 Day0判定・BUY判定・Q5ランキング・prior_mean・Risk-off・switch
    recommendation・ポートフォリオ・資金配分のいずれ)にも一切使用せず、
    market_regime_display.build_market_regime_view()(表示専用、無変更)が
    組み立てた値をそのまま返すだけ。他のロジックはこの戻り値を参照しない。

    データ不足(refresh.py未実行直後等)の場合はNone(呼び出し側のJSはパネル
    自体を表示しない、_build_ai_market_decline_viewと同じ既存の扱い方)。"""
    state = store.get_riskoff_state()
    if not state:
        return None
    return market_regime_display.build_market_regime_view(
        state.get("spy_close"), state.get("sma200"),
    )


def _ai_market_decline_bucket(pct_down):
    """pct_down(0〜100)が該当するバックテスト参考区分を返す(design
    2026-10-23、refresh.AI_MARKET_DECLINE_BACKTEST_BUCKETSをそのまま参照する
    読み取り専用の純粋関数)。該当区分が無い場合はNone(通常は発生しない、
    0〜100を5区分で網羅済み)。"""
    for bucket in refresh.AI_MARKET_DECLINE_BACKTEST_BUCKETS:
        if bucket["lo"] <= pct_down < bucket["hi"]:
            return bucket
    return None


def _ai_market_decline_state_label(pct_down):
    """pct_down(0〜100)を客観的に説明する一行ラベルを返す(design
    2026-10-23、表示専用の純粋関数)。「買いチャンス」等の勧誘的な表現は
    使わず、下落銘柄の広がりの程度だけを事実として記述する。"""
    if pct_down < 20:
        return "AI市場の大部分は自己最高値に近い状態"
    if pct_down < 40:
        return "一部の銘柄で下落が見られる状態"
    if pct_down < 60:
        return "半数程度の銘柄で下落が見られる状態"
    if pct_down < 80:
        return "AI市場全体で調整が広がっている状態"
    return "AI市場の大部分が大きく下落している状態"


def _build_ai_market_decline_view():
    """AI市場下落状況パネル(design 2026-10-23、過去検証済み)の表示専用ビューを
    組み立てる。refresh.py側が書き込むai_market_decline:state(Redis)を読む
    だけの純粋関数。

    完全に参考情報の表示であり、Q1〜Q5判定・Q5 Day0判定・BUY/SELL判定・
    Risk-off判定・投資比率・Q5ランキング・銘柄の優先順位には一切関与しない。
    ここで組み立てた値を他のロジックが参照することも一切ない。未計算
    (初回デプロイ直後等)の場合はNoneを返し、呼び出し側のJSはパネル自体を
    表示しない。"""
    state = store.get_ai_market_decline_state()
    if not state:
        return None
    pct_down = state.get("pct_down")
    if pct_down is None:
        return None
    bucket = _ai_market_decline_bucket(pct_down)
    return {
        "n_down": state.get("n_down"),
        "n_pop": state.get("n_pop"),
        "pct_down": pct_down,
        "threshold_pct": refresh.AI_MARKET_DECLINE_THRESHOLD_PCT,
        "date": state.get("date"),
        "state_label": _ai_market_decline_state_label(pct_down),
        "backtest": bucket,
    }


# 日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
# フェーズ2)。「今日やること」をSELL→PROFIT→空き枠確定→BUYの順で判定する。
# 既存のQ1〜Q5判定・Day0判定・Risk-off判定・ランキング(sort_rows)には一切
# 変更を加えない、読み取り専用の追加レイヤー。実際の証券口座とは連携せず、
# ユーザーが確認ボタンで記録した内容(portfolio:state)だけを扱う。

HOLD_DAYS = 20          # 1銘柄あたりの基本保有営業日数(既存のバックテスト検証値、無変更)
PROFIT_THRESHOLD_PCT = 50  # +50%到達で利益確定(既存のバックテスト検証値、無変更)
MAX_POSITIONS = 6          # 最大保有銘柄数(既存のバックテスト検証値、無変更)


def _compute_profit_take_amounts(shares, entry_price, current_price):
    """+50%利益100%利確ルール(design 2026-10-18、バックテスト検証済み: 戦略Eが
    CAGR/Sharpe/Calmarいずれも最良水準かつ運用が最も単純)の$計算。「取得原本
    (shares*entry_price)は残し、含み益(現在評価額-取得原本)を100%利確する」。

    保有株数の50%を売るのではなく、現在の評価額に対して取得原本相当額を
    残すように売却株数を計算する(ユーザー確定仕様)。部分株式(小数)を許容する。

    shares(保有株数)が未入力(None)の場合は計算不能として全てNoneを返す
    (架空の株数は作らない。既存の「取得価格のみ記録する」フローとの後方互換)。

    戻り値: {"cost_basis_value","current_value","profit_value",
             "sell_shares","sell_value","remaining_shares","remaining_value"}
    """
    if not shares or not entry_price or current_price is None:
        return {"cost_basis_value": None, "current_value": None, "profit_value": None,
                "sell_shares": None, "sell_value": None, "remaining_shares": None, "remaining_value": None}
    cost_basis_value = shares * entry_price
    current_value = shares * current_price
    profit_value = max(0.0, current_value - cost_basis_value)
    sell_shares = min(profit_value / current_price, shares) if (profit_value > 0 and current_price > 0) else 0.0
    sell_value = sell_shares * current_price
    remaining_shares = shares - sell_shares
    remaining_value = remaining_shares * current_price
    return {"cost_basis_value": cost_basis_value, "current_value": current_value, "profit_value": profit_value,
            "sell_shares": sell_shares, "sell_value": sell_value,
            "remaining_shares": remaining_shares, "remaining_value": remaining_value}


def _compute_daily_actions(rows, portfolio_state, riskoff_state, anchor_date, trading_calendar,
                            ai_universe_daily_return=None):
    """「今日のアクション」をSELL→PROFIT→空き枠確定→BUYの順で判定する純粋関数。
    Redis・HTTPへのアクセスは一切行わない(呼び出し元が全て読み込んで渡す)。

    ai_universe_daily_return(design 2026-10-24、Q5乗り換え推奨機能で追加した
    引数): 当日のAI_UNIVERSE当日騰落率(%)。store.get_ai_market_decline_state()
    の"ai_universe_daily_return"キーをそのまま渡す想定。省略時(None)は
    switch_recommendation.active=Falseになるだけで、他の判定(SELL/PROFIT/
    BUY)には一切影響しない(既存の呼び出し・既存テストはそのまま動く)。

    rows: _build_rowが組み立てた監視銘柄一覧。**watchlist登録順(sort_rowsで
    並び替える前の順)で渡すこと**(同日に複数Q5 Day0が出て空き枠を超えた場合、
    この順番がBUY候補の優先順位になるため)。
    portfolio_state: store.get_portfolio_state()の戻り値(None可)。
      {"positions": {"<TICKER>": {"entry_date","entry_price","shares"(任意、
      未入力ならNone),"profit_taken","profit_taken_date"}}, "last_updated":...}
    riskoff_state: store.get_riskoff_state()の戻り値(None可)。
    anchor_date: 判定基準日("YYYY-MM-DD"、通常は最新の取引日)。
    trading_calendar: ソート済み取引日リスト(_trading_days_calendar()の戻り値)。

    戻り値: {"sell": [{"ticker","days_held","profit_eligible_too"}],
             "profit": [{"ticker","gain_pct","cost_basis_value","current_value",
               "profit_value","sell_shares","sell_value","remaining_shares",
               "remaining_value"}](+50%利益100%利確ルール、design 2026-10-18。
               sharesが未入力の銘柄は金額系が全てNone)],
             "buy": [{"ticker","suggested_price","prior_mean"(参考情報、design
               2026-10-23、q5_prior_mean_logic.get_prior_meanの戻り値そのまま。
               Q5判定・購入可否には一切関与しない表示専用の過去実績統計)}]
               (free_slots件まで), "buy_candidates_total": int,
             "free_slots": int(SELL確定後・BUY判定に使う値),
             "held_count": int(現在の実際の保有数),
             "risk_off_active": bool, "recommended_ratio": float,
             "switch_recommendation": switch_recommendation_logic.compute_switch_recommendationの
               戻り値そのまま(design 2026-10-24、表示専用。自動乗り換えは行わない)}

    状態遷移(IMPLEMENTATION_DESIGN_daily_action_ui.md 7節で確認済み):
    - 保有銘柄が再びDay0になってもBUY候補にしない(ticker in positionsで除外)
    - +50%到達後、profit_taken=trueなら二度とPROFITを出さない
    - 20営業日到達とPROFITが同日に発生した場合はSELLのみ表示する
    - Risk-offはBUY判定自体を変更しない(推奨比率の参考値を返すだけ)
    """
    positions = dict((portfolio_state or {}).get("positions") or {})

    # --- 1. SELL判定(保有営業日数 >= HOLD_DAYS) ---
    today_idx = _trading_day_index(trading_calendar, anchor_date)
    sell_list = []
    sell_tickers = set()
    for ticker, pos in positions.items():
        entry_idx = _trading_day_index(trading_calendar, pos.get("entry_date"))
        if entry_idx is None or today_idx is None:
            continue  # 判定不能(カレンダー範囲外等)。架空の値は作らない。
        days_held = today_idx - entry_idx
        if days_held >= HOLD_DAYS:
            price = (store.get_ticker_record(ticker) or {}).get("price")
            profit_eligible_too = False
            entry_price = pos.get("entry_price")
            if price is not None and entry_price:
                profit_eligible_too = (price / entry_price - 1) * 100 >= PROFIT_THRESHOLD_PCT
            sell_list.append({"ticker": ticker, "days_held": days_held,
                               "profit_eligible_too": profit_eligible_too})
            sell_tickers.add(ticker)

    # --- 2. PROFIT判定(SELL対象は除く、+50%未到達・既に利確済みも除く) ---
    profit_list = []
    for ticker, pos in positions.items():
        if ticker in sell_tickers or pos.get("profit_taken"):
            continue
        entry_price = pos.get("entry_price")
        price = (store.get_ticker_record(ticker) or {}).get("price")
        if not entry_price or price is None:
            continue
        gain_pct = (price / entry_price - 1) * 100
        if gain_pct >= PROFIT_THRESHOLD_PCT:
            amounts = _compute_profit_take_amounts(pos.get("shares"), entry_price, price)
            profit_list.append({"ticker": ticker, "gain_pct": gain_pct, **amounts})

    # --- 3. 空き枠確定(SELL確定後の保有数を基準にする、PROFITは枠を変えない) ---
    held_count = len(positions)
    free_slots = max(0, MAX_POSITIONS - (held_count - len(sell_tickers)))

    # --- 4. BUY判定(watchlist登録順、保有中銘柄とDay0以外は除外) ---
    yesterday = _trading_day_offset(trading_calendar, anchor_date, 1) if trading_calendar else None
    buy_candidates = []
    for row in rows:
        ticker = row.get("ticker")
        if ticker in positions:
            continue
        q = row.get("quintile") or {}
        if q.get("status") != "ready":
            continue
        history = q.get("history") or []
        today_q = _resolve_q_for_date(history, anchor_date)
        yesterday_q = _resolve_q_for_date(history, yesterday) if yesterday else None
        is_day0 = (today_q == "Q5") and (yesterday_q != "Q5")
        if is_day0:
            # prior_mean(design 2026-10-23、ticker-reliability研究Phase1〜3で
            # 検証済みの定義)は表示専用の参考情報であり、is_day0判定・
            # buy_candidatesへの採否・この後のbuy_list=buy_candidates[:free_slots]
            # の順序には一切影響しない(ここで追加するのは辞書の1キーのみ)。
            prior_mean = q5_prior_mean_logic.get_prior_mean(ticker, anchor_date)
            buy_candidates.append({
                "ticker": ticker, "suggested_price": row.get("price"), "prior_mean": prior_mean,
            })

    buy_list = buy_candidates[:free_slots]

    risk_off_active = bool(riskoff_state and riskoff_state.get("active"))
    recommended_ratio = (riskoff_state or {}).get("ratio", 1.0) if risk_off_active else 1.0

    # Q5乗り換え推奨(design 2026-10-24)。free_slots truncation前のbuy_candidates
    # (=既存のis_day0判定・既存のwatchlist順そのまま)を渡す。乗り換え推奨は
    # 「空き枠があるかどうか」とは無関係(既存BUY判定・buy_listの中身には
    # 一切影響しない、完全に別系統の読み取り専用判定)。
    switch_recommendation = switch_recommendation_logic.compute_switch_recommendation(
        buy_candidates, positions, ai_universe_daily_return,
    )

    return {
        "sell": sell_list,
        "profit": profit_list,
        "buy": buy_list,
        "buy_candidates_total": len(buy_candidates),
        "free_slots": free_slots,
        "held_count": held_count,
        "risk_off_active": risk_off_active,
        "recommended_ratio": recommended_ratio,
        "switch_recommendation": switch_recommendation,
    }


def _build_portfolio_view():
    """保有枠(最大6)の表示用データを組み立てる。store.get_portfolio_state()・
    store.get_ticker_record()を読むだけの表示専用関数(design 2026-10-16、
    _build_riskoff_viewと同じ「読み取り専用で組み立てるだけ」のパターン)。
    新しい価格取得・新しいQ5計算は一切行わない。各holdingのdays_heldは
    _compute_daily_actionsのSELL判定(20営業日)と完全に同じ_trading_day_index
    ベースの計算を再利用しており(design 2026-10-18、保有期間表示機能)、
    値そのものは元から計算済みだった(今回新規に追加した計算ではない)。
    hold_days(=HOLD_DAYS定数)はPF一覧側の表示用に追加しただけで、
    SELL判定ロジック自体はここでは一切行わない・変更しない。

    prior_mean(design 2026-10-24、Q5 Day0購入銘柄への参考情報表示拡張):
    既存のq5_prior_mean_logic.get_prior_mean(ticker, entry_date)をそのまま
    呼ぶだけ(entry_date=購入時のQ5 Day0日付をカットオフとして使う、新しい
    prior_mean計算ロジックは作らない)。entry_price・gain_pct等の既存の
    損益計算には一切影響しない追加フィールド。"""
    state = store.get_portfolio_state()
    positions = (state or {}).get("positions") or {}
    trading_calendar = _trading_days_calendar()
    today_idx = _trading_day_index(trading_calendar, trading_calendar[-1]) if trading_calendar else None

    holdings = []
    for ticker, pos in sorted(positions.items(), key=lambda kv: kv[1].get("entry_date") or ""):
        entry_price = pos.get("entry_price")
        price = (store.get_ticker_record(ticker) or {}).get("price")
        gain_pct = ((price / entry_price - 1) * 100) if (price is not None and entry_price) else None
        entry_idx = _trading_day_index(trading_calendar, pos.get("entry_date")) if trading_calendar else None
        days_held = (today_idx - entry_idx) if (today_idx is not None and entry_idx is not None) else None
        shares = pos.get("shares")
        current_value = (shares * price) if (shares and price is not None) else None
        entry_date = pos.get("entry_date")
        prior_mean = (q5_prior_mean_logic.get_prior_mean(ticker, entry_date) if entry_date
                      else {"status": "insufficient", "n": 0, "mean": None})
        holdings.append({
            "ticker": ticker, "entry_date": entry_date, "entry_price": entry_price,
            "current_price": price, "gain_pct": gain_pct, "days_held": days_held,
            "profit_taken": bool(pos.get("profit_taken")),
            "shares": shares, "current_value": current_value,
            "profit_take_sold_shares": pos.get("profit_take_sold_shares"),
            "profit_take_sold_value": pos.get("profit_take_sold_value"),
            "prior_mean": prior_mean,
        })

    return {"holdings": holdings, "held_count": len(positions), "max_positions": MAX_POSITIONS,
             "hold_days": HOLD_DAYS}


def _build_current_q5_prior_means(rows, anchor_date):
    """現在Q5状態にある銘柄(保有中かどうかは問わない、watchlist全体が対象)の
    prior_mean参考情報を組み立てる(design 2026-10-24)。

    「Q5になった日」ではなく、anchor_date(通常は最新の取引日)を基準日として
    q5_prior_mean_logic.get_prior_meanをそのまま呼ぶ(ユーザー確定仕様:
    「現在その銘柄に紐づいている最新のprior_mean」)。新しいQ1〜Q5判定・
    新しいprior_mean計算ロジックは一切作らない。既存のrows構造
    (row["quintile"]["status"]/["history"])と、_compute_daily_actionsが
    既に使っている_resolve_q_for_dateをそのまま再利用するだけの読み取り
    専用関数(既存のQ1〜Q5カードの表示・判定には一切接続しない、別の
    レスポンスキーとして追加するだけ)。

    戻り値: [{"ticker", "prior_mean": {"status","n","mean"},
              "prior_mean_label": str または None,
              "prior_stats": {"status","n","mean","median","win_rate",
              "max_dd_mean","max_dd_median"}}, ...]  (rowsの順序のまま、
    現在Q5の銘柄のみ)。

    prior_stats(design 2026-10-25追加): 中央値・勝率・平均最大DD・中央値最大DD
    を追加表示するための参考情報。既存のprior_meanフィールド・その計算方法
    (q5_prior_mean_logic.get_prior_mean、無変更)はそのまま。prior_statsは
    新規関数q5_prior_mean_logic.get_prior_statsを呼ぶだけで、prior_meanと
    完全に同じ母集団(同じticker・同じanchor_date)から計算される。Q1〜Q5判定・
    BUY判定・ranking・20営業日保有ルール・Risk-off・switch_recommendation_logic
    のいずれにも使われない、表示・研究専用の追加情報。

    prior_mean_label(design 2026-10-25追加、「Q5信頼度β」): prior_mean(平均)
    のみをユーザー確定のしきい値で定性ラベルに変換した表示専用文字列
    (q5_prior_mean_logic.label_prior_mean)。median/win_rate/max_dd_meanは
    ラベル判定に一切使わない(新しい合成スコアは作らない、ユーザー確定仕様)。"""
    result = []
    for row in rows:
        q = row.get("quintile") or {}
        if q.get("status") != "ready":
            continue
        history = q.get("history") or []
        current_q = _resolve_q_for_date(history, anchor_date)
        if current_q != "Q5":
            continue
        ticker = row.get("ticker")
        pm = q5_prior_mean_logic.get_prior_mean(ticker, anchor_date)
        result.append({
            "ticker": ticker,
            "prior_mean": pm,
            "prior_mean_label": q5_prior_mean_logic.label_prior_mean(pm),
            "prior_stats": q5_prior_mean_logic.get_prior_stats(ticker, anchor_date),
        })
    return result


def _build_watchlist_prior_means(tickers, anchor_date):
    """登録段階でprior_meanを確認するための、watchlist全銘柄(Q5状態を問わない)
    一覧を組み立てる(design 2026-10-25)。

    _build_current_q5_prior_means(現在Q5の銘柄のみに絞る既存関数、無変更)とは
    別の読み取り専用関数。Q1〜Q5判定・現在Qの解決(_resolve_q_for_date)には
    一切関与せず、tickersの各要素についてq5_prior_mean_logic.get_prior_mean/
    load_reference_dataをそのまま呼ぶだけ(新しいprior_mean計算ロジック・
    新しいQ5判定ロジックは一切作らない)。

    表示順はtickers(=watchlist登録順、ユーザー確定仕様:ランキングではなく
    登録順をそのまま維持する)の順序そのまま。

    戻り値: [{"ticker", "prior_mean": {"status","n","mean"},
              "prior_mean_label": str または None,
              "prior_stats": {"status","n","mean","median","win_rate",
              "max_dd_mean","max_dd_median"},
              "prior_stats_recent5y": 同じ形(直近5年だけの集計)、
              "in_reference_universe": bool(q5_prior_mean_reference.jsonの
              48銘柄に存在するか)}, ...]  (tickersと同じ順序・同じ件数)。

    prior_stats(design 2026-10-25追加)は_build_current_q5_prior_meansと同様、
    q5_prior_mean_logic.get_prior_stats(新規関数、get_prior_mean無変更)を
    呼ぶだけで、prior_meanと完全に同じ母集団から計算される表示・研究専用の
    追加情報。

    prior_mean_label(design 2026-10-26更新): このパネルだけ、prior_meanの
    勝率研究(walk-forward OOS検証済み)結果を反映し、q5_prior_mean_logic.
    label_prior_mean_with_win_rate(新規関数、prior_mean+prior_statsのwin_rate
    を組み合わせる)を使う。_build_current_q5_prior_means側(現在Q5の銘柄、
    表示自体は既に非表示化済み)は既存のlabel_prior_mean(prior_meanのみ)の
    まま無変更。

    prior_stats_recent5y(design 2026-10-26追加、「全期間×直近5年prior_mean」
    研究結果を反映): q5_prior_mean_logic.get_prior_stats_recent_years(新規
    関数)を呼ぶだけの表示・研究専用の追加情報。prior_mean_label(カテゴリー)
    の判定・表示順ソート(フロントエンドのwlpmSortedList)には一切使わない
    (直近5年データが変わってもカテゴリー・表示順は変化しない)。"""
    data = q5_prior_mean_logic.load_reference_data()
    events_by_ticker = data.get("events_by_ticker", {})
    result = []
    for ticker in tickers:
        pm = q5_prior_mean_logic.get_prior_mean(ticker, anchor_date, data=data)
        prior_stats = q5_prior_mean_logic.get_prior_stats(ticker, anchor_date, data=data)
        result.append({
            "ticker": ticker,
            "prior_mean": pm,
            "prior_mean_label": q5_prior_mean_logic.label_prior_mean_with_win_rate(pm, prior_stats),
            "prior_stats": prior_stats,
            "prior_stats_recent5y": q5_prior_mean_logic.get_prior_stats_recent_years(
                ticker, anchor_date, data=data
            ),
            "in_reference_universe": ticker in events_by_ticker,
        })
    return result


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
*{box-sizing:border-box} body{margin:0;background:#f4f6f8;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif;overflow-x:hidden}
.wrap{max-width:880px;margin:auto;padding:18px}
@media(min-width:760px){.wrap{max-width:1120px}}
.title{font-size:24px;font-weight:800;margin-bottom:5px}
.tickercount{color:#175cd3;font-weight:700;font-size:13px;margin-bottom:4px}
.sub{color:#68748a;margin-bottom:18px;font-size:13px;line-height:1.6}
.card{background:white;border-radius:22px;padding:18px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.controls{display:flex;gap:8px;flex-wrap:wrap}
.controls input{flex:1;min-width:150px;padding:13px;border:1px solid #ccd2db;border-radius:13px;font-size:16px}
.controls input#ticker-date,.controls input#ticker-price{min-width:130px}
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

/* 「Q5勢い上位8」(design 2026-09-30)。既存のq5_signal・3日間/1週間の値を
   読んで並べ替えるだけの表示専用セクション。current_q・pred_score・購入判定・
   ランキング・q5_signal自体の計算には一切関与しない。

   2026-10-06: 親の.wrap(max-width:880/1120px、margin:auto)がこのセクション
   にも適用され、PC幅で右側に大きな余白が生じていた問題への対応として、
   #q5momentumラッパーだけを.wrapの幅制約から外し、ビューポート幅いっぱいに
   広げるfull-bleed(left:50%+margin-left:-50vw)にした。他セクション(検索
   カード・銘柄一覧)の幅・.wrap自体は無変更。 */
#q5momentum{width:100vw;position:relative;left:50%;margin-left:-50vw}
.q5mom{background:#fff7ed;border:1px solid #fed7aa;border-radius:16px;padding:12px 18px;margin-bottom:18px}
.q5momtitle{font-size:15px;font-weight:900;color:#9a3412;margin-bottom:6px}
/* 各行を必ず1行に収める(design 2026-10-06追加、同日に2回再調整: 文字拡大→
   full-bleedで広がった横幅を使い切るレイアウトに変更)。flex-wrap:nowrap+
   white-space:nowrapで折り返しを禁止。左側(順位・銘柄名・鮮度バッジ)は
   詰めて配置し、右側(3日/1週の数値)は.q5momnumsのmargin-left:autoで
   行の右端に寄せることで、広がった横幅を意味のある形で使う。幅が足りない
   場合だけticker側をtext-overflow:ellipsisで省略する(選定ロジック・順位・
   スコアには無関係、表示専用)。 */
.q5momrow{display:flex;align-items:center;gap:10px;flex-wrap:nowrap;white-space:nowrap;padding:8px 0;border-top:1px solid #fed7aa;overflow:hidden}
.q5momrow:first-of-type{border-top:none}
.q5momrank{font-size:19px;font-weight:900;flex:0 0 auto}
.q5momticker{font-size:19px;font-weight:900;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.q5momq5{font-size:14px;font-weight:800;flex:0 0 auto}
.q5momnums{display:flex;gap:10px;flex:0 0 auto;margin-left:auto;padding-left:8px}
.q5momstat{font-size:13px;font-weight:700;color:#475467;flex:0 0 auto}
.q5momq5-green{color:#087443}.q5momq5-yellow{color:#9a6a00}.q5momq5-red{color:#b42318}
@media(max-width:480px){
  .q5mom{padding:10px 12px}
  .q5momrow{gap:6px;padding:7px 0}
  .q5momnums{gap:8px;padding-left:4px}
}
@media(max-width:360px){
  .q5momrank{font-size:16px}
  .q5momticker{font-size:16px}
  .q5momq5{font-size:12px}
  .q5momstat{font-size:11px}
}

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

/* 株価・RSI(現在値の補足情報、design 2026-09-29)。値動き比較用のstatrowから
   分離し、横幅を圧迫しない控えめな1行にまとめて表示する(表示専用、Q1〜Q5
   判定・購入判定・スコア・ランキングには無関係)。 */
.pricebar{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;font-size:12px;color:#758096}
.pricebar b{color:#172033;font-size:13px;font-weight:800}

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

/* Q5 Day5注意喚起(design 2026-09-18正式実装)。購入不可等の判定には一切
   影響しない、補助的な警告表示のみ。将来の下落を断定する表現にはしない。 */
.q5warn{background:#fdeceb;border-radius:13px;padding:10px 14px;margin-top:6px}
.q5warntop{font-weight:800;color:#b42318;font-size:13px}
.q5warnnote{color:#8a3a30;font-size:11px;margin-top:4px;line-height:1.6}

/* Q5中線割れ:購入中断 補助表示(design 2026-10-02ユーザー確定仕様)。
   購入不可・売却・除外等の判定には一切関与しない、状態表示専用(一覧からは
   除外しない)。Q5 Day5注意喚起(.q5warn)と同系統の配色にしているが、
   別フィールド(q5_midline_halt)・別ロジックであり、互いに独立している。 */
.q5halt{background:#fdeceb;border-radius:13px;padding:10px 14px;margin-top:6px}
.q5halttop{font-weight:800;color:#b42318;font-size:13px}
.q5haltsub{color:#8a3a30;font-size:11px;margin-top:4px;line-height:1.6}

/* Risk-off表示(design 2026-10-16)。アプリを開いた瞬間に「今は通常運用か
   Risk-offか」「投資比率をどうすべきか」が一目で分かることを最優先する、
   画面最上部固定の最重要アラート。既存のQ1〜Q5判定・購入/売却判定・
   AI一斉調整パネルには一切関与しない、表示専用。常に表示する(非アクティブ
   でも非表示にしない)。 */
#riskoff{margin-bottom:18px}
.riskoff-normal{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:#e6f6ee;border-radius:14px;padding:12px 16px;font-size:14px;color:#0a5c38}
.riskoff-normal b{font-weight:800}
.riskoff-normal .riskoff-dot{font-size:16px}
.riskoff-alert{background:#fdeceb;border:3px solid #b42318;border-radius:18px;padding:18px 20px;box-shadow:0 4px 20px #0000001a}
.riskoff-alert-title{font-size:22px;font-weight:900;color:#b42318;letter-spacing:.02em}
.riskoff-alert-desc{font-size:14px;font-weight:700;color:#8a3a30;margin-top:8px}
.riskoff-alert-cond{font-size:14px;font-weight:700;color:#172033;margin-top:10px;background:#fff;border-radius:10px;padding:8px 12px;display:inline-block}
.riskoff-ratio-label{font-size:12px;font-weight:700;color:#8a3a30;margin-top:14px}
.riskoff-ratio-value{font-size:34px;font-weight:900;color:#b42318;line-height:1.2}
.riskoff-ratio-change{font-size:13px;font-weight:700;color:#8a3a30;margin-top:2px}
.riskoff-exit{font-size:13px;font-weight:700;color:#172033;margin-top:14px;background:#fff;border-radius:10px;padding:10px 12px;line-height:1.6}
.riskoff-exit b{color:#b42318}

/* AI市場下落状況パネル(design 2026-10-23)。Risk-off表示の直下・BUY候補の
   直前に入る補助的な参考情報。「買いチャンス」等の勧誘的な表現は使わず、
   過度に目立たせないニュートラルな配色にする(Risk-offの赤・通常運用の緑
   とは区別し、注意/推奨のどちらにも見えないグレー基調にする)。 */
.aimktdecl{background:#f5f6f8;border-radius:14px;padding:14px 16px;margin-bottom:18px}
.aimktdecl-title{font-weight:800;font-size:14px;color:#475467}
.aimktdecl-headline{font-weight:800;font-size:17px;color:#172033;margin-top:6px}
.aimktdecl-sub{font-size:12.5px;color:#667085;margin-top:4px;line-height:1.5}
.aimktdecl-asof{font-size:10.5px;color:#98a2b3;margin-top:4px}
.aimktdecl-ref{margin-top:12px;padding-top:12px;border-top:1px dashed #d0d5dd}
.aimktdecl-ref-title{font-size:12.5px;font-weight:700;color:#475467}
.aimktdecl-ref-body{font-size:13px;color:#344054;margin-top:4px;line-height:1.7}
.aimktdecl-ref-body b{color:#172033}
.aimktdecl-disclaimer{font-size:10.5px;color:#98a2b3;margin-top:8px;line-height:1.5}
.aimktdecl-riskoff-note{font-size:12px;font-weight:700;color:#b42318;margin-top:10px;background:#fdeceb;border-radius:8px;padding:8px 10px;line-height:1.6}

/* 市場環境(SPY過熱度)パネル(design 2026-10-27、Candidate C研究の表示専用
   実装)。Q1〜Q5判定・BUY判定・ランキング・Risk-offには一切関与しない参考
   表示。Risk-off(赤)とは明確に区別し、「過熱=危険」に見えすぎないよう
   アンバー系の中間色を使う(通常=緑、やや高め=アンバー、過熱=オレンジ)。 */
#marketregime{margin-bottom:18px}
.mktregime{border-radius:14px;padding:12px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.mktregime-normal{background:#eefcf3;border:1px solid #bbe8cf}
.mktregime-elevated{background:#fff8e8;border:1px solid #f3dfa0}
.mktregime-overheated{background:#fff3e6;border:1px solid #f3c98a}
.mktregime-title{font-weight:800;font-size:14px;color:#172033}
.mktregime-dev{font-size:15px;font-weight:800;color:#172033}
.mktregime-note{font-size:11px;color:#8a7a5c;flex-basis:100%;line-height:1.5;margin-top:2px}
.mktregime-disclaimer{font-size:10.5px;color:#98a2b3;flex-basis:100%}

/* 今日のアクション(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
   フェーズ4)。daily_actions(SELL→PROFIT→BUYの優先順でサーバー側が計算済み)を
   そのままカードとして描画するだけ。Q1〜Q5判定・Day0判定・Risk-off判定には
   一切関与しない、表示とボタン操作のみの追加。 */
#dailyActions{margin-bottom:14px;display:flex;flex-direction:column;gap:10px}
.daction{border-radius:16px;padding:14px 16px;box-shadow:0 2px 14px #0000000c}
.daction-sell{background:#fdeceb;border:1px solid #f3c9c6}
.daction-profit{background:#fff7e6;border:1px solid #f4dba3}
.daction-buy{background:#e6f6ee;border:1px solid #b8e6cf}
.daction-none{background:#eef1f5;border:1px solid #dde1e8;color:#68748a;text-align:center;font-weight:700}
.daction-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.daction-badge{font-weight:900;font-size:13px;padding:2px 10px;border-radius:999px}
.daction-sell .daction-badge{background:#b42318;color:#fff}
.daction-profit .daction-badge{background:#9a6a00;color:#fff}
.daction-buy .daction-badge{background:#087443;color:#fff}
/* Q5乗り換え推奨(design 2026-10-24)。既存のBUY(緑)・SELL(赤)・PROFIT(黄)
   とは別系統の表示であることが分かるよう、紫系の配色にする。あくまで表示・
   提案のみで、自動売買・自動乗り換えは一切行わない。 */
.daction-switch{background:#f1ecfb;border:1px solid #d9c9f5}
.daction-switch .daction-badge{background:#6b3fa0;color:#fff}
.daction-switch-desc{font-size:12.5px;color:#475467;margin-bottom:10px}
.daction-switch-list{display:flex;flex-direction:column;gap:8px}
.daction-switch-item{background:#ffffffb3;border-radius:10px;padding:8px 10px;display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
.daction-switch-item .daction-ticker{font-size:14px}
.daction-ticker{font-weight:900;font-size:18px}
.daction-rows{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:#475467;margin-bottom:10px}
.daction-rows b{color:#172033;font-size:13.5px}
.daction-note{font-size:12px;font-weight:700;color:#9a6a00;margin-bottom:8px}
.daction-priormean{font-size:11.5px;font-weight:400;color:#8a94a6;margin-bottom:8px;cursor:help}
/* Q5信頼度β(design 2026-10-25)。prior_meanのみから決まる定性ラベル(新しい
   合成スコアではない)。prior_mean本体より少し目立つ濃い色・太字にするだけで、
   しきい値ごとの色分けはしない(絵文字で強弱を表現する設計のため)。 */
.priormean-label{font-weight:800;color:#172033}
/* 中央値・勝率・平均最大DD(design 2026-10-25)。daction-priormeanの2行目相当、
   同じ控えめな見た目(グレー系・小さめ)にする。既存のdaction-priormean自体の
   スタイルは変更しない。 */
.daction-priorstats{font-size:11px;font-weight:400;color:#98a2b3;margin-top:-4px;margin-bottom:8px;cursor:help}
.daction-row-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.daction-price-input{width:110px;padding:8px 10px;border:1px solid #ccd2db;border-radius:10px;font-size:14px}
.daction-btn{border:0;border-radius:10px;padding:9px 14px;font-weight:800;font-size:13px;cursor:pointer;color:#fff}
.daction-sell .daction-btn{background:#b42318}
.daction-profit .daction-btn{background:#9a6a00}
.daction-buy .daction-btn{background:#087443}
.daction-btn:disabled{opacity:.5;cursor:not-allowed}
.daction-msg{font-size:11.5px;margin-top:6px;min-height:14px}

/* 保有ポートフォリオ表示(design 2026-10-16フェーズ4)。 */
#portfolioStatus{margin-bottom:18px}
.pfstatus{background:white;border-radius:16px;padding:14px 16px;box-shadow:0 2px 14px #0000000c}
.pfstatus-head{font-weight:900;font-size:14px;margin-bottom:8px}
.pfstatus-row{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;padding:8px 0;border-top:1px solid #eef1f5;font-size:12.5px;color:#475467}
.pfstatus-row:first-of-type{border-top:none}
.pfstatus-ticker{font-weight:900;font-size:14px;color:#172033;min-width:52px}
.pfstatus-row b{color:#172033}
.pfstatus-gain-up{color:#087443;font-weight:800}
.pfstatus-gain-down{color:#b42318;font-weight:800}
.pfstatus-taken{font-weight:800;color:#9a6a00}
.pfstatus-empty{color:#98a2b3;font-size:12.5px}
/* 保有期間(design 2026-10-18)。1〜14営業日=通常・15〜19=注意・20以上=SELL対象。
   表示専用(既存の20営業日SELL判定ロジック自体は一切変更しない)。 */
.pfstatus-hold-normal{color:#172033;font-weight:800}
.pfstatus-hold-warn{color:#9a6a00;font-weight:800}
.pfstatus-hold-sell{color:#b42318;font-weight:900}
/* 現在Q5銘柄のprior_mean参考情報パネル(design 2026-10-24)。保有有無を
   問わない、watchlist全体の「現在Q5」一覧。既存のQ1〜Q5カードとは別の、
   読み取り専用の追加パネル。 */
#currentQ5PriorMeans{margin-bottom:18px}
.currentq5pm{background:white;border-radius:16px;padding:14px 16px;box-shadow:0 2px 14px #0000000c}
.currentq5pm-head{font-weight:900;font-size:14px;margin-bottom:8px}
.currentq5pm-row{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;padding:6px 0;border-top:1px solid #eef1f5;font-size:12.5px;color:#475467}
.currentq5pm-row:first-of-type{border-top:none}
.currentq5pm-ticker{font-weight:900;font-size:13.5px;color:#172033;min-width:52px}
.currentq5pm-empty{color:#98a2b3;font-size:12.5px}
/* 中央値・勝率・平均最大DD(design 2026-10-25)を常に独立した行にする
   (flex-basis:100%でcurrentq5pm-row内の折り返しを強制、画面幅に依存しない)。
   daction-priormean/daction-priorstats自体の既定スタイルは変更しない。 */
.currentq5pm-row .daction-priormean{flex-basis:100%;margin-bottom:0}
.currentq5pm-row .daction-priorstats{flex-basis:100%;margin-top:0;margin-bottom:0}
/* watchlist全銘柄のprior_mean一覧(design 2026-10-25)。登録段階での事前確認用、
   読み取り専用パネル。◎○✕等の最終判定しきい値はまだ無く、マイナス/データ不足/
   対象外を目立たせるだけの最小限の配色。 */
#watchlistPriorMeans{margin-bottom:18px}
.wlpm{background:white;border-radius:16px;padding:14px 16px;box-shadow:0 2px 14px #0000000c}
.wlpm-head{font-weight:900;font-size:14px;margin-bottom:2px}
.wlpm-anchor{color:#98a2b3;font-size:11.5px;margin-bottom:8px}
/* design 2026-10-26更新: 1ティッカーにつき「カテゴリー+全期間mean/win/n」
   「直近5年mean/win/n」の2行を基本とする表示(テーブルからdivベースの行に
   変更、列構成を持たないため表ではなくflexの行リストにした)。中央値・
   平均最大DDは既存情報を削除せず3行目(小さめ・淡色)として残す。 */
.wlpm-rows{display:flex;flex-direction:column}
.wlpm-row{padding:8px 2px;border-bottom:1px solid #eef1f5;font-size:12.5px}
.wlpm-row:last-child{border-bottom:none}
.wlpm-row-line1{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px;color:#475467}
.wlpm-ticker{font-weight:900;color:#172033}
.wlpm-mean-neg{color:#b42318;font-weight:800}
.wlpm-mean-pos{color:#172033;font-weight:700}
/* Q5信頼度β(design 2026-10-25)。親(wlpm-mean-neg/pos)の色をそのまま
   継承させる(マイナス=赤、プラス=濃色が自動的に揃う)。ここではfont-sizeのみ
   調整し、独自の色指定はしない。 */
.wlpm-label{font-size:11px;font-weight:700}
.wlpm-row-line2{margin-top:2px;color:#475467;font-size:11.5px}
.wlpm-row-line3{margin-top:2px;color:#98a2b3;font-size:11px}
.wlpm-flag-warn{color:#b42318;font-weight:700}
.wlpm-flag-none{color:#98a2b3}
.wlpm-empty{color:#98a2b3;font-size:12.5px}
/* 取得情報(取得単価・取得日)の訂正フォーム(design 2026-10-20)。 */
.pfedit-togglebtn{background:#eef1f5;color:#172033;border:0;border-radius:10px;padding:6px 10px;font-weight:700;font-size:11.5px;cursor:pointer}
.pfedit-form{display:flex;gap:10px;flex-wrap:wrap;align-items:end;padding:10px 0 12px;border-top:1px dashed #dde2e8}
.pfedit-label{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:#475467;font-weight:700}
.pfedit-input{padding:9px;border:1px solid #ccd2db;border-radius:10px;font-size:14px;min-width:130px}
.pfedit-actions{display:flex;gap:8px}
.pfedit-savebtn{background:#172033;color:white;border:0;border-radius:10px;padding:9px 14px;font-weight:800;font-size:13px;cursor:pointer}
.pfedit-cancelbtn{background:#eef1f5;color:#172033;border:0;border-radius:10px;padding:9px 14px;font-weight:800;font-size:13px;cursor:pointer}
.pfedit-msg{font-size:11.5px;color:#b42318;min-height:14px;width:100%}

/* 自分の実績 vs Q5 48銘柄 比較パネル(design 2026-10-21)。読み取り専用の
   表示のみで、Q1〜5判定・BUY/SELL判定・20営業日保有・+50%利確・Risk-off
   等の既存ロジックには一切関与しない。 */
.cmp-panel{background:white;border-radius:16px;padding:14px 16px;margin-bottom:16px;box-shadow:0 2px 14px #0000000c}
.cmp-head{font-weight:900;font-size:14px;margin-bottom:10px}
.cmp-table{width:100%;border-collapse:collapse;font-size:12px}
.cmp-table th,.cmp-table td{padding:7px 6px;text-align:right;border-top:1px solid #eef1f5;white-space:nowrap}
.cmp-table th:first-child,.cmp-table td:first-child{text-align:left}
.cmp-table thead th{color:#758096;font-weight:700;border-top:none}
.cmp-side-label{font-size:10px;color:#98a2b3;display:block}
.cmp-empty{color:#98a2b3}
.cmp-note{font-size:11px;color:#758096;margin-top:8px;line-height:1.6}
details.cmp-details{margin:10px 0}
details.cmp-details summary{cursor:pointer;font-size:12px;color:#475467;font-weight:700;list-style:none}
details.cmp-details summary::-webkit-details-marker{display:none}
details.cmp-details summary::before{content:"▸ "}
details.cmp-details[open] summary::before{content:"▾ "}
details.cmp-details .cmp-details-body{margin-top:6px}
details.cmp-details .cmp-note{margin-top:10px}
details.cmp-details .cmp-note:first-child{margin-top:6px}
.cmp-detail{margin-top:14px;border-top:1px dashed #dde2e8;padding-top:10px}
.cmp-detail-row{display:flex;gap:12px;flex-wrap:wrap;align-items:baseline;padding:6px 0;border-top:1px solid #eef1f5;font-size:12px;color:#475467}
.cmp-detail-row:first-of-type{border-top:none}
.cmp-detail-ticker{font-weight:900;color:#172033;min-width:52px}
.cmp-headline{margin-bottom:8px}
.cmp-headline-row{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;padding:4px 0;font-size:12.5px}
.cmp-headline-label{font-weight:900;color:#172033;min-width:118px}
.cmp-headline-item{color:#475467}
.cmp-headline-item b{color:#172033}

@media(max-width:480px){.wrap{padding:12px}.title{font-size:20px}.tcard{padding:14px 16px}.tickerbig{font-size:18px}.statrow{gap:14px}.heroticker{font-size:26px}
  .riskoff-alert{padding:14px 16px}.riskoff-alert-title{font-size:19px}.riskoff-ratio-value{font-size:28px}
  .aimktdecl{padding:12px 14px}.aimktdecl-headline{font-size:15.5px}
  .daction{padding:12px 14px}.daction-ticker{font-size:16px}.daction-price-input{width:90px}}
</style>
</head>
<body>
<div class="wrap">
<div id="riskoff"></div>
<div id="aimarketdecline"></div>
<div id="marketregime"></div>
<div id="dailyActions"></div>
<div id="portfolioStatus"></div>
<div id="currentQ5PriorMeans"></div>
<div id="comparePanel"></div>

<!-- design 2026-10-27: 「🔔更新通知・銘柄追加・運用情報」ブロックを
     「📊 保有銘柄のQ1〜Q5状態」の上に移動(表示順のみの変更、どの要素も
     中身は無変更)。Q1〜Q5判定・Day0判定・BUY判定・prior_mean・watchlist・
     switch recommendation・Risk-off・AI市場下落判定・市場過熱判定・購入
     判定・API取得処理のいずれにも一切影響しない。 -->
<div id="pushNotifyBar"></div>

<div class="card">
  <div class="controls">
    <input id="ticker" placeholder="例 NVDA" maxlength="10" onkeydown="if(event.key==='Enter')addTicker()">
    <input id="ticker-date" type="date" title="取得日(任意・省略時は本日)">
    <input id="ticker-price" type="number" step="0.01" min="0.01" placeholder="取得価格(任意)" onkeydown="if(event.key==='Enter')addTicker()">
    <button onclick="addTicker()" id="addBtn">＋追加（即時取得）</button>
  </div>
  <div class="small">取得価格を入力すると、保有銘柄として取得日・取得価格が記録されます(省略時はウォッチリストへの登録のみ)</div>
  <div id="status" class="small">読み込み中…</div>
  <details class="opinfo"><summary>運用情報</summary><div id="opinfo" class="opbody"></div></details>
</div>

<div class="title">📊 保有銘柄のQ1〜Q5状態</div>
<div id="tickerCount" class="tickercount"></div>
<div id="watchlistPriorMeans"></div>
<div class="sub">実データ版｜最大20銘柄｜毎日サーバー側で自動更新｜各銘柄が現在Q1〜Q5のどの状態かを確認できます</div>

<div id="q5momentum"></div>
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

// Risk-off表示(design 2026-10-16)。app._build_riskoff_viewが組み立てた値を
// そのまま描画するだけの表示専用関数(既存のQ1〜Q5判定・購入/売却判定には
// 一切関与しない)。常に表示する(非アクティブでもセクション自体を消さない。
// 「今は通常運用かRisk-offか」を常に一目で分かるようにする仕様のため)。
// 本番には実際の購入・ポジション管理機能が存在しないため、「投資比率」は
// ユーザーへの推奨表示。
function renderRiskOff(d){
  const el = document.getElementById("riskoff");
  if(!el) return;
  if(!d){ el.innerHTML = ""; return; }
  if(!d.active){
    el.innerHTML = `<div class="riskoff-normal">
      <span class="riskoff-dot">🟢</span><b>通常運用</b>　Risk-off：OFF　投資比率：<b>100%</b>
    </div>`;
    return;
  }
  const ratioPct = Math.round((d.ratio ?? 1.0) * 100);
  const remain = Math.max(0, d.exit_days - d.above_streak);
  el.innerHTML = `<div class="riskoff-alert">
    <div class="riskoff-alert-title">🔴 RISK-OFF</div>
    <div class="riskoff-alert-desc">市場環境がRisk-offです</div>
    <div class="riskoff-alert-cond">SPY ＜ SMA200　${esc(d.entry_days)}営業日連続</div>
    <div class="riskoff-ratio-label">推奨投資比率</div>
    <div class="riskoff-ratio-value">${esc(ratioPct)}%</div>
    <div class="riskoff-ratio-change">通常：100% → 現在：${esc(ratioPct)}%</div>
    <div class="riskoff-exit">
      🔴 Risk-off継続中　解除進捗：<b>${esc(d.above_streak)} / ${esc(d.exit_days)}営業日</b>
      ${remain > 0 ? `<br>→ あと${esc(remain)}営業日で通常運用へ` : ""}
    </div>
  </div>`;
}

// AI市場下落状況パネル(design 2026-10-23)。app._build_ai_market_decline_viewが
// 組み立てた値をそのまま描画するだけの表示専用関数。あくまで参考情報であり、
// BUY判定・Q5 Day0判定・Risk-off判定・投資比率・売却判定・利益確定判定・
// Q5ランキング・銘柄の優先順位には一切使用しない(売買結果は本パネルの追加
// 前と完全に同じ)。「買いチャンス」「上昇期待」等の勧誘的な表現は使わない。
// riskOff(j.risk_off)はRisk-off発動中かどうかの注記を出すためだけに参照する
// (このパネル自体がRisk-offの判定・投資比率を変更することはない。優先順位は
// 常に ①Risk-off → ②Q5 Day0 → ③本パネル(参考情報)の順)。
function renderAiMarketDecline(d, riskOff){
  const el = document.getElementById("aimarketdecline");
  if(!el) return;
  if(!d){ el.innerHTML = ""; return; }
  const bt = d.backtest;
  const refBlock = bt ? `<div class="aimktdecl-ref">
      <div class="aimktdecl-ref-title">📊 過去検証による参考情報</div>
      <div class="aimktdecl-ref-body">
        現在と同じ「${esc(bt.label)}」の環境でQ5 Day0が発生したケースでは<br>
        20日後平均：<b>${bt.mean_ret20>=0?"+":""}${esc(bt.mean_ret20)}%</b>　勝率：<b>${esc(bt.win_rate)}%</b>
      </div>
    </div>` : "";
  const riskOffNote = (riskOff && riskOff.active)
    ? `<div class="aimktdecl-riskoff-note">⚠️ Risk-off発動中のため、投資比率はRisk-offの表示に従ってください。</div>`
    : "";
  el.innerHTML = `<div class="aimktdecl">
    <div class="aimktdecl-title">AI市場の下落状況</div>
    <div class="aimktdecl-headline">${esc(d.threshold_pct)}%以上下落：${esc(d.n_down)} / ${esc(d.n_pop)}銘柄（${esc(d.pct_down)}%）</div>
    <div class="aimktdecl-sub">→ ${esc(d.state_label)}</div>
    <div class="aimktdecl-asof">時点：${esc(fmtDate(d.date))}</div>
    ${refBlock}
    ${riskOffNote}
    <div class="aimktdecl-disclaimer">※過去データに基づく参考値であり、今回の20日後のリターンを保証するものではありません。</div>
  </div>`;
}

// 市場環境(SPY過熱度)パネル(design 2026-10-27、Candidate C研究の表示専用
// 実装)。app._build_market_regime_viewが組み立てた値をそのまま描画するだけの
// 表示専用関数。Q1〜Q5判定・Q5 Day0判定・BUY判定・Q5ランキング・prior_mean・
// Risk-off判定・switch recommendation・ポートフォリオ・資金配分のいずれにも
// 一切関与しない(研究結果: このルールは2026年の完全ホールドアウトOOSで失敗
// したため、自動判定には採用せず参考表示のみとした)。「購入禁止」「買わない
// 方がいい」等の勧誘的・指示的な表現は使わない。
function renderMarketRegime(d){
  const el = document.getElementById("marketregime");
  if(!el) return;
  if(!d){ el.innerHTML = ""; return; }
  const devStr = (d.deviation_pct>=0?"+":"") + d.deviation_pct.toFixed(1) + "%";
  el.innerHTML = `<div class="mktregime mktregime-${esc(d.level)}">
    <div class="mktregime-title">${esc(d.emoji)} ${esc(d.label)}</div>
    <div class="mktregime-dev">SPY SMA200乖離 ${esc(devStr)}</div>
    <div class="mktregime-note">${esc(d.note)}</div>
    <div class="mktregime-disclaimer">${esc(d.disclaimer)}</div>
  </div>`;
}

// 今日のアクション(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
// フェーズ4)。app._compute_daily_actions(SELL→PROFIT→空き枠確定→BUYの順で
// サーバー側が計算済み)とapp._build_portfolio_view(保有銘柄の取得価格・現在
// 価格・損益率・保有日数)をそのまま組み合わせて描画するだけの表示専用関数。
// 判定ロジック自体はここでは一切行わない(Q1〜Q5判定・Day0判定・Risk-off判定・
// daily_actionsの計算には関与しない)。ボタンは既存のPhase3確認API
// (/api/portfolio/confirm_buy・confirm_profit・confirm_sell)を呼ぶだけで、
// 新しいAPIは追加しない。
function fmtPrice(v){
  return (v===null||v===undefined) ? "—" : Number(v).toFixed(2);
}
function fmtPctSigned(v){
  if(v===null||v===undefined) return "—";
  return (v>=0?"+":"") + v.toFixed(1) + "%";
}

async function confirmDailyAction(kind, ticker, extra){
  const btn = document.getElementById(`daction-btn-${kind}-${ticker}`);
  const msgEl = document.getElementById(`daction-msg-${kind}-${ticker}`);
  if(btn) btn.disabled = true;
  if(msgEl) msgEl.textContent = "処理中…";
  try{
    const path = kind==="buy" ? "/api/portfolio/confirm_buy"
      : kind==="profit" ? "/api/portfolio/confirm_profit"
      : "/api/portfolio/confirm_sell";
    const body = Object.assign({ticker}, extra||{});
    const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "処理に失敗しました");
    await updateRanking();
  }catch(e){
    if(msgEl) msgEl.textContent = "エラー："+e.message;
    if(btn) btn.disabled = false;
  }
}

function dactionSellCard(s, h){
  const entryPrice = h ? h.entry_price : null;
  const currentPrice = h ? h.current_price : null;
  const gainPct = h ? h.gain_pct : null;
  const suggestedExit = (currentPrice===null||currentPrice===undefined) ? "" : Number(currentPrice).toFixed(2);
  const note = s.profit_eligible_too
    ? `<div class="daction-note">※+50%にも到達済み(保有期限のためSELLを優先表示)</div>` : "";
  return `<div class="daction daction-sell">
    <div class="daction-head"><span class="daction-badge">🔴 SELL</span><span class="daction-ticker">${esc(s.ticker)}</span></div>
    <div class="daction-rows">
      <span>保有 <b>${esc(s.days_held)}営業日</b></span>
      <span>取得価格 <b>${fmtPrice(entryPrice)}</b></span>
      <span>現在価格 <b>${fmtPrice(currentPrice)}</b></span>
      <span>損益率 <b>${fmtPctSigned(gainPct)}</b></span>
    </div>
    ${note}
    <div class="daction-row-actions">
      <input class="daction-price-input" type="number" step="0.01" min="0.01"
             placeholder="売却価格" value="${esc(suggestedExit)}" id="daction-exitprice-${esc(s.ticker)}">
      <input class="daction-price-input" type="date" id="daction-exitdate-${esc(s.ticker)}" title="売却日(任意・省略時は本日)">
      <button class="daction-btn" id="daction-btn-sell-${esc(s.ticker)}"
              onclick="confirmDailyAction('sell','${esc(s.ticker)}',{exit_price: parseFloat(document.getElementById('daction-exitprice-${esc(s.ticker)}').value), exit_date: (document.getElementById('daction-exitdate-${esc(s.ticker)}').value || undefined)})">売却しました</button>
      <span class="daction-msg" id="daction-msg-sell-${esc(s.ticker)}"></span>
    </div>
  </div>`;
}

function dactionProfitCard(p, h){
  const entryPrice = h ? h.entry_price : null;
  const currentPrice = h ? h.current_price : null;
  const hasAmounts = p.cost_basis_value !== null && p.cost_basis_value !== undefined;
  const amountsHtml = hasAmounts ? `
    <div class="daction-rows">
      <span>取得原本 <b>${fmtPrice(p.cost_basis_value)}</b></span>
      <span>現在評価額 <b>${fmtPrice(p.current_value)}</b></span>
      <span>利確額(約) <b>${fmtPrice(p.sell_value)}</b></span>
      <span>利確後残す評価額(約) <b>${fmtPrice(p.remaining_value)}</b></span>
    </div>
    <div class="daction-rows">
      <span>売却株数(約) <b>${p.sell_shares!=null ? p.sell_shares.toFixed(3) : "—"}</b>株</span>
      <span>残す株数(約) <b>${p.remaining_shares!=null ? p.remaining_shares.toFixed(3) : "—"}</b>株</span>
    </div>` : `<div class="daction-note">※株数未入力のため金額は計算できません(BUY時に株数を入力すると表示されます)</div>`;
  return `<div class="daction daction-profit">
    <div class="daction-head"><span class="daction-badge">🟡 PROFIT</span><span class="daction-ticker">${esc(p.ticker)}</span></div>
    <div class="daction-note">+50%到達: 取得原本を残し、利益を100%利確(design確定仕様)</div>
    <div class="daction-rows">
      <span>取得価格 <b>${fmtPrice(entryPrice)}</b></span>
      <span>現在価格 <b>${fmtPrice(currentPrice)}</b></span>
      <span>損益率 <b>${fmtPctSigned(p.gain_pct)}</b></span>
    </div>
    ${amountsHtml}
    <div class="daction-row-actions">
      <button class="daction-btn" id="daction-btn-profit-${esc(p.ticker)}" onclick="confirmDailyAction('profit','${esc(p.ticker)}')">+50%利益100%利確を確定</button>
      <span class="daction-msg" id="daction-msg-profit-${esc(p.ticker)}"></span>
    </div>
  </div>`;
}

// 「Q5過去平均」(design 2026-10-23、ticker-reliability研究Phase1〜3で検証済みの
// prior_mean、validation_ticker_reliability_q5メモリ参照)の表示専用ヘルパー。
// あくまで参考情報であり、Q5判定・BUY判定そのものではないことが分かるように、
// BUYバッジ・ティッカー名より目立たせない控えめな見た目(.daction-priormean、
// グレー系)にする。prior_meanが未定義(古いキャッシュ応答等)の場合は何も出さない。
// label(第2引数、design 2026-10-25「Q5信頼度β」)は省略可能。省略時(既存の
// 全呼び出し箇所: BUYカード・保有銘柄パネル等)は従来通りラベル無しで、表示・
// 挙動は一切変わらない。渡された場合のみ(現在Q5パネル、下記)prior_meanの
// 直後に付記する。ラベル自体はq5_prior_mean_logic.label_prior_mean(prior_mean
// のみから決まる、median/win_rate/max_dd_meanは使わない)をそのまま表示する
// だけで、ここでは新しいしきい値判定を作らない。
function dactionPriorMeanHtml(pm, label){
  if(!pm || !pm.status) return "";
  const title = "過去のQ5 Day0後20営業日間の平均リターン(参考情報、Q5判定そのものではありません)";
  if(pm.status === "insufficient"){
    return `<div class="daction-priormean" title="${esc(title)}">Q5過去平均：データ不足${pm.n ? `(過去${pm.n}件)` : ""}</div>`;
  }
  const sign = pm.mean >= 0 ? "+" : "";
  const labelHtml = label ? ` <span class="priormean-label">${esc(label)}</span>` : "";
  return `<div class="daction-priormean" title="${esc(title)}">Q5過去平均：${sign}${pm.mean}%（n=${pm.n}）${labelHtml}</div>`;
}

// 中央値・勝率・平均最大DD(design 2026-10-25、表示・研究専用の追加統計、
// q5_prior_mean_logic.get_prior_stats)の2行目表示ヘルパー。既存の
// dactionPriorMeanHtml(1行目、prior_mean)のラベル・計算・呼び出し箇所は
// 一切変更しない、完全に独立した追加関数。prior_statsがinsufficientの場合は
// 何も出さない(prior_meanが「データ不足」表示になるのと常に同じタイミング、
// 同一母集団・同一n判定のため)。
function dactionPriorStatsHtml(stats){
  if(!stats || !stats.status || stats.status === "insufficient") return "";
  const msign = stats.median >= 0 ? "+" : "";
  const dsign = stats.max_dd_mean >= 0 ? "+" : "";
  const title = "同じQ5 Day0イベント群の中央値・勝率(ret20>0の割合)・平均最大DD(参考情報)";
  return `<div class="daction-priorstats" title="${esc(title)}">中央値 ${msign}${stats.median}%｜勝率 ${stats.win_rate}%｜平均最大DD ${dsign}${stats.max_dd_mean}%</div>`;
}

function dactionBuyCard(b){
  const suggested = (b.suggested_price===null||b.suggested_price===undefined) ? "" : Number(b.suggested_price).toFixed(2);
  return `<div class="daction daction-buy">
    <div class="daction-head"><span class="daction-badge">🟢 BUY</span><span class="daction-ticker">${esc(b.ticker)}</span></div>
    <div class="daction-note">Q5 Day0</div>
    ${dactionPriorMeanHtml(b.prior_mean)}
    <div class="daction-row-actions">
      <input class="daction-price-input" type="number" step="0.01" min="0.01"
             placeholder="取得価格" value="${esc(suggested)}" id="daction-price-${esc(b.ticker)}">
      <input class="daction-price-input" type="number" step="0.0001" min="0.0001"
             placeholder="株数(任意)" id="daction-shares-${esc(b.ticker)}">
      <button class="daction-btn" id="daction-btn-buy-${esc(b.ticker)}"
              onclick="confirmDailyAction('buy','${esc(b.ticker)}',{entry_price: parseFloat(document.getElementById('daction-price-${esc(b.ticker)}').value), shares: (document.getElementById('daction-shares-${esc(b.ticker)}').value ? parseFloat(document.getElementById('daction-shares-${esc(b.ticker)}').value) : undefined)})">購入済みにする</button>
      <span class="daction-msg" id="daction-msg-buy-${esc(b.ticker)}"></span>
    </div>
    <div class="daction-note">株数を入力すると、+50%到達時に「原本を残して利益のみ利確」する金額を自動計算できます(未入力の場合は従来どおり利確フラグのみ記録)</div>
  </div>`;
}

// Q5乗り換え推奨(design 2026-10-24)。既存BUYカードとは完全に別のカードとして
// 描画する(BUYバッジ・BUY判定・free_slots/buy_listの中身には一切影響しない)。
// 自動売買・自動乗り換えは一切行わない(ボタン・確認アクションを持たない、
// 純粋な参考表示)。
function dactionSwitchCard(sw){
  const items = (sw.candidates || []).map(c => {
    const sign = (c.prior_mean >= 0) ? "+" : "";
    return `<div class="daction-switch-item">
      <span class="daction-ticker">${esc(c.ticker)}</span>
      <span>Q5 Day0</span>
      <span>prior_mean <b>${sign}${esc(c.prior_mean)}%</b></span>
      <span>prior_n <b>${esc(c.prior_n)}</b></span>
    </div>`;
  }).join("");
  const ret = sw.ai_universe_daily_return;
  const retTxt = (ret===null||ret===undefined) ? "—" : (ret>=0?"+":"") + Number(ret).toFixed(2) + "%";
  return `<div class="daction daction-switch">
    <div class="daction-head"><span class="daction-badge">🔄 乗り換え推奨</span></div>
    <div class="daction-switch-desc">AI市場が当日${esc(retTxt)}と弱い状況（閾値${esc(sw.threshold_pct)}%以下）で、乗り換え条件を満たす新しいQ5 Day0銘柄があります。</div>
    <div class="daction-switch-list">${items}</div>
  </div>`;
}

function renderDailyActions(da, portfolio){
  const el = document.getElementById("dailyActions");
  if(!el) return;
  if(!da){ el.innerHTML = ""; return; }
  const holdingsByTicker = {};
  ((portfolio && portfolio.holdings) || []).forEach(h => { holdingsByTicker[h.ticker] = h; });

  // 表示優先順位: 乗り換え推奨(参考情報) → SELL → PROFIT → BUY
  // (daily_actions側の計算順をそのまま使う、switch_recommendationはactiveの
  // 時だけ先頭に追加するだけで、SELL/PROFIT/BUYの判定・並び順は無変更)
  const cards = [];
  if(da.switch_recommendation && da.switch_recommendation.active){
    cards.push(dactionSwitchCard(da.switch_recommendation));
  }
  (da.sell || []).forEach(s => cards.push(dactionSellCard(s, holdingsByTicker[s.ticker])));
  (da.profit || []).forEach(p => cards.push(dactionProfitCard(p, holdingsByTicker[p.ticker])));
  (da.buy || []).forEach(b => cards.push(dactionBuyCard(b)));

  el.innerHTML = cards.length
    ? cards.join("")
    : `<div class="daction daction-none">今日は売買アクションはありません</div>`;
}

// 現在Q5銘柄のprior_mean参考情報パネル(design 2026-10-24)。既存のQ1〜Q5
// カード・daily_actionsとは別の、読み取り専用の追加パネル。dactionPriorMeanHtml
// (既存のBUYカードで使っているヘルパー)をそのまま再利用し、新しい表示ロジックは
// 作らない。
function renderCurrentQ5PriorMeans(list){
  const el = document.getElementById("currentQ5PriorMeans");
  if(!el) return;
  if(!list){ el.innerHTML = ""; return; }
  // design 2026-10-26ユーザー確定: 下の「監視銘柄のQ5過去平均(登録段階
  // チェック用)」パネルと情報が重複するため、ここではprior_mean/prior_stats
  // (Q5過去平均・prior_n・中央値・勝率・平均最大DD等)の表示を削除し、
  // 「現在Q5の銘柄」自体(ticker名の一覧)だけを残す。dactionPriorMeanHtml/
  // dactionPriorStatsHtml関数・prior_mean計算ロジック・データは他の呼び出し
  // 箇所(BUYカード等)でそのまま使われているため無変更。
  const rows = list.map(item => `<div class="currentq5pm-row">
    <span class="currentq5pm-ticker">${esc(item.ticker)}</span>
  </div>`).join("");
  el.innerHTML = `<div class="currentq5pm">
    <div class="currentq5pm-head">現在Q5の銘柄</div>
    ${rows || `<div class="currentq5pm-empty">現在Q5の銘柄はありません</div>`}
  </div>`;
}

// watchlist全銘柄のprior_mean一覧(design 2026-10-25)。「登録段階でprior_meanを
// 確認する」ための読み取り専用パネル。renderCurrentQ5PriorMeans(現在Q5の銘柄のみ)
// とは別物で、Q1〜Q5判定・Q5ランキング・BUY判定には一切接続しない。表示順は
// サーバーから渡されたlist(=watchlist登録順)のまま、並べ替えは行わない。
// ◎○✕等の最終判定しきい値はまだ決めていないため、ここではマイナス/データ不足/
// 48銘柄対象外をそれぞれ目立たせるだけの最小限の表示にする。
// label(第2引数、design 2026-10-25「Q5信頼度β」→2026-10-26更新)は
// q5_prior_mean_logic.label_prior_mean_with_win_rateが算出した表示専用
// 文字列をそのまま出すだけ(ここでは一切判定し直さない)。
//
// design 2026-10-26ユーザー確定: 1ティッカーにつき基本2行で表示する。
//   1行目: カテゴリーラベル + 全期間 Q5過去平均/勝率/n
//   2行目: 直近5年 Q5過去平均/勝率/n(prior_stats_recent5y、新規追加情報。
//          カテゴリー判定・表示順には一切使わない、表示専用)
// 既存の中央値・平均最大DD(design 2026-10-25)は削除せず、3行目(小さめ・
// 淡色)として残す。
function wlpmLine1Html(pm, label, stats){
  if(!pm || !pm.status || pm.status === "insufficient"){
    return `<span class="wlpm-flag-warn">データ不足${(pm && pm.n) ? `(過去${pm.n}件)` : ""}</span>`;
  }
  const sign = pm.mean >= 0 ? "+" : "";
  const cls = pm.mean < 0 ? "wlpm-mean-neg" : "wlpm-mean-pos";
  const labelHtml = label ? `<span class="wlpm-label">${esc(label)}</span>` : "";
  const winPart = (stats && stats.status === "ok") ? `勝率${esc(stats.win_rate)}%｜` : "";
  return `${labelHtml}<span class="${cls}">Q5過去平均 ${sign}${esc(pm.mean)}%｜${winPart}n=${esc(pm.n)}</span>`;
}
function wlpmLine2Html(stats5y){
  if(!stats5y || !stats5y.status || stats5y.status === "insufficient"){
    const nPart = (stats5y && stats5y.n) ? `(過去${esc(stats5y.n)}件)` : "";
    return `<div class="wlpm-row-line2">直近5年平均：データ不足${nPart}</div>`;
  }
  const sign = stats5y.mean >= 0 ? "+" : "";
  return `<div class="wlpm-row-line2">直近5年平均 ${sign}${esc(stats5y.mean)}%｜勝率${esc(stats5y.win_rate)}%｜n=${esc(stats5y.n)}</div>`;
}
// 中央値・平均最大DD(design 2026-10-25、既存情報を削除せず3行目として残す)。
// prior_statsがinsufficientの場合(=prior_meanも常にinsufficient)は何も
// 出さない(既存表示と同じ条件)。
function wlpmLine3Html(stats){
  if(!stats || !stats.status || stats.status === "insufficient") return "";
  const msign = stats.median >= 0 ? "+" : "";
  const dsign = stats.max_dd_mean >= 0 ? "+" : "";
  return `<div class="wlpm-row-line3">中央値 ${msign}${stats.median}%｜平均最大DD ${dsign}${stats.max_dd_mean}%</div>`;
}
// 表示順(design 2026-10-26ユーザー確定): 「監視銘柄のQ5過去平均」パネルだけ、
// カテゴリー優先順位(🔥最強→🔥高期待・高リスク→🟢優良→🟡標準→🔴弱い・
// 購入優先度低)を第1キー、同カテゴリー内はprior_win_rate降順を第2キーで
// 並べる。_build_watchlist_prior_means側(登録順を維持する既存仕様・既存
// テストtest_registration_order_preserved_not_sorted_by_mean、無変更)は
// 変更せず、ここ(JS描画直前)でだけソートする表示専用の変更。ラベル判定
// ロジック(label_prior_mean_with_win_rate)・prior_mean/prior_win_rate/
// prior_nの計算方法には一切触れない。ラベルが無い(データ不足)銘柄は
// 既知の5カテゴリーより後ろに回す。
const WLPM_CATEGORY_PRIORITY = {
  "🔥 最強": 1,
  "🔥 高期待・高リスク": 2,
  "🟢 優良": 3,
  "🟡 標準": 4,
  "🔴 弱い・購入優先度低": 5,
};
function wlpmSortedList(list){
  return list.slice().sort((a, b) => {
    const pa = WLPM_CATEGORY_PRIORITY[a.prior_mean_label] || 99;
    const pb = WLPM_CATEGORY_PRIORITY[b.prior_mean_label] || 99;
    if(pa !== pb) return pa - pb;
    const wa = (a.prior_stats && a.prior_stats.status === "ok") ? a.prior_stats.win_rate : -Infinity;
    const wb = (b.prior_stats && b.prior_stats.status === "ok") ? b.prior_stats.win_rate : -Infinity;
    return wb - wa;
  });
}
function renderWatchlistPriorMeans(list, anchorDate){
  const el = document.getElementById("watchlistPriorMeans");
  if(!el) return;
  if(!list){ el.innerHTML = ""; return; }
  const rows = wlpmSortedList(list).map(item => {
    const inRef = item.in_reference_universe;
    const refFlag = inRef ? "" : `<span class="wlpm-flag-warn">対象外</span>`;
    const hasFullData = item.prior_mean && item.prior_mean.status === "ok";
    const line2 = hasFullData ? wlpmLine2Html(item.prior_stats_recent5y) : "";
    const line3 = hasFullData ? wlpmLine3Html(item.prior_stats) : "";
    return `<div class="wlpm-row">
      <div class="wlpm-row-line1">
        <span class="wlpm-ticker">${esc(item.ticker)}</span>
        ${wlpmLine1Html(item.prior_mean, item.prior_mean_label, item.prior_stats)}
        ${refFlag}
      </div>
      ${line2}
      ${line3}
    </div>`;
  }).join("");
  el.innerHTML = `<div class="wlpm">
    <div class="wlpm-head">監視銘柄のQ5過去平均(登録段階チェック用)</div>
    <div class="wlpm-anchor">基準日：${esc(fmtDate(anchorDate))}時点で計算（この銘柄が今Q5 Day0になったと仮定した場合の参考値）</div>
    ${list.length ? `<div class="wlpm-rows">${rows}</div>` : `<div class="wlpm-empty">監視銘柄が登録されていません</div>`}
  </div>`;
}

// 取得情報(取得単価・取得日)の訂正機能(design 2026-10-20)。実際の購入日/
// 購入価格が誤って登録された場合に、既存ポジションのentry_price/entry_date
// を訂正するだけの機能(新しい日付フィールドは追加しない、二重管理しない)。
// Q5判定・BUY判定・20営業日SELL判定・PROFIT判定・Risk-off等の既存ロジックは
// 一切変更しない(/api/portfolio/edit_entryがpositions[ticker]を更新する
// だけで、再計算は既存のentry_date参照ロジックがそのまま行う)。
let lastPortfolioData = null;
// 「取得情報を編集」と「売却を記録」(design 2026-10-21)は同一ticker行で
// 同時に開かない(排他制御)。どちらのパネルが開いているかを1つの変数
// {ticker, mode:"edit"|"sell"}で管理する(null = どちらも閉じている)。
let pfOpenPanel = null;

function pfTogglePanel(ticker, mode){
  pfOpenPanel = (pfOpenPanel && pfOpenPanel.ticker === ticker && pfOpenPanel.mode === mode)
    ? null : {ticker, mode};
  renderPortfolioStatus(lastPortfolioData);
}
function pfClosePanel(){
  pfOpenPanel = null;
  renderPortfolioStatus(lastPortfolioData);
}
async function savePfEntryEdit(ticker){
  const msgEl = document.getElementById(`pfedit-msg-${ticker}`);
  const priceEl = document.getElementById(`pfedit-price-${ticker}`);
  const dateEl = document.getElementById(`pfedit-date-${ticker}`);
  const body = {ticker};
  if(priceEl && priceEl.value !== "") body.entry_price = parseFloat(priceEl.value);
  if(dateEl && dateEl.value !== "") body.entry_date = dateEl.value;
  if(msgEl) msgEl.textContent = "保存中…";
  try{
    const r = await fetch("/api/portfolio/edit_entry", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "保存に失敗しました");
    pfOpenPanel = null;
    await updateRanking();
  }catch(e){
    if(msgEl) msgEl.textContent = "エラー："+e.message;
  }
}

// 任意売却を記録(design 2026-10-21)。20営業日SELLカード(dactionSellCard)と
// 同じ既存API(/api/portfolio/confirm_sell)を呼ぶだけで、新しいAPI・新しい
// バックエンドロジックは追加しない。株数は入力・送信しない(design確定仕様)。
async function saveManualSell(ticker){
  const msgEl = document.getElementById(`pfsell-msg-${ticker}`);
  const priceEl = document.getElementById(`pfsell-price-${ticker}`);
  const dateEl = document.getElementById(`pfsell-date-${ticker}`);
  const body = {ticker};
  if(priceEl && priceEl.value !== "") body.exit_price = parseFloat(priceEl.value);
  if(dateEl && dateEl.value !== "") body.exit_date = dateEl.value;
  if(msgEl) msgEl.textContent = "保存中…";
  try{
    const r = await fetch("/api/portfolio/confirm_sell", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "保存に失敗しました");
    pfOpenPanel = null;
    await updateRanking();
  }catch(e){
    if(msgEl) msgEl.textContent = "エラー："+e.message;
  }
}

function renderPortfolioStatus(portfolio){
  const el = document.getElementById("portfolioStatus");
  if(!el) return;
  lastPortfolioData = portfolio;
  if(!portfolio){ el.innerHTML = ""; return; }
  const holdDays = portfolio.hold_days;  // サーバー側HOLD_DAYS定数(表示のみ、判定はしない)
  const rows = (portfolio.holdings || []).map(h => {
    const gainCls = (h.gain_pct !== null && h.gain_pct !== undefined && h.gain_pct < 0)
      ? "pfstatus-gain-down" : "pfstatus-gain-up";
    const sharesInfo = (h.shares !== null && h.shares !== undefined)
      ? `<span>残り株数 <b>${h.shares.toFixed(3)}</b>株(評価額 ${fmtPrice(h.current_value)})</span>` : "";
    // 保有期間表示(design 2026-10-18): 既存の_compute_daily_actionsが使う
    // days_heldをそのまま流用するだけで、ここでは何も再計算・再判定しない。
    // 1〜14=通常・15〜19=注意・20以上=SELL対象、という表示上の色分けのみ。
    let holdPeriodHtml = `<span>保有期間 <b>—</b></span>`;
    if(h.days_held !== null && h.days_held !== undefined && holdDays){
      const d = h.days_held;
      const cls = d >= holdDays ? "pfstatus-hold-sell" : (d >= holdDays - 5 ? "pfstatus-hold-warn" : "pfstatus-hold-normal");
      holdPeriodHtml = `<span>保有期間 <b class="${cls}">${esc(d)}/${esc(holdDays)}営業日</b></span>`;
    }
    const isEditing = !!(pfOpenPanel && pfOpenPanel.ticker === h.ticker && pfOpenPanel.mode === "edit");
    const isSelling = !!(pfOpenPanel && pfOpenPanel.ticker === h.ticker && pfOpenPanel.mode === "sell");
    const editToggleBtn = `<button class="pfedit-togglebtn" onclick="pfTogglePanel('${esc(h.ticker)}','edit')">${isEditing ? "編集を閉じる" : "取得情報を編集"}</button>`;
    const sellToggleBtn = `<button class="pfedit-togglebtn" onclick="pfTogglePanel('${esc(h.ticker)}','sell')">${isSelling ? "売却記録を閉じる" : "売却を記録"}</button>`;
    const editForm = isEditing ? `<div class="pfedit-form">
      <label class="pfedit-label">取得単価
        <input type="number" step="0.01" min="0.01" class="pfedit-input" id="pfedit-price-${esc(h.ticker)}" value="${esc(h.entry_price != null ? h.entry_price : "")}">
      </label>
      <label class="pfedit-label">取得日
        <input type="date" class="pfedit-input" id="pfedit-date-${esc(h.ticker)}" value="${esc(h.entry_date || "")}">
      </label>
      <div class="pfedit-actions">
        <button class="pfedit-savebtn" onclick="savePfEntryEdit('${esc(h.ticker)}')">保存</button>
        <button class="pfedit-cancelbtn" onclick="pfClosePanel()">キャンセル</button>
      </div>
      <div class="pfedit-msg" id="pfedit-msg-${esc(h.ticker)}"></div>
    </div>` : "";
    const suggestedExit = (h.current_price===null||h.current_price===undefined) ? "" : Number(h.current_price).toFixed(2);
    const sellForm = isSelling ? `<div class="pfedit-form">
      <div class="small" style="width:100%">20営業日未満でも、実際に売却した場合はこちらで記録できます(保有期限の自動SELLとは別の、手動の記録です)</div>
      <label class="pfedit-label">売却価格
        <input type="number" step="0.01" min="0.01" class="pfedit-input" id="pfsell-price-${esc(h.ticker)}" value="${esc(suggestedExit)}">
      </label>
      <label class="pfedit-label">売却日(任意・省略時は本日)
        <input type="date" class="pfedit-input" id="pfsell-date-${esc(h.ticker)}">
      </label>
      <div class="pfedit-actions">
        <button class="pfedit-savebtn" onclick="saveManualSell('${esc(h.ticker)}')">保存</button>
        <button class="pfedit-cancelbtn" onclick="pfClosePanel()">キャンセル</button>
      </div>
      <div class="pfedit-msg" id="pfsell-msg-${esc(h.ticker)}"></div>
    </div>` : "";
    return `<div class="pfstatus-row">
      <span class="pfstatus-ticker">${esc(h.ticker)}</span>
      <span>取得日 <b>${esc(fmtDate(h.entry_date))}</b></span>
      <span>取得価格 <b>${fmtPrice(h.entry_price)}</b></span>
      <span>現在価格 <b>${fmtPrice(h.current_price)}</b></span>
      <span class="${gainCls}">${fmtPctSigned(h.gain_pct)}</span>
      ${holdPeriodHtml}
      ${sharesInfo}
      ${h.profit_taken ? `<span class="pfstatus-taken">PROFIT済み(原本のみ保有中)</span>` : ""}
      ${editToggleBtn}
      ${sellToggleBtn}
      ${dactionPriorMeanHtml(h.prior_mean)}
    </div>${editForm}${sellForm}`;
  }).join("");
  el.innerHTML = `<div class="pfstatus">
    <div class="pfstatus-head">保有：${esc(portfolio.held_count)} / ${esc(portfolio.max_positions)}</div>
    ${rows || `<div class="pfstatus-empty">現在保有している銘柄はありません</div>`}
  </div>`;
}

// 自分の実績 vs Q5 48銘柄 比較パネル(design 2026-10-21)。読み取り専用の
// 新規API(/api/compare/q5universe)を1回呼ぶだけで、既存のQ1〜5判定・
// BUY/SELL判定・20営業日保有・+50%利確・Risk-off・既存のupdateRanking()の
// ポーリングには一切関与しない(完全に独立したfetch)。
function fmtWinRate(v){
  return (v===null||v===undefined) ? "—" : v.toFixed(0) + "%";
}
function fmtCount(v){
  return (v===null||v===undefined) ? "—" : String(v);
}
function cmpRowHtml(label, row){
  const mine = row.mine, q5 = row.q5;
  const diffCls = (row.diff === null || row.diff === undefined) ? "" : (row.diff >= 0 ? "pfstatus-gain-up" : "pfstatus-gain-down");
  return `<tr>
    <td>${esc(label)}</td>
    <td>${fmtCount(mine.n)}</td>
    <td>${fmtPctSigned(mine.mean)}</td>
    <td>${fmtPctSigned(mine.median)}</td>
    <td>${fmtWinRate(mine.win_rate)}</td>
    <td>${fmtCount(q5.n)}</td>
    <td>${fmtPctSigned(q5.mean)}</td>
    <td>${fmtPctSigned(q5.median)}</td>
    <td>${fmtWinRate(q5.win_rate)}</td>
    <td class="${diffCls}">${fmtPctSigned(row.diff)}</td>
  </tr>`;
}
function cmpDetailRowHtml(e){
  const dayCell = (dateVal, priceVal, retVal) => dateVal
    ? `<span>${esc(fmtDate(dateVal))} ${fmtPrice(priceVal)} <b>${fmtPctSigned(retVal)}</b></span>`
    : `<span class="cmp-empty">未確定</span>`;
  return `<div class="cmp-detail-row">
    <span class="cmp-detail-ticker">${esc(e.ticker)}</span>
    <span>Day0 ${esc(fmtDate(e.day0_date))} ${fmtPrice(e.day0_price)}</span>
    ${dayCell(e.day5_date, e.day5_price, e.day5_return_pct)}
    ${dayCell(e.day20_date, e.day20_price, e.day20_return_pct)}
    ${dayCell(e.day60_date, e.day60_price, e.day60_return_pct)}
  </div>`;
}
// 「① Q5直近勢い」(design 2026-10-22改訂: 平均のみだったUI表示を、
// バックエンドで既に計算済みのmean/median/win_rate/nがすべて見える
// テーブル形式に変更。1銘柄の極端な値動きが「Q5全体の勢い」に見えて
// しまうのを防ぐため中央値も併記し、nが少ない期間は「⚠ サンプル少」を
// 添える(データ自体は除外しない、あくまで参考値として表示するだけ)。
// バックエンド(build_momentum_report)は変更しない、新しいAPI・新しい
// データ保存も作らない。
const MOMENTUM_LOW_SAMPLE_THRESHOLD = 3;
function momentumRowHtml(label, agg){
  const lowSample = agg.n > 0 && agg.n <= MOMENTUM_LOW_SAMPLE_THRESHOLD;
  const nCell = lowSample
    ? `${fmtCount(agg.n)}<div class="pfstatus-taken">⚠ サンプル少</div>`
    : fmtCount(agg.n);
  return `<tr>
    <td>${esc(label)}</td>
    <td>${fmtPctSigned(agg.mean)}</td>
    <td>${fmtPctSigned(agg.median)}</td>
    <td>${fmtWinRate(agg.win_rate)}</td>
    <td>${nCell}</td>
  </tr>`;
}
function cmpHeadlineHtml(data){
  const mom = data.momentum || {};
  const emptyAgg = {n: 0, mean: null, median: null, win_rate: null};
  const dateLabel = data.latest_price_date ? `（${esc(fmtDate(data.latest_price_date))}時点）` : "";
  const rows = ["5", "20", "60"].map(h => momentumRowHtml(`直近${h}日`, mom[h] || emptyAgg)).join("");
  return `<div class="cmp-head">① Q5直近勢い${dateLabel}</div>
  <div style="overflow-x:auto">
  <table class="cmp-table">
    <thead><tr><th>期間</th><th>平均</th><th>中央値</th><th>勝率</th><th>件数</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  </div>`;
}
// design 2026-10-22改訂: ①②それぞれの説明文(cmp-note)は通常表示では
// 邪魔になるため、「ℹ️ この比較の見方」という1つの折りたたみ(<details>)に
// まとめる。計算ロジック・APIレスポンスには一切触れない表示のみの変更。
// 既存の<details class="opinfo">/<details class="q5stats">と同じ、
// ::before擬似要素で▸/▾を出す既存パターンを踏襲する(デフォルトは閉じた
// 状態=openなし)。
function cmpGuideDetailsHtml(){
  return `<details class="cmp-details">
  <summary>ℹ️ この比較の見方</summary>
  <div class="cmp-details-body">
    <div class="cmp-note">「Q5直近勢い」は、直近にQ5 Day0になった銘柄が現在まで(20営業日を超えても)どれだけ動いているかを示す「騰落・勢い」の指標です(運用成績ではありません)。件数(n)が${MOMENTUM_LOW_SAMPLE_THRESHOLD}件以下の期間は「⚠ サンプル少」と表示します(データは除外せず参考値として表示、特定銘柄の値動きに強く依存するため平均・中央値とも鵜呑みにしないでください)。20営業日運用した場合の成績は、下の「② 実績比較」で確認できます。同じ「5日」でも②とは意味が異なります(①＝現在までの騰落、②＝20営業日運用ルールでの実績)。</div>
    <div class="cmp-note">Q5側はQ5 Day0を仮想購入日とし、20営業日未満は現在価格、20営業日以上は20営業日後の価格で固定した成績です(固定後は値動きを反映しません)。20営業日以上経過してもまだ20営業日後価格が取得できていない銘柄は、現在価格を代用せず「未確定」として集計から除外します(データが存在しないケース)。私側は、Q5 48ユニバースに含まれる銘柄のみを対象に、実際の購入日を基準として、売却済みは20営業日以内の実売却価格、保有中は20営業日未満の現在価格を使います。20営業日を超えてからの売却・20営業日以上の保有は、実データ自体はあってもQ5側と比較条件が合わないため「比較対象外」として除外します(未確定とは異なり、データ不存在ではなく比較条件不一致が理由です)。いずれも対象件数・未確定/比較対象外件数は件数欄に表示します。5/20/60/120/180営業日は保有期間ではなく、直近どの期間に発生したDay0・購入かという観測ウィンドウです(まだ20営業日経過していない銘柄も対象に含みます)。株数・資金額・NAV等は使用していません。上の「① Q5直近勢い」と同じ「5日」等の数字でも、①は現在までの騰落、②は20営業日運用ルールでの実績比較という別の意味です。</div>
  </div>
  </details>`;
}
// 【保有確定】【現在価格】の2ブロック比較(design 2026-10-21確定仕様)。
// Q5側はQ5 Day0起算(既存のrecent_window=確定成績・momentum=直近勢いを
// そのまま再利用、新しい計算はしない)、私側は実際の購入日起算(意図的な
// 違い)。既存のcmpHeadlineHtml・既存の詳細テーブルには一切影響しない。
function cmpBlockRowHtml(label, q5Agg, mineAgg, extraHtml){
  const diff = (q5Agg.mean != null && mineAgg.mean != null) ? (mineAgg.mean - q5Agg.mean) : null;
  const diffCls = (diff === null) ? "" : (diff >= 0 ? "pfstatus-gain-up" : "pfstatus-gain-down");
  return `<tr>
    <td>${esc(label)}</td>
    <td>${fmtPctSigned(q5Agg.mean)}<span class="cmp-side-label">(n=${q5Agg.n})</span></td>
    <td>${fmtPctSigned(mineAgg.mean)}<span class="cmp-side-label">(n=${mineAgg.n})</span>${extraHtml || ""}</td>
    <td class="${diffCls}">${fmtPctSigned(diff)}</td>
  </tr>`;
}
function cmpConfirmedBlockHtml(data){
  const rw = data.recent_window || {};
  const mc = data.my_confirmed || {};
  const emptyAgg = {n: 0, mean: null};
  const rows = ["5", "20", "60"].map(h =>
    cmpBlockRowHtml(`${h}日`, (rw[h] || {}).q5 || emptyAgg, mc[h] || emptyAgg)
  ).join("");
  return `<div class="pfstatus-head">【保有確定】</div>
  <div style="overflow-x:auto">
  <table class="cmp-table">
    <thead><tr><th>期間</th><th>Q5 48仮想</th><th>私の実績</th><th>差</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  </div>
  <div class="cmp-note">Q5側はDay0から5/20/60営業日後の確定リターン(既存の「Q5確定成績」と同じ値、Day0起算)。私側は実際の購入日から5/20/60営業日以上経過した取引(売却済みは実際の売却価格、保有中はまだ経過済みの場合のみ現在価格を暫定値として使用、購入日起算)。</div>`;
}
function cmpCurrentPriceBlockHtml(data){
  const mom = data.momentum || {};
  const mcp = data.my_current_price || {};
  const emptyAgg = {n: 0, mean: null};
  const rows = ["5", "20", "60"].map(h => {
    const mine = (mcp[h] || {}).open || emptyAgg;
    const sold = (mcp[h] || {}).sold || emptyAgg;
    const soldHtml = sold.n > 0
      ? `<div class="cmp-side-label">参考(直近購入で既に売却済み) ${fmtPctSigned(sold.mean)}(n=${sold.n})</div>` : "";
    return cmpBlockRowHtml(`${h}日`, mom[h] || emptyAgg, mine, soldHtml);
  }).join("");
  return `<div class="pfstatus-head">【現在価格】</div>
  <div style="overflow-x:auto">
  <table class="cmp-table">
    <thead><tr><th>期間</th><th>Q5 48仮想</th><th>私の実績(保有中)</th><th>差</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  </div>
  <div class="cmp-note">Q5側は直近5/20/60営業日以内のQ5 Day0(未経過含む、既存の「Q5直近勢い」と同じ値、Day0起算)。私側は直近5/20/60営業日以内に実際に購入し、まだ保有中のものを取得価格→現在価格でmark-to-market評価(購入日起算)。直近購入で既に売却済みのものは、別途参考値として表示します。</div>`;
}
// 「③実運用成績」(design 2026-10-21最終確定仕様)。Q5 48銘柄を「Day0→
// 原則20営業日保有→20営業日後に仮想売却」という既存運用ルールと同じ
// 基準で評価した成績と、私が実際に購入した銘柄群の成績を、5/20/60/120/
// 180営業日の観測期間ごとに比較する。株数・資金額・NAV等は一切表示しない。
function actualPerfRowHtml(label, row){
  const q5 = row.q5, mine = row.mine;
  const diffCls = (row.mean_diff === null || row.mean_diff === undefined) ? "" : (row.mean_diff >= 0 ? "pfstatus-gain-up" : "pfstatus-gain-down");
  const subDiff = `<div class="cmp-side-label">中央値差 ${fmtPctSigned(row.median_diff)} / 勝率差 ${row.win_rate_diff==null?"—":(row.win_rate_diff>=0?"+":"")+row.win_rate_diff.toFixed(0)+"pt"}</div>`;
  // Q5側の件数セルには対象/未確定の内訳を添える(未確定=20営業日以上経過
  // したのにday20_priceが無く、現在価格を代用せず集計から除外した件数。
  // データが存在しないケース)。
  const q5Unconfirmed = q5.unconfirmed || 0;
  const q5CountHtml = q5Unconfirmed > 0
    ? `${fmtCount(q5.n)}<div class="cmp-side-label">対象${fmtCount(q5.total)}・未確定${fmtCount(q5Unconfirmed)}</div>`
    : fmtCount(q5.n);
  // 私側の件数セルには対象/比較対象外の内訳を添える(比較対象外=主に
  // 20営業日を超えた売却/保有で、実データは存在するがQ5側と比較条件が
  // 合わないため除外した件数。Q5側の「未確定」=データ不存在とは意味が
  // 異なるため、あえて別の呼び名にしている)。
  const mineExcluded = mine.excluded || 0;
  const mineCountHtml = mineExcluded > 0
    ? `${fmtCount(mine.n)}<div class="cmp-side-label">対象${fmtCount(mine.total)}・比較対象外${fmtCount(mineExcluded)}</div>`
    : fmtCount(mine.n);
  return `<tr>
    <td>${esc(label)}</td>
    <td>${q5CountHtml}</td><td>${fmtPctSigned(q5.mean)}</td><td>${fmtPctSigned(q5.median)}</td><td>${fmtWinRate(q5.win_rate)}</td>
    <td>${mineCountHtml}</td><td>${fmtPctSigned(mine.mean)}</td><td>${fmtPctSigned(mine.median)}</td><td>${fmtWinRate(mine.win_rate)}</td>
    <td class="${diffCls}">${fmtPctSigned(row.mean_diff)}${subDiff}</td>
  </tr>`;
}
function actualPerformanceBlockHtml(data){
  const ap = data.actual_performance || {};
  const emptyQ5Agg = {n: 0, mean: null, median: null, win_rate: null, total: 0, unconfirmed: 0};
  const emptyMineAgg = {n: 0, mean: null, median: null, win_rate: null, total: 0, excluded: 0};
  const emptyRow = {q5: emptyQ5Agg, mine: emptyMineAgg, mean_diff: null, median_diff: null, win_rate_diff: null};
  const rows = ["5", "20", "60", "120", "180"].map(h =>
    actualPerfRowHtml(`直近${h}営業日`, ap[h] || emptyRow)
  ).join("");
  return `<div class="cmp-head">② 実績比較</div>
  <div class="cmp-note" style="margin-top:0">Q5 48銘柄を20営業日運用した場合 vs 私の実運用</div>
  <div style="overflow-x:auto">
  <table class="cmp-table">
    <thead><tr>
      <th>期間</th>
      <th colspan="4">Q5 48（Day0→20営業日運用）</th>
      <th colspan="4">私の実運用</th>
      <th>差（私−Q5）</th>
    </tr><tr>
      <th></th>
      <th>件数</th><th>平均</th><th>中央値</th><th>勝率</th>
      <th>件数</th><th>平均</th><th>中央値</th><th>勝率</th>
      <th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
  </div>`;
}
// design 2026-10-22改訂: 比較画面を「① Q5直近勢い」「② 実績比較」の2本
// 構成に整理。旧「Q5確定成績」見出し・【保有確定】【現在価格】ブロック・
// 私 vs Q5 Day0 48銘柄の詳細テーブルは、今回の画面では表示しない(各JS
// 関数・バックエンドのロジック/データ自体は削除せず残してある)。
function renderComparison(data){
  const el = document.getElementById("comparePanel");
  if(!el) return;
  if(!data){ el.innerHTML = ""; return; }
  el.innerHTML = `<div class="cmp-panel">
    <div class="cmp-head">📈 実績比較：私 vs Q5</div>
    ${cmpHeadlineHtml(data)}
    ${cmpGuideDetailsHtml()}
    ${actualPerformanceBlockHtml(data)}
  </div>`;
}
async function loadComparison(){
  try{
    const r = await fetch("/api/compare/q5universe");
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "取得失敗");
    renderComparison(j);
  }catch(e){
    const el = document.getElementById("comparePanel");
    if(el) el.innerHTML = `<div class="cmp-panel cmp-empty">実績比較の取得に失敗しました: ${esc(e.message)}</div>`;
  }
}

async function updateRanking(extraMsg){
  document.getElementById("status").textContent = extraMsg || "読み込み中…";
  try{
    const r=await fetch("/api/ranking");
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"取得失敗");
    render(j.rows);
    renderRiskOff(j.risk_off);
    renderAiMarketDecline(j.ai_market_decline, j.risk_off);
    renderMarketRegime(j.market_regime);
    renderDailyActions(j.daily_actions, j.portfolio);
    renderPortfolioStatus(j.portfolio);
    // design 2026-10-26ユーザー確定: 「現在Q5の銘柄」セクション(見出し・
    // ticker一覧)自体を非表示にする(下の「監視銘柄のQ5過去平均」と重複
    // するため)。renderCurrentQ5PriorMeans関数・current_q5_prior_means
    // データ・算出ロジックは一切変更せず残してある(呼び出しを止めるだけの
    // 表示専用の変更)。
    // renderCurrentQ5PriorMeans(j.current_q5_prior_means);
    renderWatchlistPriorMeans(j.watchlist_prior_means, j.watchlist_prior_means_anchor_date);
    renderQ5Momentum(j.q5_momentum);
    renderOpInfo(j);
    renderTickerCount(j);
    document.getElementById("status").textContent = extraMsg || "";
  }catch(e){document.getElementById("status").textContent="エラー："+e.message}
}

// 「Q5勢い上位8」(design 2026-09-30)。サーバー側app._compute_q5_momentum_
// rankingが組み立てた配列(既に条件判定・並べ替え・上位8件絞り込み済み)を
// そのまま描画するだけの表示専用関数。JS側でのフィルタ・ソートは行わない
// (current_q・q5_signal・3日間/1週間の判定ロジック自体はPython側の純粋関数
// に一本化し、テスト可能にするため)。0件の場合はセクション自体を非表示にする。
const Q5MOM_MEDALS = ["🥇","🥈","🥉"];
// Q5経過日数バッジ(design 2026-10-06)。既存のq5_signal.status/days_elapsed
// (app._compute_q5_momentum_rankingがそのままコピーしたq5_signal_status/
// q5_signal_days_elapsed)を見て表示を分けるだけの表示専用ロジック。
// 選定条件・順位付け・momentum_scoreには一切関与しない。
function q5MomBadge(status, daysElapsed){
  if(status === "ok") return {label:"Q5継続中", cls:"q5momq5-green"};
  if(daysElapsed <= 2) return {label:`Q5から${daysElapsed}日`, cls:"q5momq5-green"};
  if(daysElapsed <= 6) return {label:`Q5から${daysElapsed}日`, cls:"q5momq5-yellow"};
  return {label:`Q5から${daysElapsed}日`, cls:"q5momq5-red"};
}
function renderQ5Momentum(list){
  const el = document.getElementById("q5momentum");
  if(!el) return;
  if(!list || !list.length){ el.innerHTML = ""; return; }
  const rows = list.map((m,i)=>{
    const rank = Q5MOM_MEDALS[i] || String(i+1);
    const badge = q5MomBadge(m.q5_signal_status, m.q5_signal_days_elapsed);
    return `<div class="q5momrow">
      <span class="q5momrank">${rank}</span>
      <span class="q5momticker">${esc(m.ticker)}</span>
      <span class="q5momq5 ${badge.cls}">${badge.label}</span>
      <span class="q5momnums">
        <span class="q5momstat">3日${fmt(m.three_day_return,"%")}</span>
        <span class="q5momstat">1週${fmt(m.week_return,"%")}</span>
      </span>
    </div>`;
  }).join("");
  el.innerHTML = `<div class="q5mom">
    <div class="q5momtitle">🔥 Q5勢い上位8</div>
    ${rows}
  </div>`;
}

// 登録銘柄数の表示(design 2026-09-18)。/api/rankingが返すrows(=現在の
// watchlist)の件数と、既存のstock_logic.MAX_TICKERS(max_ticketsとして
// レスポンスに追加)をそのまま表示するだけの表示専用機能。Q1〜Q5判定・
// Q5経過状態・追加/削除の既存処理には一切変更を加えない。
function renderTickerCount(j){
  const el = document.getElementById("tickerCount");
  if(!el) return;
  const n = (j.rows||[]).length;
  const max = j.max_tickers ?? 20;
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
  const dateEl=document.getElementById("ticker-date"), priceEl=document.getElementById("ticker-price");
  const dateRaw = dateEl ? dateEl.value.trim() : "";
  const priceRaw = priceEl ? priceEl.value.trim() : "";
  const btn=document.getElementById("addBtn");
  btn.disabled=true;
  document.getElementById("status").textContent=`${t}を追加して即時取得中…`;
  try{
    const r=await fetch("/api/watchlist",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticker:t})});
    const j=await r.json();
    if(!j.ok){alert(j.error||"追加できませんでした");return;}
    let note = j.fetch_note ? `${t}: ${j.fetch_note}` : null;

    // 取得価格が入力されている場合のみ、追加直後に保有ポジションとしても登録する
    // (省略時は従来どおりウォッチリスト登録のみ、既存の「監視だけ」の使い方を維持)。
    // entry_date/entry_price自体の検証・保存ロジックは既存の/api/portfolio/confirm_buy
    // (Q5/BUY/SELL/+50%利確/Risk-off等の既存ロジックには一切関与しない)をそのまま使う。
    if(priceRaw !== ""){
      const buyBody = {ticker:t, entry_price: parseFloat(priceRaw)};
      if(dateRaw) buyBody.entry_date = dateRaw;
      const r2 = await fetch("/api/portfolio/confirm_buy",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(buyBody)});
      const j2 = await r2.json();
      if(!j2.ok){
        note = `${t}はウォッチリストに追加しましたが、保有登録に失敗しました（${j2.error||"不明なエラー"}）`;
      }
    }

    el.value=""; if(dateEl) dateEl.value=""; if(priceEl) priceEl.value="";
    await updateRanking(note);
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
// design 2026-10-26ユーザー確定: 今後のQ5判断材料としてベータを使わない方針
// のため、render()のカードからこの関数の呼び出しを削除した(関数自体・
// beta_logic.py・算出データは無変更、呼び出されなくなっただけ)。
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
// 2026-09-29修正: 「ok」時に固定文言「Q5 Day 0」を表示していたのを廃止した
// (このバッジのsig.days_elapsedは元々「現在Q5なら常に0」という固定値であり、
// 実際の継続日数ではない。実際の継続日数はq5_progress側(q5ProgressHtml、
// 「Q5 Day N」)が正しく数えているため、CIFR等のようにこのバッジと
// q5_progressバッジで異なる「Day」の値が同時表示される不整合があった。
// design: CIFR/AEHR/AXTI/MXL/NBISの監査で確認)。
function q5SignalHtml(q){
  if(!q || q.status !== "ready" || !q.q5_signal) return "";
  const sig = q.q5_signal;
  if(sig.status === "ok"){
    return `<div class="q5sig q5sig-ok"><span class="q5sigmain">🟢 購入OK</span><span class="q5sigsub">Q5シグナル有効</span></div>`;
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

  return `<div class="q5prog">
    <div class="q5progtop">
      <span class="q5progday">Q5 Day ${p.days_elapsed}</span>
      <span class="q5progstate ${stateCls}">${p.current_state_emoji} ${esc(p.current_state_label)}</span>
      <span class="q5progret ${retCls}">Q5起点 ${fmtSignedPct(p.current_return_pct)}</span>
    </div>
  </div>`;
}

// Q5 Day5注意喚起(design 2026-09-18の過去データ検証結果に基づく正式実装)。
// Q5クールのDay5時点のQ5起点騰落率が-7.5%以下の場合にのみ発生し、以後は
// 現在のQ5クールの状態(新クール開始・クール②のDay5回復)とは無関係に、
// 発生から最大40営業日保持される(発生・保持・失効の判定はrefresh.py側で
// 完結済み、ここは表示専用)。購入不可等の判定には一切関与しない。
// 将来の下落を断定する表現(「〜になる確率」等)は使わない。
// design 2026-10-26ユーザー確定: 現在のQ5判断材料として使わない方針のため、
// render()のカードからこの関数の呼び出しを削除した(関数自体・発生/失効の
// 判定ロジック・refresh.py側データは無変更、呼び出されなくなっただけ)。
function q5WarningHtml(q){
  if(!q || q.status !== "ready" || !q.q5_warning) return "";
  return `<div class="q5warn">
    <div class="q5warntop">🔴 続落　⚠️ −20%程度までの下落に注意</div>
    <div class="q5warnnote">過去の類似ケースでは、Q5開始後5日目の下落が大きい局面で、その後−20%程度まで下落したケースが多く確認されています。</div>
  </div>`;
}

// Q5中線割れ:購入中断 補助表示(design 2026-10-02ユーザー確定仕様)。
// バックエンド(_build_midline_halt_view、app.py)が組み立てたq5_midline_haltを
// そのまま表示するだけ。発生(Q5 Day0〜Day7以内の初回中線割れ)・解除(終値が
// SMA20以上を3取引日連続維持)の判定はrefresh.py側で完結済み、ここでは値の
// 解釈・判定は一切行わない。購入不可・売却・除外の判定ではなく、保有継続・
// 買い増しを再検討する材料としての補助表示(一覧・ランキングからは除外しない)。
// design 2026-10-26ユーザー確定: 現在のQ5判断で中線割れによる購入中断を
// 使わない方針のため、render()のカードからこの関数の呼び出しを削除した
// (関数自体・発生/解除の判定ロジック・refresh.py側データは無変更、
// 呼び出されなくなっただけ)。
function q5MidlineHaltHtml(q){
  if(!q || q.status !== "ready" || !q.q5_midline_halt) return "";
  const h = q.q5_midline_halt;
  const streakLine = h.recover_streak > 0
    ? `中線回復中（${h.recover_streak}/${h.unlock_streak_required}日）`
    : `中線割れ中`;
  return `<div class="q5halt">
    <div class="q5halttop">🛑 Q5中線割れ：購入中断</div>
    <div class="q5haltsub">中線割れ発生日：${esc(fmtDate(h.break_date))}　${esc(streakLine)}</div>
  </div>`;
}

// 今日・昨日・2〜5日前(計6日分)のQ状態を横長の表で表示する。
// 2026-09-29修正: 日付計算(取引日オフセット・前方補完)は全てバックエンド
// (app.py: _build_daily_breakdown/_resolve_q_for_date、実取引日カレンダー
// ベース)側で行うようになり、ここは受け取ったq.daily_breakdownをそのまま
// 描画するだけになった(従来JS側にあった暦日ベースのaddDaysToDateStr/
// daysBetweenDateStrは、土日を挟むと「N日前」が実際のN取引日前とズレる
// 不整合があったため廃止、design: BEの監査で確認)。
// バックエンド側のhistory保存方式・refresh.py・quintile_logic.py・Q1〜Q5
// 判定ロジックは一切変更していない。
// 2026-09-29(2回目): UI整理の依頼により「Q5継続：N日」の表示行を非表示に
// した(バックエンド側のq.q5_continuation_days計算・API応答は削除していない、
// このJS側の描画だけを取りやめる表示専用の変更)。
function qDailyBreakdownHtml(q){
  if(!q || q.status !== "ready" || !q.daily_breakdown || !q.daily_breakdown.length) return "";

  const headCells = q.daily_breakdown.map(d => `<th>${esc(d.label)}</th>`).join("");
  const valCells = q.daily_breakdown.map(d => `<td>${d.q ? esc(d.q) : "—"}</td>`).join("");

  return `<div class="qdaily-wrap"><table class="qdaily-table"><thead><tr>${headCells}</tr></thead>`
    + `<tbody><tr>${valCells}</tr></tbody></table></div>`;
}

// Q5の過去実績統計。あくまで「過去の類似状態における統計」であり、
// 将来この銘柄が同じように上がると予測するものではないことを明記する(design 14)。
// 2026-09-17のUI整理で、常時表示から「📊 過去実績」ボタン(details/summary)
// クリックで展開する形式に変更。中身(数値・注意書き)は一切変更していない。
// 2026-09-29: UI整理の依頼により、render()側の呼び出しを外して非表示にした
// (この関数自体・バックエンド側のq5_stats計算〈quintile_logic.load_q5_stats〉・
// APIレスポンスは削除していない、カードへの描画を取りやめる表示専用の変更)。
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
      ${q5SignalHtml(x.quintile)}
      ${q5ProgressHtml(x.quintile)}
      ${qDailyBreakdownHtml(x.quintile)}

      <div class="pricebar">
        <span>株価 <b>${fmt(x.price)}</b></span>
        <span>RSI14 <b>${fmt(x.rsi14)}</b></span>
      </div>
      <div class="statrow">
        <div class="stat"><div class="statlabel">前日比</div><div class="statval ${x.change_pct>0?'up':x.change_pct<0?'down':''}">${fmt(x.change_pct,"%")}</div></div>
        <div class="stat"><div class="statlabel">3日間</div><div class="statval ${x.three_day_return>0?'up':x.three_day_return<0?'down':''}">${fmt(x.three_day_return,"%")}</div></div>
        <div class="stat"><div class="statlabel">1週間</div><div class="statval ${x.week_return>0?'up':x.week_return<0?'down':''}">${fmt(x.week_return,"%")}</div></div>
        <div class="stat"><div class="statlabel">1ヶ月</div><div class="statval ${x.month_return>0?'up':x.month_return<0?'down':''}">${fmt(x.month_return,"%")}</div></div>
      </div>

      <div class="cardfoot">
        ${freshTagHtml(x.freshness)}
        <button class="secondary" onclick="delTicker('${esc(x.ticker)}')">削除</button>
      </div>
    </div>`;
  }).join("");
}

updateRanking();
// 「📈 実績比較：私 vs Q5」(① Q5直近勢い・② 実績比較)は表示を停止する
// (design 2026-10-26ユーザー確定: サンプル数が少なく比較・判断する段階では
// ないため)。renderComparison/loadComparison関数・/api/compare/q5universe・
// comparison_logic.py側のロジック・データは一切変更せず残してある
// (呼び出しを止めるだけの表示専用の変更)。
// loadComparison();

// PWA化(design 2026-09-17): ホーム画面追加・スタンドアロン起動のためのSW登録のみ。
// /sw.js側でAPI(/api/*)は明示的にキャッシュ対象外にしており、株価・Q1〜Q5・
// Q5シグナルの表示は従来通り毎回サーバーから取得する(挙動・API呼び出しは無変更)。
if("serviceWorker" in navigator){
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
}

// Web Push通知(design 2026-10-22)。「🔔 更新通知を有効にする」ボタンの
// 表示・購読登録と、Push未到達時のフォールバック通知のみを行う。
// Q1〜Q5・Q5シグナル・BUY/SELL・Risk-off・実績比較・株価APIの取得回数には
// 一切関与しない(完全に独立した追加UI)。
function _b64ToUint8Array(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const base64 = (b64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

function _renderPushBar(html, onEnableClick) {
  const el = document.getElementById("pushNotifyBar");
  if (!el) return;
  el.innerHTML = html;
  if (onEnableClick) {
    const btn = el.querySelector("#pushEnableBtn");
    if (btn) btn.addEventListener("click", onEnableClick);
  }
}

async function enablePushNotify(vapidKey) {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      _renderPushBar('<div class="small">通知は許可されませんでした(ブラウザ設定から変更できます)</div>');
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _b64ToUint8Array(vapidKey),
    });
    await fetch("/api/push/subscribe", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({subscription: sub.toJSON()}),
    });
    _renderPushBar('<div class="small">🔔 更新通知: 有効</div>');
  } catch (e) {
    _renderPushBar('<div class="small">通知の設定に失敗しました</div>');
  }
}

async function initPushNotifyUI() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return;
  }
  try {
    const r = await fetch("/api/push/public-key");
    const j = await r.json();
    if (!j.ok || !j.enabled || !j.key) return;

    if (Notification.permission === "granted") {
      _renderPushBar('<div class="small">🔔 更新通知: 有効</div>');
    } else if (Notification.permission === "denied") {
      _renderPushBar('<div class="small">通知がブロックされています(ブラウザ設定で許可すると有効化できます)</div>');
    } else {
      _renderPushBar(
        '<button class="secondary" id="pushEnableBtn">🔔 更新通知を有効にする</button>',
        () => enablePushNotify(j.key)
      );
    }
  } catch (e) {
    // 取得できなくても既存機能には影響させない。
  }
}

// フォールバック: Web Pushが端末に届かなかった場合に備え、アプリを開いた
// (ページ読み込み)時点でも未通知の新commitがないか確認し、あればその場で
// 同じ内容のデスクトップ通知を表示する(last_notified_commitが既に最新なら
// 何もしない=二重通知しない)。
async function checkPushFallback() {
  if (!("serviceWorker" in navigator) || Notification.permission !== "granted") return;
  try {
    const r = await fetch("/api/push/status");
    const j = await r.json();
    if (!j.ok || !j.pending_notification || !j.current_commit) return;

    const reg = await navigator.serviceWorker.ready;
    const commit = j.current_commit;
    await reg.showNotification("Stock Buy App", {
      body: "アプリが最新版に更新されました。\ncommit: " + commit.slice(0, 7),
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      tag: "stock-buy-app-update",
      data: {commit: commit},
    });
    await fetch("/api/push/ack", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({commit: commit}),
    });
  } catch (e) {
    // フォールバック自体の失敗は既存機能に影響させない。
  }
}

window.addEventListener("load", () => {
  initPushNotifyUI();
  checkPushFallback();
});
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

// Web Push通知(design 2026-10-22)。Renderで新しいcommitがLiveになった時、
// アプリを閉じていてもこのpushイベントが届く。Q1〜Q5等の判定・キャッシュ
// 戦略には一切影響しない(上のfetchハンドラとは完全に独立)。
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = {};
  }
  const title = payload.title || "Stock Buy App";
  const commit = payload.commit || "";
  const body = payload.body
    ? (commit ? payload.body + "\ncommit: " + commit : payload.body)
    : "アプリが最新版に更新されました。";

  const show = self.registration.showNotification(title, {
    body: body,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    tag: "stock-buy-app-update",
    data: {commit: commit},
  }).then(() => {
    // 実際に表示できたことをサーバーへ通知する(last_notified_commitの更新)。
    // 失敗してもshowNotification自体には影響させない(fire-and-forget)。
    if (commit) {
      return fetch("/api/push/ack", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({commit: commit}),
      }).catch(() => {});
    }
  });

  event.waitUntil(show);
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({type: "window", includeUncontrolled: true}).then((clientsArr) => {
      for (const c of clientsArr) {
        if ("focus" in c) return c.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
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


# 日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md
# フェーズ3)。「実際にユーザーが取引した後に記録する」確認APIのみを追加する。
# これらのAPIは証券口座と連携しておらず、呼び出しただけで売買が実行されたと
# 仮定することは一切ない(ユーザーが実際の取引後に明示的に呼ぶ想定)。
# いずれも_compute_daily_actions()の判定ロジックには一切関与しない
# (判定はリクエスト時に/api/ranking側で都度計算するだけで、記録とは独立)。

@app.post("/api/portfolio/confirm_buy")
def confirm_buy():
    try:
        body = request.get_json(silent=True) or {}
        ticker = _sanitize_ticker(body.get("ticker", ""))
        if not ticker:
            return jsonify({"ok": False, "error": "銘柄コードを入力してください"}), 400
        if ticker not in _get_watchlist():
            return jsonify({"ok": False, "error": f"{ticker}は監視銘柄に登録されていません"}), 400

        try:
            entry_price = float(body.get("entry_price"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "取得価格は数値で入力してください"}), 400
        if not (entry_price > 0):
            return jsonify({"ok": False, "error": "取得価格は正の数で入力してください"}), 400

        # 株数(shares)は任意入力(design 2026-10-18、+50%利益100%利確ルール用)。
        # 省略時はNoneのまま保存し、_compute_daily_actions/confirm_profitの
        # $ベース利確計算は行わない(架空の株数は作らない、既存の「取得価格のみ
        # 記録する」フローはそのまま維持する)。部分株式(小数)を許容する
        # (ユーザー確定仕様: 3.333...株のような端数計算を前提とするため)。
        shares = None
        shares_raw = body.get("shares")
        if shares_raw not in (None, ""):
            try:
                shares = float(shares_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "株数は数値で入力してください"}), 400
            if not (shares > 0):
                return jsonify({"ok": False, "error": "株数は正の数で入力してください"}), 400

        trading_calendar = _trading_days_calendar()
        entry_date = body.get("entry_date")
        if entry_date:
            try:
                date.fromisoformat(entry_date)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "取得日はYYYY-MM-DD形式で入力してください"}), 400
            # 未来日判定は実時間の「今日(JST)」基準(edit_entryと同じ判定、design
            # 2026-10-20の修正をそのまま踏襲)。株価データの最終取引日
            # (trading_calendar[-1])より古い/新しいかは無関係に、実際の未来日
            # のみを拒否する(過去日・最終取引日より後でも今日以前なら許可)。
            if entry_date > _today_jst_str():
                return jsonify({"ok": False, "error": "取得日は未来の日付にできません"}), 400
        elif trading_calendar:
            entry_date = trading_calendar[-1]
        else:
            return jsonify({"ok": False, "error": "取引日カレンダーが未取得のため、取得日を省略できません"}), 400

        state = store.get_portfolio_state() or {}
        positions = dict(state.get("positions") or {})

        # 二重登録防止: 既に保有中なら拒否する(entry_date/entry_price/
        # profit_takenを勝手に上書きしない)。
        if ticker in positions:
            return jsonify({"ok": False, "error": f"{ticker}は既に保有中です"}), 400
        # 6枠超過防止: 安全側で拒否する(強制入替・自動置換はしない)。
        if len(positions) >= MAX_POSITIONS:
            return jsonify({
                "ok": False,
                "error": f"保有枠が満杯です(最大{MAX_POSITIONS}銘柄)。空き枠ができてから記録してください",
            }), 400

        positions[ticker] = {
            "entry_date": entry_date, "entry_price": entry_price, "shares": shares,
            "profit_taken": False, "profit_taken_date": None,
        }
        # trade_history(design 2026-10-21、実運用の売買履歴記録)はここでは
        # 変更しないが、state全体を上書きするため素通しで引き継ぐ(引き継がないと
        # 次のconfirm_buy呼び出しで既存の売却履歴が消えてしまう)。
        new_state = {"positions": positions, "trade_history": state.get("trade_history") or [],
                      "last_updated": trading_calendar[-1] if trading_calendar else entry_date}
        if not store.set_portfolio_state(new_state):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502

        return jsonify({"ok": True, "ticker": ticker, "position": positions[ticker],
                         "held_count": len(positions)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/portfolio/confirm_profit")
def confirm_profit():
    try:
        body = request.get_json(silent=True) or {}
        ticker = _sanitize_ticker(body.get("ticker", ""))
        if not ticker:
            return jsonify({"ok": False, "error": "銘柄コードを入力してください"}), 400

        state = store.get_portfolio_state() or {}
        positions = dict(state.get("positions") or {})
        pos = positions.get(ticker)
        if not pos:
            return jsonify({"ok": False, "error": f"{ticker}は保有していません"}), 400

        # 冪等性: 既に利確済みなら状態を変更せずそのまま成功として返す
        # (entry_date/entry_price/profit_taken_dateはいずれも書き換えない)。
        if pos.get("profit_taken"):
            return jsonify({"ok": True, "ticker": ticker, "position": pos, "already_done": True})

        trading_calendar = _trading_days_calendar()
        today = trading_calendar[-1] if trading_calendar else state.get("last_updated")
        new_pos = dict(pos)
        new_pos["profit_taken"] = True
        new_pos["profit_taken_date"] = today
        # entry_date/entry_priceは変更しない(20日タイマー・損益計算の基準を保つ)。

        # +50%利益100%利確ルール(design 2026-10-18): sharesが記録されている
        # 銘柄のみ、現在価格で「取得原本を残し含み益を100%利確」した結果の
        # 残り株数を計算してpositionsに反映する(保存先の実行結果はこの1回の
        # confirm呼び出し時点の価格で確定する、既存のconfirm_buy/confirm_sellと
        # 同じ「ユーザーが確認した時点の値で記録する」方式)。sharesが無い場合は
        # 既存通りprofit_takenフラグのみを更新する(手動「利益確定」機能の
        # 後方互換、架空の株数は作らない)。
        current_price = (store.get_ticker_record(ticker) or {}).get("price")
        amounts = _compute_profit_take_amounts(pos.get("shares"), pos.get("entry_price"), current_price)
        if amounts["remaining_shares"] is not None:
            new_pos["shares"] = amounts["remaining_shares"]
            new_pos["profit_take_sold_shares"] = amounts["sell_shares"]
            new_pos["profit_take_sold_value"] = amounts["sell_value"]
            new_pos["profit_take_price"] = current_price
        positions[ticker] = new_pos
        # trade_history(design 2026-10-21)はここでは変更しないが、素通しで引き継ぐ
        # (引き継がないと次回呼び出しで既存の売却履歴が消えてしまう)。
        new_state = {"positions": positions, "trade_history": state.get("trade_history") or [],
                      "last_updated": today}
        if not store.set_portfolio_state(new_state):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502

        return jsonify({"ok": True, "ticker": ticker, "position": new_pos, "already_done": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/portfolio/confirm_sell")
def confirm_sell():
    """保有ポジションの売却を記録する。design 2026-10-21で、実際の売買成績を
    蓄積するため、売却価格(exit_price)・売却日(exit_date)を任意で受け取り、
    positions削除前にtrade_history(ticker/entry_date/entry_price/exit_date/
    exit_price/return_pct)へ追記する。exit_priceが未指定かつ現在価格も
    取得できない場合はtrade_historyへの追記のみスキップし、positions削除自体
    は従来通り成功させる(既存の「価格を入れずに売却確認するだけ」の挙動を
    壊さない)。株数(shares)はこの履歴には含めない(design確定仕様)。"""
    try:
        body = request.get_json(silent=True) or {}
        ticker = _sanitize_ticker(body.get("ticker", ""))
        if not ticker:
            return jsonify({"ok": False, "error": "銘柄コードを入力してください"}), 400

        state = store.get_portfolio_state() or {}
        positions = dict(state.get("positions") or {})

        # 冪等性: 既に未保有なら状態を変更せずそのまま成功として返す。
        if ticker not in positions:
            return jsonify({"ok": True, "ticker": ticker, "already_absent": True,
                             "held_count": len(positions)})

        pos = positions[ticker]

        exit_price = None
        exit_price_raw = body.get("exit_price")
        if exit_price_raw not in (None, ""):
            try:
                exit_price = float(exit_price_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "売却価格は数値で入力してください"}), 400
            if not (exit_price > 0):
                return jsonify({"ok": False, "error": "売却価格は正の数で入力してください"}), 400

        trading_calendar = _trading_days_calendar()
        exit_date = body.get("exit_date")
        if exit_date:
            try:
                date.fromisoformat(exit_date)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "売却日はYYYY-MM-DD形式で入力してください"}), 400
            # 未来日判定はconfirm_buy/edit_entryと同じ実時間の「今日(JST)」基準。
            if exit_date > _today_jst_str():
                return jsonify({"ok": False, "error": "売却日は未来の日付にできません"}), 400
        else:
            exit_date = trading_calendar[-1] if trading_calendar else state.get("last_updated")

        # 取得日より前の売却日は拒否する(design 2026-10-21修正: 買う前に売った
        # という矛盾したtrade_historyが記録される不具合の修正)。entry_date==
        # exit_dateは許可(同日中の売買)。exit_dateが省略時デフォルト由来でも
        # 同じ基準を適用する(entry_dateは常に「今日(JST)」以前のため通常は
        # 問題にならないが、一貫性のため分岐しない)。
        if pos.get("entry_date") and exit_date and exit_date < pos.get("entry_date"):
            return jsonify({"ok": False, "error": "売却日は取得日より前にできません"}), 400

        if exit_price is None:
            exit_price = (store.get_ticker_record(ticker) or {}).get("price")

        del positions[ticker]

        trade_history = list(state.get("trade_history") or [])
        entry_price = pos.get("entry_price")
        if exit_price is not None and entry_price:
            trade_history.append({
                "ticker": ticker,
                "entry_date": pos.get("entry_date"),
                "entry_price": entry_price,
                "exit_date": exit_date,
                "exit_price": exit_price,
                "return_pct": (exit_price / entry_price - 1) * 100,
            })

        today = trading_calendar[-1] if trading_calendar else state.get("last_updated")
        new_state = {"positions": positions, "trade_history": trade_history, "last_updated": today}
        if not store.set_portfolio_state(new_state):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502

        return jsonify({"ok": True, "ticker": ticker, "already_absent": False,
                         "held_count": len(positions)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/portfolio/edit_entry")
def edit_entry():
    """既存保有ポジションの取得単価・取得日を訂正する(design 2026-10-20)。
    実際の購入日/購入価格が誤って登録された場合の訂正専用で、新規購入
    (confirm_buy)とは別のAPI。positions[ticker]のentry_price/entry_dateを
    直接更新するだけで、新しい日付フィールドは追加しない(二重管理しない)。

    entry_priceだけ・entry_dateだけ・両方同時、のいずれの指定も許可する
    (未指定/空欄のフィールドは変更しない)。entry_dateを変更すると、
    20営業日SELL判定・保有期間表示はいずれも既存の_trading_day_index
    ベースの計算がentry_dateを読むだけなので、ここでentry_dateを書き換える
    以外の追加対応は不要(再計算ロジック自体は無変更)。

    profit_taken/profit_taken_date/profit_take_sold_shares/
    profit_take_sold_value/profit_take_priceといった利確履歴は、取得情報の
    訂正によって一切リセットしない。"""
    try:
        body = request.get_json(silent=True) or {}
        ticker = _sanitize_ticker(body.get("ticker", ""))
        if not ticker:
            return jsonify({"ok": False, "error": "銘柄コードを入力してください"}), 400

        state = store.get_portfolio_state() or {}
        positions = dict(state.get("positions") or {})
        pos = positions.get(ticker)
        if not pos:
            return jsonify({"ok": False, "error": f"{ticker}は保有していません"}), 400

        new_entry_price = None
        entry_price_raw = body.get("entry_price")
        if entry_price_raw not in (None, ""):
            try:
                new_entry_price = float(entry_price_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "取得単価は数値で入力してください"}), 400
            if not (new_entry_price > 0):
                return jsonify({"ok": False, "error": "取得単価は正の数で入力してください"}), 400

        new_entry_date = None
        entry_date_raw = body.get("entry_date")
        if entry_date_raw not in (None, ""):
            try:
                date.fromisoformat(entry_date_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "取得日はYYYY-MM-DD形式で入力してください"}), 400
            # 未来日判定は実時間の「今日(JST)」を基準にする(design 2026-10-20
            # 修正: _trading_days_calendar()[-1]は株価データが実際に処理された
            # 最終取引日であり、更新タイミングによっては実際の今日より遅れる
            # ことがある。そのため「株価データの最終取引日より後」という理由
            # だけでは拒否しない。実際に未来の日付だけを拒否する)。
            if entry_date_raw > _today_jst_str():
                return jsonify({"ok": False, "error": "取得日は未来の日付にできません"}), 400
            new_entry_date = entry_date_raw

        if new_entry_price is None and new_entry_date is None:
            return jsonify({"ok": False, "error": "取得単価または取得日のいずれかを入力してください"}), 400

        new_pos = dict(pos)
        if new_entry_price is not None:
            new_pos["entry_price"] = new_entry_price
        if new_entry_date is not None:
            new_pos["entry_date"] = new_entry_date
        # profit_taken/profit_taken_date/profit_take_sold_shares/
        # profit_take_sold_value/profit_take_price/sharesはここでは一切
        # 変更しない(利確履歴を取得情報の訂正でリセットしないため)。
        positions[ticker] = new_pos

        trading_calendar = _trading_days_calendar()
        # trade_history(design 2026-10-21)はここでは変更しないが、素通しで引き継ぐ
        # (引き継がないと既存の売却履歴が訂正操作のたびに消えてしまう)。
        new_state = {"positions": positions, "trade_history": state.get("trade_history") or [],
                      "last_updated": trading_calendar[-1] if trading_calendar else state.get("last_updated")}
        if not store.set_portfolio_state(new_state):
            return jsonify({"ok": False, "error": "保存先(Redis)への書き込みに失敗しました"}), 502

        return jsonify({"ok": True, "ticker": ticker, "position": new_pos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/compare/q5universe")
def compare_q5universe():
    """自分の実績(trade_history)とQ5 48銘柄の仮想実績(q5universe:entries)の
    比較集計(design 2026-10-21)。読み取り専用で、trade_history・
    q5universe:state・positions・Q1〜5判定等、既存のどのキーにも一切
    書き込まない。集計ロジック自体はcomparison_logic.py(純粋関数)に
    委ねるだけで、ここでは既存データを読んで渡すだけ。"""
    try:
        portfolio_state = store.get_portfolio_state() or {}
        trade_history = portfolio_state.get("trade_history") or []
        positions = portfolio_state.get("positions") or {}

        q5_state = store.get_q5universe_state() or {}
        q5_entries = q5_state.get("entries") or []
        latest_price = q5_state.get("latest_price") or {}
        latest_price_date = q5_state.get("latest_price_date")

        trading_calendar = _trading_days_calendar()
        anchor_date = trading_calendar[-1] if trading_calendar else None

        report = comparison_logic.build_comparison_report(trade_history, q5_entries, trading_calendar, anchor_date)
        # 「Q5直近勢い」(design 2026-10-21): 既存の確定成績(report)とは別の
        # 指標で、day5/20/60の確定を問わずday0から現在までの途中経過を
        # 含める。既存のrecent_window/all_time_by_horizonのキー・値には
        # 一切影響しない(完全に追加のキー)。
        momentum = comparison_logic.build_momentum_report(q5_entries, latest_price, trading_calendar, anchor_date)

        # 「①保有確定」「②現在価格」の2ブロック(design 2026-10-21確定仕様)。
        # Q5側はQ5 Day0起算(既存のrecent_window[...].q5=確定成績、
        # momentum=直近勢いをそのまま再利用、新しい計算はしない)。
        # 私側は実際の購入日(entry_date)起算(この違いは意図的な仕様)。
        # 保有中ポジションの現在価格は、既存の保有銘柄一覧
        # (_build_portfolio_view)と同じstore.get_ticker_recordをそのまま
        # 使うだけで、新しい取得経路は増やさない。株数は使わない。
        current_price_by_ticker = {
            ticker: (store.get_ticker_record(ticker) or {}).get("price")
            for ticker in positions
        }
        my_confirmed = comparison_logic.build_my_confirmed_report(
            trade_history, positions, current_price_by_ticker, trading_calendar, anchor_date)
        my_current_price = comparison_logic.build_my_current_price_report(
            trade_history, positions, current_price_by_ticker, trading_calendar, anchor_date)

        # 「③実運用成績」(design 2026-10-21最終確定仕様): Q5 48銘柄を
        # 「Day0→原則20営業日保有→20営業日後に仮想売却」という既存運用
        # ルールと同じ基準で評価した成績と、私が実際に購入した銘柄群の
        # 成績を、5/20/60/120/180営業日の観測期間ごとに比較する。株数・
        # 資金額・NAV等は一切使わない(銘柄ごとのリターン%の平均・中央値・
        # 勝率のみ)。既存のrecent_window(確定成績)・momentum(直近勢い)・
        # my_confirmed・my_current_priceにはいずれも影響しない(別計算)。
        actual_performance = comparison_logic.build_actual_performance_report(
            q5_entries, trade_history, positions, latest_price, current_price_by_ticker,
            trading_calendar, anchor_date)

        return jsonify({
            "ok": True,
            "recent_window": report["recent_window"],
            "all_time_by_horizon": report["all_time_by_horizon"],
            "q5_entries": comparison_logic.enrich_entries_with_returns(q5_entries),
            "momentum": momentum,
            "my_confirmed": my_confirmed,
            "my_current_price": my_current_price,
            "actual_performance": actual_performance,
            "latest_price_date": latest_price_date,
        })
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
        # 日次アクション型UI(design 2026-10-16)のBUY候補選定はwatchlist登録順
        # (検出順)を使う仕様のため、表示用の並び替え(sort_rows、下記)より前の
        # 順序を別名で保持しておく。既存のrows(表示用)の並び順・内容には
        # 一切影響しない(sort_rows自体・その呼び出し位置も変更しない)。
        rows_watchlist_order = list(rows)
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

        # 日次アクション型UI(design 2026-10-16、フェーズ3)。既存のQ1〜Q5判定・
        # Day0判定・Risk-off判定・ランキング(sort_rows)・他のレスポンスキーには
        # 一切変更を加えない、読み取り専用の追加キー。trading_calendarが空
        # (初回デプロイ直後等)の場合は判定基準日が無いためdaily_actionsはNone
        # のままとする(架空の判定は作らない)。
        # Q5乗り換え推奨(design 2026-10-24)が使うAI_UNIVERSE当日騰落率。
        # refresh._update_ai_market_decline_stateが既存のATH取得データから
        # 追加計算した値をそのまま読むだけ(新規API呼び出しなし)。未計算
        # (初回デプロイ直後・取得失敗等)の場合はNoneのままとなり、
        # switch_recommendation_logicが「条件②を満たさない」として扱う。
        ai_market_decline_state = store.get_ai_market_decline_state()
        ai_universe_daily_return = (ai_market_decline_state or {}).get("ai_universe_daily_return")

        daily_actions = None
        if trading_calendar:
            daily_actions = _compute_daily_actions(
                rows_watchlist_order, store.get_portfolio_state(), store.get_riskoff_state(),
                trading_calendar[-1], trading_calendar, ai_universe_daily_return,
            )

        # 現在Q5銘柄のprior_mean参考情報(design 2026-10-24)。既存のQ1〜Q5判定・
        # rows・ソート順には一切変更を加えない、読み取り専用の追加キー。
        current_q5_prior_means = (
            _build_current_q5_prior_means(rows_watchlist_order, trading_calendar[-1])
            if trading_calendar else None
        )

        # watchlist全銘柄のprior_mean一覧(design 2026-10-25、登録段階での
        # 事前確認用)。current_q5_prior_means(現在Q5の銘柄のみ)とは別の
        # 読み取り専用キー。tickersはwatchlist登録順そのまま(_get_watchlist
        # の戻り値、sort_rowsを通していない)。
        watchlist_prior_means = (
            _build_watchlist_prior_means(tickers, trading_calendar[-1])
            if trading_calendar else None
        )
        watchlist_prior_means_anchor_date = trading_calendar[-1] if trading_calendar else None

        return jsonify({
            "ok": True,
            "rows": rows,
            "q5_momentum": _compute_q5_momentum_ranking(rows),
            "risk_off": _build_riskoff_view(),
            "ai_market_decline": _build_ai_market_decline_view(),
            "market_regime": _build_market_regime_view(),
            "daily_actions": daily_actions,
            "portfolio": _build_portfolio_view(),
            "current_q5_prior_means": current_q5_prior_means,
            "watchlist_prior_means": watchlist_prior_means,
            "watchlist_prior_means_anchor_date": watchlist_prior_means_anchor_date,
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


# ---------------------------------------------------------------------------
# Web Push通知(design 2026-10-22)。Q1〜Q5・Q5シグナル・BUY/SELL・Risk-off・
# 実績比較・株価API取得・GitHub Actionsには一切関与しない、表示層の追加のみ。
# ---------------------------------------------------------------------------

@app.get("/api/push/public-key")
def push_public_key():
    return jsonify({"ok": True, "enabled": push_notify.is_configured(), "key": push_notify.get_vapid_public_key()})


@app.post("/api/push/subscribe")
def push_subscribe():
    try:
        body = request.get_json(silent=True) or {}
        sub = body.get("subscription")
        ok, err = push_notify.add_subscription(store, sub)
        if not ok:
            return jsonify({"ok": False, "error": err or "購読情報が不正です"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    try:
        body = request.get_json(silent=True) or {}
        endpoint = body.get("endpoint")
        push_notify.remove_subscription(store, endpoint)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/push/status")
def push_status():
    try:
        return jsonify({"ok": True, **push_notify.get_push_status(store)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/push/ack")
def push_ack():
    try:
        body = request.get_json(silent=True) or {}
        commit = body.get("commit")
        push_notify.ack_notified(store, commit)
        # ack自体の成否(commit不一致など)で画面側の処理を失敗させる必要はない。
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
