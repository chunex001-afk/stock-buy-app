"""日次の株価自動更新ジョブ、および銘柄追加時の即時取得から共通利用される更新ロジック。

GitHub Actions の schedule（.github/workflows/daily-refresh.yml）から
1日1回 `python refresh.py` として実行される想定のエントリポイント。
Render Web Service（app.py）はこのスクリプトが書き込んだUpstash Redis
のデータを読むだけで、日常的には自らTwelve Dataへ新規アクセスはしない。
ただし app.py は本モジュールの `backfill_quintile_history_for_new_ticker` を
import し、銘柄追加直後の即時取得（1銘柄のみ）で再利用する。

2026-09-17: Alpha Vantage完全撤去・Twelve Data一本化。株価取得は全て
twelvedata_client経由になり、旧指標(stock_logic.build_result、株価・RSI・
前日比・1ヶ月騰落率)とQ1〜Q5判定(quintile_logic)は、同じ1回のTwelve Data
取得結果を共有して両方に使う(取得回数を増やさない)。ニュース・時価総額
(企業情報)はTwelve Dataで代替できないため機能ごと撤去した。

設計上の原則:
- 1銘柄の取得失敗が他銘柄の処理を止めない（銘柄ごとにtry/except）
- Twelve Data Basicプラン(800 credits/day)を超えないよう、自己申告の予算
  (twelvedata_client.DAILY_API_BUDGET)を使い切ったら残りは前回データのまま
  スキップする
- Redis接続断など想定外の例外でもプロセス全体をクラッシュさせない
- Q1〜Q5判定ロジック(quintile_logic.py呼び出し部分)は今回のAlpha Vantage
  撤去作業で一切変更していない
"""

import bisect
import os
import sys
from datetime import datetime, timezone

import beta_logic
import quintile_logic
import redis_store as store
import stock_logic as logic
import twelvedata_client as td


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _mark_stale(ticker, error_type, message):
    """取得失敗時、前回の正常データを保持したまま失敗理由だけを上書きする。
    Alpha Vantage固有の処理ではなく、Twelve Data撤去後も無変更で使う。"""
    try:
        rec = store.get_ticker_record(ticker) or {"ticker": ticker}
        rec["is_stale"] = True
        rec["last_error"] = error_type
        rec["last_error_message"] = message
        rec["last_error_at"] = _now_iso()
        store.set_ticker_record(ticker, rec)
    except Exception as e:  # Redis書き込み自体の失敗もジョブを止めない
        print(f"[WARN] {ticker}: 失敗記録の保存にも失敗しました: {e}", file=sys.stderr)


def _build_rank_rows(tickers):
    rows = []
    for t in tickers:
        rec = store.get_ticker_record(t) or {}
        rows.append({"ticker": t, "judgment": rec.get("judgment", "判定不可"), "upside_score": rec.get("upside_score")})
    return rows


def update_rank_snapshot(tickers):
    """当日のランキング順位をRedisに保存する。保存済みスナップショットの日付が
    今日と異なる場合のみ、それを「前日順位」として退避してから今日の分で上書きする
    （同日中に複数回自動更新が走っても「前日」の意味がずれないようにするため）。"""
    today = _today_str()
    ranked = logic.sort_rows(_build_rank_rows(tickers))
    new_ranks = {r["ticker"]: i + 1 for i, r in enumerate(ranked)}
    try:
        current = store.get_json("rank_snapshot")
        if current and isinstance(current, dict) and current.get("date") and current.get("date") != today:
            store.set_json("rank_snapshot_prev", current)
        store.set_json("rank_snapshot", {"date": today, "ranks": new_ranks})
    except Exception as e:
        print(f"[WARN] rank_snapshotの更新に失敗しました: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Q1〜Q5判定(Twelve Data、quintile_logic.py)。IMPLEMENTATION_DESIGN_quintile_q1q5.md、
# および2026-09-16の本番実装レビューで確定した流れ(①〜⑩の判定ロジック自体は
# 2026-09-17のAlpha Vantage撤去作業でも一切変更していない):
# ① SPY取得 ② 当日のreference group取得 ③ ユーザー監視銘柄取得
# ④ 9特徴量計算 ⑤ pred_score計算 ⑥ reference pool更新
# ⑦ rolling 252日poolからpercentile境界計算 ⑧ Q1〜Q5判定
# ⑨ state history更新 ⑩ Redis保存
# ---------------------------------------------------------------------------

MAX_QUINTILE_STATE_HISTORY = 60

# Q5後5営業日固定クール(design 2026-09-18正式仕様、過去データによる検証済み)。
# Q1〜Q5判定ロジック(quintile_logic.py)・既存history・Q5シグナルには一切
# 使わない、追加のみのフィールド。新規Q5突入日(Day0)からの終値を積み上げる。
# 「現在Q5かどうか」とは別管理: 途中でQ4以下に戻っても・途中で再びQ5に
# なってもクールは継続・リセットされず、Day0からQ5_COOL_BUSINESS_DAYS営業日
# (=Day0〜Day5の計MAX_Q5_PRICE_PATH_DAYS件)に達したらクール終了として
# 追記を止める(先頭=Day0は上書きせず、末尾から切り捨てない)。クール終了後に
# 「前日Q5でない→当日Q5」の genuine な再突入が起きた場合のみ、新しいクール
# Day0としてリセットする。
Q5_COOL_BUSINESS_DAYS = 5
MAX_Q5_PRICE_PATH_DAYS = Q5_COOL_BUSINESS_DAYS + 1  # Day0〜Day5の6件

# Q5注意喚起(design 2026-09-18正式仕様)。Q5クールのDay5時点(Day1/Day3等の
# 中間値は使わない)のQ5起点騰落率がこの閾値以下の場合にのみ発生させる。
Q5_WARNING_TRIGGER_PCT = -7.5
# 発生した注意喚起は、現在のQ5クールの状態(新クール開始・クール②のDay5回復)
# とは独立に、発生日から最大この営業日数まで保持し、それ以降は自動的に解除
# する(過去データ検証の結果、次クールのDay5回復だけでは解除の根拠が弱いと
# 判断したため、固定日数での失効とした)。
Q5_WARNING_MAX_BUSINESS_DAYS = 40

# Q5中線割れ:購入中断 補助表示(design 2026-10-02ユーザー確定仕様)。
# Q1〜Q5判定ロジック・pred_score・history・Q5シグナル有効期限・q5_price_path・
# q5_warning・既存の購入判定/スコア/ランキングには一切関与しない、
# quintile:state:<TICKER>への追加フィールド(q5_midline_halt)のみ。
# 対象: Q5 Day0(_true_q5_day0と同じ起点)から8取引日(Day0〜Day7)以内に、
# 終値がSMA20(stock_logic.sma(closes, 20)、既存実装と同一の計算方法)を
# 初めて下回った場合にのみ有効化する(Day8以降の初回割れは対象外)。
Q5_MIDLINE_WATCH_DAYS = 7
# 解除条件: 終値がSMA20以上を何取引日連続で維持したら解除するか
# (回復当日を1日目として数える、ユーザー確定仕様2026-10-02)。
Q5_MIDLINE_UNLOCK_STREAK = 3


def remaining_td_budget():
    """(Twelve Dataの残りcredits, 本日の使用済みcredits) を返す。"""
    used = store.get_td_api_budget(_today_str())
    return td.DAILY_API_BUDGET - used, used


def _td_fetch_one(ticker, api_key, date_key):
    """Twelve Dataから1銘柄取得し、成功なら(dates, closes, volumes)、
    失敗ならNoneを返す。RATE_LIMIT発生時は第2戻り値をTrueにする
    (以降の新規Twelve Data呼び出しを中断すべきというシグナル)。第3戻り値は
    失敗時のtwelvedata_client.TdApiError.error_type(例: "INVALID_SYMBOL"、
    成功時や予期しない例外時はNone/"UNKNOWN")。第4戻り値は失敗時の詳細
    メッセージ(成功時はNone)、_mark_staleへそのまま渡してRedisに記録し、
    「原因不明」状態をなくすために使う(2026-09-17、SKHY/AXT障害調査を受けて
    追加)。使用credits(attempts)は必ずtd_api_budgetへ加算する。"""
    try:
        dates, closes, volumes, attempts = td.fetch_daily_series(ticker, api_key)
        store.incr_td_api_budget(date_key, attempts)
        return (dates, closes, volumes), False, None, None
    except td.TdApiError as e:
        store.incr_td_api_budget(date_key, e.attempts)
        print(f"[Q1-5][NG] {ticker}: {e.error_type} - {e.message}")
        return None, e.error_type == "RATE_LIMIT", e.error_type, e.message
    except Exception as e:
        print(f"[Q1-5][NG] {ticker}: 予期しないエラー: {e}", file=sys.stderr)
        return None, False, "UNKNOWN", str(e)


def _advance_q5_warning(prev_warning, price_path, just_completed_day5, date_key):
    """Q5クールDay5時点の注意喚起の発生・保持・失効を判定する(design 2026-09-18
    正式仕様、Redisアクセスなしの純粋関数)。「現在のQ5クール」(price_path)とは
    別状態として管理し、新しいQ5クールが始まっても、そのクールのDay5が-7.5%
    より上に回復しても解除しない。発生からQ5_WARNING_MAX_BUSINESS_DAYS営業日
    経過した時点でのみ自動的に解除する。既に有効な注意喚起がある間は、新たな
    Day5<=-7.5%が発生しても上書きしない(最初の発生情報をそのまま保持する。
    過去データ検証で、次クールのDay5回復だけでは解除の根拠が弱いと確認済み)。"""
    warning = dict(prev_warning) if prev_warning else None
    if warning is not None:
        warning["days_since_trigger"] = warning.get("days_since_trigger", 0) + 1
        if warning["days_since_trigger"] >= Q5_WARNING_MAX_BUSINESS_DAYS:
            warning = None

    if warning is None and just_completed_day5:
        day0_price = price_path[0].get("price")
        day5_price = price_path[Q5_COOL_BUSINESS_DAYS].get("price")
        if day0_price and day5_price:
            day5_return = (day5_price / day0_price - 1) * 100
            if day5_return <= Q5_WARNING_TRIGGER_PCT:
                warning = {
                    "triggered_date": date_key,
                    "triggered_return_pct": round(day5_return, 2),
                    "days_since_trigger": 0,
                }
    return warning


def _true_q5_day0(history):
    """historyから見て「現在のQ5クールが起点とすべき正しいDay0」を返す
    (=最後にQ5になった日)。app.pyの_compute_q5_signalが行っている
    「historyの末尾から最初に見つかるQ5エントリ」と全く同じ考え方を使い、
    q5_price_path側の起点をそれと同期させるための基準にする
    (2026-09-29追加、design: BE/MXL/AXTI/AEHRで確認された不整合の修正)。
    一度もQ5になっていなければNone。"""
    for h in reversed(history):
        if h.get("q") == "Q5":
            return h["date"]
    return None


def _reconcile_price_path(price_path, true_day0, trading_calendar, date_key, dates, closes):
    """q5_price_pathをtrue_day0(_true_q5_day0の結果)基準で整合させる
    (2026-09-29追加、design: BE/MXL/AXTI/AEHRで確認された不整合の修正)。

    - price_pathの先頭がtrue_day0と食い違っている場合(古いQ5クールの
      残骸をq5_price_pathだけが引きずっている、またはDay0自体の取得が
      その日失敗して記録されなかった場合)、price_pathを空にしてtrue_day0
      から作り直す。これにより、Q5再発時は必ず新しいDay0になり
      (q5_signalと起点ルールが揃う)、過去に生じた不整合データも次回の
      日次更新で自動的に復旧する。
    - true_day0からdate_key未満までの実取引日(trading_calendar基準)の
      うち、price_pathにまだ無い日を、dates/closes(当日取得できた銘柄の
      全期間終値、outputsize=full)から実際の終値を引いて補完する。ある
      銘柄の取得が特定の日だけ失敗しても、翌日以降にoutputsize=fullで
      再取得できれば、その抜けていた日の実際の終値からDay経過を復元できる
      (架空の値は作らない。dates内に見つからない日は補完せずそのまま
      スキップする)。
    - MAX_Q5_PRICE_PATH_DAYSを超えては補完しない(先頭=Day0は保持する)。
    - trading_calendar/dates/closesが利用できない場合(pool_history未初期化
      直後・当日の取得自体が失敗した等)は、起点の食い違いチェックだけを
      行い(架空の値を作れないため)、欠測補完は行わない。
    - historyは一切参照・変更しない(引数として読むだけ)。既存の
      current_q/previous_q/history/pred_scoreの計算・quintile_logic.py
      (Q1〜Q5判定ロジック本体)には一切影響しない、q5_price_path専用の
      整合処理。"""
    if true_day0 is None:
        return []
    if price_path and price_path[0]["date"] != true_day0:
        price_path = []
    if not trading_calendar or dates is None or closes is None:
        return price_path

    lo = bisect.bisect_left(trading_calendar, true_day0)
    hi = bisect.bisect_left(trading_calendar, date_key)  # 当日分はこの後の通常追記処理に任せる
    window = trading_calendar[lo:hi]
    have_dates = {e["date"] for e in price_path}
    date_to_close = dict(zip(dates, closes))

    filled = list(price_path)
    for d in window:
        if len(filled) >= MAX_Q5_PRICE_PATH_DAYS:
            break
        if d in have_dates:
            continue
        close = date_to_close.get(d)
        if close is None:
            continue  # この日の終値が無ければ復元しない(架空値は作らない)
        filled.append({"date": d, "price": close})
    filled.sort(key=lambda e: e["date"])
    return filled


def _q5_midline_days_elapsed(trading_calendar, day0_date, date_key):
    """day0_dateからdate_keyまでの取引日経過数を返す(Day0自身=0)。
    _reconcile_price_pathと同じbisectベースの数え方(trading_calendarに
    実際に存在する取引日だけを数える)。trading_calendarが空、または
    day0_date/date_keyがその範囲に無い場合はNoneを返す(架空の日数は
    作らない、呼び出し側は「判定不能」として扱う)。"""
    if not trading_calendar:
        return None
    lo = bisect.bisect_left(trading_calendar, day0_date)
    hi = bisect.bisect_right(trading_calendar, date_key)
    if hi <= lo:
        return None
    return (hi - lo) - 1


def _update_q5_midline_halt(prev_halt, day0_date, date_key, price, sma20, trading_calendar):
    """「Q5中線割れ：購入中断」補助表示の状態を更新する(design 2026-10-02
    ユーザー確定仕様、Redisアクセスなしの純粋関数)。Q1〜Q5判定ロジック・
    pred_score・history・q5_signal・q5_price_path・q5_warning・既存の
    購入判定/スコア/ランキングには一切影響しない、quintile:state:<TICKER>
    への追加フィールド(q5_midline_halt)の計算のみ。

    仕様(ユーザー確定、2026-10-02):
    - 対象: day0_date(_true_q5_day0と同じ、「最後にQ5になった日」)から
      Q5_MIDLINE_WATCH_DAYS(7)取引日以内、すなわちDay0〜Day7の計8取引日
      以内に、終値がSMA20を初めて下回った場合にのみ"active"になる
      (Day8以降に初めて割れた場合は対象外、新規に購入中断にしない)。
    - 一度activeになったら、終値がSMA20以上をQ5_MIDLINE_UNLOCK_STREAK
      (3)取引日連続で維持するまでactiveのまま維持する。現在のQ(Q4以下
      への降格)・q5_signalの有効期限(失効)・新しいQ5サイクルの開始の
      いずれにも影響されない(ここでは一切参照しない。activeである間は
      Day0〜Day7の判定自体を行わないため、新しいサイクルが始まっても
      既存のactive状態を勝手にリセットしない)。
    - 連続維持日数(recover_streak)は、解除前に終値が再びSMA20を下回ると
      0にリセットし、次に終値がSMA20以上に戻った日を1日目として数え直す。
    - 解除(active=False)した後は、新たにDay0〜Day7以内の初回割れが
      発生するまで再度activeにはならない。
    - price/sma20のいずれかが欠測(当日の取得失敗・SMA20算出に必要な
      20日分の価格履歴が無い等のデータ不足)の場合は、状態を一切変更
      せず前回の状態をそのまま返す(誤って解除・リセットしない、
      beta/q5_warning等ほかのフィールドと同じ「前回状態を保つ」方針)。
    """
    halt = dict(prev_halt) if prev_halt else {
        "active": False, "break_date": None,
        "recover_streak": 0, "recovered_since": None,
        "unlock_streak_required": Q5_MIDLINE_UNLOCK_STREAK,
    }

    if price is None or sma20 is None:
        return halt

    above = price >= sma20

    if halt.get("active"):
        if above:
            halt["recover_streak"] = halt.get("recover_streak", 0) + 1
            if halt["recover_streak"] == 1:
                halt["recovered_since"] = date_key
        else:
            halt["recover_streak"] = 0
            halt["recovered_since"] = None

        if halt["recover_streak"] >= Q5_MIDLINE_UNLOCK_STREAK:
            halt["active"] = False
            halt["break_date"] = None
            halt["recover_streak"] = 0
            halt["recovered_since"] = None
    elif not above and day0_date is not None and trading_calendar:
        days_elapsed = _q5_midline_days_elapsed(trading_calendar, day0_date, date_key)
        if days_elapsed is not None and 0 <= days_elapsed <= Q5_MIDLINE_WATCH_DAYS:
            halt["active"] = True
            halt["break_date"] = date_key
            halt["recover_streak"] = 0
            halt["recovered_since"] = None

    halt["last_updated"] = date_key
    return halt


def _update_quintile_state(ticker, score, bounds, date_key, price=None, beta=None,
                            trading_calendar=None, dates=None, closes=None):
    """ユーザー監視銘柄1件のQ状態を判定し、状態が変化した場合のみ履歴に追記する。
    「売り」「失敗」等の否定的な意味は一切持たせず、単なる状態記録として保存する
    (design 13の方針)。current_q/previous_q/history/pred_scoreの計算は無変更。

    2026-09-25追加: beta(表示専用、252営業日ローリングβ)。Q1〜Q5判定
    (score/bounds/assign_quintile)には一切使わない、new_stateへの追記のみ。
    その日SPYが取得できずbeta計算に失敗した場合はNoneが渡ってくるが、その
    場合は前回値をそのまま保持する(1日の取得失敗でβ表示が消えないように
    するため、q5_warning等ほかのフィールドと同じ「前回状態を保つ」方針)。

    2026-09-18追加、同日に5営業日固定クール仕様として正式化: q5_price_path
    (Q5「経過状態」表示専用、design参照)。「現在Q5かどうか」と「Q5後5営業日
    のクール」は別管理: Day0〜Day5(MAX_Q5_PRICE_PATH_DAYS件)に達したら
    クールは終了し、それ以上は追記しない(先頭=Day0は上書き・切り捨てしない)。
    Day5に到達した回だけ、_advance_q5_warningでその時点のQ5起点騰落率を見て
    注意喚起(q5_warning)の発生を判定する。q5_warningはクールの状態とは独立に
    保持・失効する(design参照)。

    2026-09-29改定(design: BE/MXL/AXTI/AEHRで確認された不整合の修正):
    「Q4以下に戻ってもクールは継続」という点は変更しないが、「途中で再び
    Q5になった場合」は、app.py側のQ5シグナル(_compute_q5_signal)と方針を
    統一し、常に新しいDay0としてリセットするように改めた(以前は、直前の
    クールが未完了〈Day5未到達〉ならリセットしない仕様だったが、これが
    「Q5状態管理(q5_price_path)」と「Q5シグナル(q5_signal)」とで再発時の
    起点が食い違う原因になっていたため)。また、取得失敗で特定の日だけ
    q5_price_pathへの追記が抜けた場合に、翌日以降のoutputsize=full取得
    (dates/closes)を使って実際の終値からその日を復元できるようにした
    (_reconcile_price_path参照)。trading_calendar/dates/closesが渡されない
    場合(pool_history未初期化時・単体呼び出し等)は、起点の食い違いだけを
    修正し、欠測補完は行わない従来同等の縮退動作にフォールバックする。
    いずれもapp.py側の表示専用ロジックが読むだけで、quintile_logic.py・
    history・pred_score・既存のQ5シグナル・Q5警告ロジックには一切影響しない。"""
    q = quintile_logic.assign_quintile(score, bounds)
    prev_state = store.get_quintile_state(ticker) or {}
    prev_q = prev_state.get("current_q")
    history = list(prev_state.get("history", []))

    if not history or prev_q != q:
        history.append({"date": date_key, "q": q})
        history = history[-MAX_QUINTILE_STATE_HISTORY:]

    prev_price_path = list(prev_state.get("q5_price_path", []))
    was_complete_before = len(prev_price_path) >= MAX_Q5_PRICE_PATH_DAYS

    true_day0 = _true_q5_day0(history)
    price_path = _reconcile_price_path(prev_price_path, true_day0, trading_calendar, date_key, dates, closes)

    # Q5中線割れ:購入中断 補助表示(design 2026-10-02)。true_day0はq5_price_path
    # と同一の起点(_true_q5_day0)を再利用し、起点のずれが生じないようにする。
    # SMA20はquintile_logic.compute_features/stock_logic.smaと同一の計算方法
    # (closesの末尾20件の単純平均)。closesが無い(当日未取得)日はsma20=Noneと
    # なり、_update_q5_midline_halt側で「データ不足」として状態を変更しない。
    sma20_today = logic.sma(closes, 20) if closes else None
    midline_halt = _update_q5_midline_halt(
        prev_state.get("q5_midline_halt"), true_day0, date_key, price, sma20_today, trading_calendar,
    )

    if price and (q == "Q5" or price_path) and len(price_path) < MAX_Q5_PRICE_PATH_DAYS:
        if not price_path or price_path[-1]["date"] != date_key:
            price_path.append({"date": date_key, "price": price})

    just_completed_day5 = (not was_complete_before) and len(price_path) == MAX_Q5_PRICE_PATH_DAYS
    warning = _advance_q5_warning(prev_state.get("q5_warning"), price_path, just_completed_day5, date_key)

    new_state = {
        "current_q": q,
        "previous_q": prev_q,
        "pred_score": score,
        "history": history,
        "q5_price_path": price_path,
        "q5_warning": warning,
        "q5_midline_halt": midline_halt,
        "beta": beta if beta is not None else prev_state.get("beta"),
        "last_updated": date_key,
    }
    store.set_quintile_state(ticker, new_state)
    return new_state


def _mark_quintile_state_insufficient(ticker, date_key, data_days, data_days_required):
    """データ不足(2026-09-22、SKHY調査を受けて追加)。ma200_devを含む9特徴量が
    全て計算可能になるMIN_HISTORY_FOR_FULL_FEATURES件に満たない銘柄について、
    quintile_logic.assign_quintileを一切呼ばずに「判定保留」であることだけを
    Redisに記録する。

    既存のcurrent_q/previous_q/history/q5_price_path/q5_warning/q5_midline_halt
    はすべてそのまま保持する(読み込んだprev_stateをコピーし、current_q・data_status・
    data_days・data_days_required・last_updatedだけを上書きする)。これにより、
    以前正常にQ5だった銘柄の履歴・クール・警告が、このデータ不足処理によって
    壊れることはない。app.py側の_build_quintile_viewはcurrent_q=Noneを
    既存のpending判定と同じ経路で扱う(表示層は無変更)。"""
    prev_state = store.get_quintile_state(ticker) or {}
    new_state = dict(prev_state)
    new_state["current_q"] = None
    new_state["data_status"] = "insufficient"
    new_state["data_days"] = data_days
    new_state["data_days_required"] = data_days_required
    new_state["last_updated"] = date_key
    store.set_quintile_state(ticker, new_state)
    return new_state


# BACKFILL_DAYS_BACK: 新規銘柄追加時に遡って再計算する日数(今日を含めて
# BACKFILL_DAYS_BACK+1日分。app.pyの表示が「今日・昨日・2〜5日前」の6列
# であることに合わせている)。
BACKFILL_DAYS_BACK = 5


def _compute_backfill_day(ticker, dates, closes, volumes, target_idx, pool_history):
    """1日分のQ1〜Q5を、その日"以前"のデータだけを使ってlook-ahead biasなしで
    計算する(backfill_quintile_history_for_new_tickerが元々1つのループの中で
    行っていた計算をそのまま関数化しただけで、計算内容・スキップ条件は一切
    変更していない)。

    2026-09-22追加: 戻り値に"price"(その日の終値)を加えた。Q5バックフィル
    (過去のQ5エントリー日からq5_price_pathを復元する機能)がこの値を使う。
    既存の呼び出し側(dateとqとscoreだけを読む)には影響しない。

    戻り値: {"date":str,"q":"Q1"〜"Q5","score":float,"price":float}。
    株価データ・pool_history・特徴量/スコア計算のいずれかが不足/失敗した日は
    Noneを返す(この日のQは推測しない)。"""
    n_dates = len(dates)
    if target_idx < 0 or target_idx >= n_dates:
        return None  # その日の株価データ自体がまだ存在しない(上場間もない等)
    target_date = dates[target_idx]

    # データ不足(2026-09-22、SKHY調査を受けて追加): ma200_devを含む9特徴量が
    # 全て計算可能になるMIN_HISTORY_FOR_FULL_FEATURES(200件)に満たない日は、
    # 中央値補完だらけの不正確なQを確定させず、この日のQ判定自体をスキップする
    # (Q1〜Q5計算ロジック・9特徴量の定義自体は無変更、判定を行うかどうかの
    # ガードを追加しただけ)。
    if not quintile_logic.has_min_history_for_quintile(target_idx + 1):
        return None

    try:
        features = quintile_logic.compute_features(
            dates[: target_idx + 1], closes[: target_idx + 1], volumes[: target_idx + 1],
        )
    except Exception as e:
        print(f"[Q1-5][WARN] {ticker} {target_date}: 特徴量計算に失敗、この日はスキップ: {e}")
        return None

    # その日"以前"の日付だけにpool_historyを絞り込む(未来のプール更新は
    # 一切参照しない。dates文字列はYYYY-MM-DD形式のため単純な文字列比較で
    # 時系列順と一致する、既存コード各所と同じ前提)。
    filtered_scores_by_date = {
        d: v for d, v in pool_history["scores_by_date"].items() if d <= target_date
    }
    if not filtered_scores_by_date:
        return None  # その日の時点でプールにまだ何も蓄積されていない
    filtered_pool_history = {
        "dates": sorted(filtered_scores_by_date.keys()),
        "scores_by_date": filtered_scores_by_date,
    }
    bounds = quintile_logic.pool_percentile_bounds(filtered_pool_history)
    if bounds is None:
        return None

    try:
        score = quintile_logic.knn_predict_score(features)
    except Exception as e:
        print(f"[Q1-5][WARN] {ticker} {target_date}: pred_score計算に失敗、この日はスキップ: {e}")
        return None

    q = quintile_logic.assign_quintile(score, bounds)
    return {"date": target_date, "q": q, "score": score, "price": closes[target_idx]}


def _find_recent_q5_entry_index(computed, entry_before_window):
    """computed(古い→新しい順、backfill対象の直近BACKFILL_DAYS_BACK+1日分)の中から、
    「前日がQ5ではなく、当日Q5になった日」(design 2026-09-22ユーザー確定仕様)を
    最も新しいものから探して、そのcomputed内のindexを返す(見つからなければNone)。

    computedの最古日(index 0)については、computed自体にその前日の情報が
    無いため、entry_before_window(computedのさらに1日前を計算した結果、
    Noneの場合もある)を仮の「前日」として使う。entry_before_windowが
    Noneの場合、prev_qはNone扱いとなり(=Q5ではない扱い)、これは
    _update_quintile_stateがprev_q未取得時にNoneをQ5でないものとして扱う
    既存の慣習(is_new_cool判定等)と同じ考え方。

    複数のQ5エントリーがcomputed内に存在する場合は、最も新しい(直近の)ものを
    採用する(「新しいQ5エントリーが発生した場合は、その日を新しいDay0として
    リセットする」というユーザー仕様通り)。"""
    for i in range(len(computed) - 1, -1, -1):
        prev_q = computed[i - 1]["q"] if i > 0 else (entry_before_window["q"] if entry_before_window else None)
        if computed[i]["q"] == "Q5" and prev_q != "Q5":
            return i
    return None


def backfill_quintile_history_for_new_ticker(ticker, api_key):
    """新規追加銘柄について、過去BACKFILL_DAYS_BACK日分(当日含め最大6日分)の
    Q1〜Q5をlook-ahead biasなしで再計算し、quintile:state:<TICKER>の初期
    historyとして登録する(design: 2026-09-17ユーザー確定仕様)。

    2026-09-17のAlpha Vantage撤去に伴い、この関数内で取得したTwelve Data
    株価データを使って旧指標(stock_logic.build_result)も同じタイミングで
    計算・保存するようになった(追加のAPI呼び出しは発生しない、詳細は
    関数内の該当コメント参照)。これにより銘柄追加直後から旧指標・Q1〜5の
    両方がready状態で表示される。この部分の追加はQ1〜Q5計算ロジック
    (以下のlook-ahead bias対策・pool_history絞り込み等)には一切影響しない。

    既存のQ1〜Q5判定ロジック(quintile_logic.py)・Q5シグナル有効期限ロジック
    (app.py)・redis_store.pyは一切変更しない。ここで作るhistoryは、
    _update_quintile_stateが日々追記していくものと全く同じ形式
    ({"date": "YYYY-MM-DD", "q": "Q1"〜"Q5"})であるため、翌日以降の通常の
    日次バッチ(run_quintile_refresh)にそのまま引き継がれ、app.py側の
    Q5シグナル計算(_compute_q5_signal)もそのまま正しく動作する。

    Twelve Dataへの追加API呼び出しは、対象銘柄の株価取得1回のみ
    (twelvedata_client.fetch_daily_series経由の_td_fetch_one、既存の
    ユーザー監視銘柄取得と同一関数)。SPYの追加取得は行わない(design方針
    「1銘柄1回の追加取得で済ませる」を優先するため)。そのためrel_strength_spy
    特徴量は過去日分についてはNoneのまま渡し、既存のTRAIN中央値補完
    (quintile_logic._standardize)に委ねる。翌日以降の通常の日次バッチでは
    SPYが毎日取得されるため、rel_strength_spy込みの完全な計算に自然に
    引き継がれる(この関数はあくまで初期表示のための「橋渡し」)。

    look-ahead bias対策(Q1〜Q5計算ロジック本体、2026-09-17以降無変更):
    - 各日の9特徴量は、その日"以前"の株価データだけ(closes等をその日の
      インデックスまでスライス)を使って計算する(quintile_logic.compute_features
      をそのまま呼ぶだけ、ロジック自体は無変更)。
    - 分位境界は、Redisにすでに保存されている本番の実データ
      quintile:pool_history(日次バッチが実際に記録してきた履歴)を、
      その日"以前"の日付だけに絞り込んでquintile_logic.pool_percentile_bounds
      に渡す。これにより「その日に実際に存在した境界」を、未来のプール
      更新を一切参照せずに再現する。
    - 必要な株価データ・プール履歴が不足している日は、その日のQを推測せず
      スキップする(historyに追加しない。表示側は既存のresolveQForDateが
      「データがない日」を「—」として扱う)。

    2026-09-17(2回目の改修、SKHY/AXT障害調査を受けて): 旧指標・Q1〜5の
    それぞれについて「既に正常なデータがあるか」を個別に見て、壊れている側
    だけを取得・再構築できるようにした(「追加→旧指標だけ失敗→削除→
    再追加」で自己修復できることが目的)。

    2026-09-18(3回目の改修、ユーザー確認済み): 「Q1〜5・旧指標とも既に
    正常」なケースでもTwelve Data取得自体は毎回必ず1回行うように変更した
    (delete→再addのたびに旧指標側の表示が古いまま固定されてしまう問題を
    防ぐため)。ただし取得した結果の使い方は非対称にした:
    - 旧指標(legacy record)は、fetchが成功するたびに常に作り直す
      (「壊れていない状態を保つ」以上の意味を持たない単純な最新表示のため、
      既存データがあっても上書きして構わない)。
    - Q1〜5履歴(quintile:state)は、既に正常なhistoryがある場合は一切
      上書きしない(look-ahead biasなしで積み上げてきた履歴を、単なる
      鮮度目的で不要に再計算・上書きするべきではないため)。
    結果として、Twelve Data呼び出しは「APIキー未設定/予算切れ/取得失敗」
    以外の全ケースで必ずちょうど1回だけ発生する。

    2026-09-22(4回目の改修、ユーザー確定仕様): Q5バックフィル(過去エントリー
    復元)を追加。新規銘柄追加日を勝手にQ5 Day0にはしない。過去
    BACKFILL_DAYS_BACK(5)取引日以内に実際のQ5エントリー(前日Q5でない→
    当日Q5)があれば、そのエントリー日をDay0としてq5_price_pathを実際の
    取引日の終値で再構成する(休場日はTwelve Dataのdates自体に存在しない
    ため自動的にスキップされる、曜日・祝日の個別判定は行わない)。Day5まで
    データがあれば通常の日次更新と全く同じ条件(Day5<=-7.5%)でq5_warningも
    復元する。過去5取引日以内にQ5エントリーが無ければq5_price_path/
    q5_warningは一切作らない(通常の現在Q判定のみ)。9特徴量・Q1〜Q5判定
    ロジック・既存の5営業日固定クール仕様・警告条件・40取引日失効は無変更。
    詳細は_compute_backfill_day/_find_recent_q5_entry_index参照。

    戻り値: {"ok": bool, "error_type": str|None, "message": str|None}
      ok: 呼び出し後、旧指標・Q1〜5のうち少なくとも一方が(既存データ含め)
          正常な状態であればTrue。
      error_type: Twelve Data取得自体が失敗した場合のtwelvedata_client.
          TdApiErrorのerror_type(例: "INVALID_SYMBOL")。取得を試みな
          かった場合・取得に成功した場合はNone。
      message: 呼び出し元(app.py)がそのままユーザーに提示してよい説明文。
          Noneの場合は呼び出し元がok/error_typeから組み立てる。
    """
    if not api_key:
        print(f"[Q1-5][WARN] {ticker}: APIキー未指定のため、過去分バックフィルをスキップします。")
        return {"ok": False, "error_type": None, "message": "APIキー未設定のため、次回の自動更新までデータは表示されません。"}

    existing_state = store.get_quintile_state(ticker)
    have_quintile_history = bool(existing_state and existing_state.get("history"))

    existing_record = store.get_ticker_record(ticker)
    have_legacy_record = bool(existing_record and existing_record.get("last_trade_date"))
    have_both_already = have_quintile_history and have_legacy_record

    date_key = _today_str()
    budget_left, _ = remaining_td_budget()
    if budget_left <= 0:
        print(f"[Q1-5][WARN] {ticker}: Twelve Data予算切れのため、取得をスキップします。")
        if have_both_already:
            return {
                "ok": True, "error_type": None,
                "message": "既存のデータを表示します（本日のTwelve Data利用上限に達したため、最新化は見送りました）。",
            }
        return {
            "ok": have_quintile_history or have_legacy_record, "error_type": None,
            "message": "本日のTwelve Data利用上限に達しました。次回の自動更新をお待ちください。",
        }

    result, _hit_rate_limit, error_type, _message = _td_fetch_one(ticker, api_key, date_key)
    if result is None:
        if have_both_already:
            return {
                "ok": True, "error_type": None,
                "message": "既存のデータを表示します（今回の取得には失敗しました）。",
            }
        if have_quintile_history or have_legacy_record:
            # 片方だけでも既存データがあるなら、それを表示できる旨を明示する
            # ("最新データを取得しました"という誤った表示を避けるため、messageを
            # 必ず埋める。error_typeはINVALID_SYMBOL等の判別に使われるが、既存
            # データがある以上「待っても直らない」わけではないのでmessage優先)。
            return {
                "ok": True, "error_type": error_type,
                "message": "既存の一部データを表示します（今回の取得には失敗しました）。",
            }
        return {"ok": False, "error_type": error_type, "message": None}
    dates, closes, volumes = result

    # 旧指標(stock_logic、株価・RSI・前日比・1ヶ月騰落率)は、fetchが成功
    # した今回は常に作り直す(design 2026-09-18、上のdocstring参照)。
    # 失敗してもQ1〜5側のバックフィル処理は継続する(このtry/exceptの外には
    # 一切影響を及ぼさない、Q1〜5計算ロジック自体には触れていない)。
    try:
        legacy_record = logic.build_result(ticker, dates, closes, volumes)
        legacy_record["fetched_at"] = _now_iso()
        legacy_record["is_stale"] = False
        legacy_record["last_error"] = None
        legacy_record["last_error_message"] = None
        legacy_record["last_error_at"] = None
        store.set_ticker_record(ticker, legacy_record)
        have_legacy_record = True
        print(f"[OK] {ticker}: {legacy_record['judgment']}（最終取引日 {legacy_record['last_trade_date']}）")
    except Exception as e:
        _mark_stale(ticker, "UNKNOWN", str(e))
        print(f"[WARN] {ticker}: 旧指標の計算・保存に失敗しました: {e}", file=sys.stderr)

    if have_quintile_history:
        # Q1〜5側は既に正常なため、既存historyには一切手を加えない(design
        # 2026-09-18、上のdocstring参照)。旧指標側の結果だけを反映して終える。
        # okはTrue固定(Q1〜5は表示可能な状態にあるため。旧指標の修復が
        # 今回失敗していても、それはis_stale/last_errorとして別途記録済みで、
        # このbackfill呼び出し全体を「失敗」とは呼ばない)。
        return {"ok": True, "error_type": None, "message": None}

    pool_history = store.get_pool_history()
    if not pool_history or not pool_history.get("scores_by_date"):
        print(f"[Q1-5][WARN] {ticker}: pool_historyが未初期化のため、過去分の再計算をスキップします。")
        return {"ok": have_legacy_record, "error_type": None, "message": None}

    n_dates = len(dates)
    computed = []  # [{"date":..., "q":..., "score":..., "price":...}, ...] 古い→新しい順
    for k in range(BACKFILL_DAYS_BACK, -1, -1):
        entry = _compute_backfill_day(ticker, dates, closes, volumes, n_dates - 1 - k, pool_history)
        if entry is not None:
            computed.append(entry)

    # Q5エントリー探索用に、6日分の探索窓("今日"含め6日、BACKFILL_DAYS_BACK+1件)の
    # さらに1日前(k=BACKFILL_DAYS_BACK+1)も計算しておく(design 2026-09-22、下記
    # 「Q5バックフィル(過去エントリー復元)」参照)。computed(表示・history用の
    # 6日分)には一切混ぜず、あくまで「computedの最古日の前日がQ5だったか」を
    # 判定するためだけに使う。
    entry_before_window = _compute_backfill_day(
        ticker, dates, closes, volumes, n_dates - 1 - (BACKFILL_DAYS_BACK + 1), pool_history,
    )

    if not computed:
        if not quintile_logic.has_min_history_for_quintile(n_dates):
            # データ不足(2026-09-22、SKHY調査を受けて追加): 中央値補完だらけの
            # 不正確なQ1〜5を書き込まず、「データ不足」であることだけを記録する。
            # have_quintile_historyがFalseの場合にのみこの分岐へ来るため
            # (上のhave_both_already早期returnを参照)、既存の正常なhistory・
            # q5_price_path・q5_warningを上書きする心配はない。
            store.set_quintile_state(ticker, {
                "current_q": None,
                "data_status": "insufficient",
                "data_days": n_dates,
                "data_days_required": quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES,
                "last_updated": dates[-1],
            })
            print(
                f"[Q1-5][PENDING] {ticker}: データ不足のためQ判定を保留します"
                f"({n_dates}/{quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES}営業日)。"
            )
        else:
            print(f"[Q1-5][WARN] {ticker}: 過去分を1日も再計算できませんでした(データ不足以外の理由)。")
        return {"ok": have_legacy_record, "error_type": None, "message": None}

    # 既存history形式(変化した日だけを記録)に合わせて圧縮する。
    compact_history = []
    for entry in computed:
        if compact_history and compact_history[-1]["q"] == entry["q"]:
            continue
        compact_history.append({"date": entry["date"], "q": entry["q"]})

    last_entry = computed[-1]
    new_state = {
        "current_q": last_entry["q"],
        "previous_q": compact_history[-2]["q"] if len(compact_history) > 1 else None,
        "pred_score": last_entry["score"],
        "history": compact_history,
        "last_updated": last_entry["date"],
    }

    # Q5バックフィル(過去エントリー復元、design 2026-09-22ユーザー確定仕様)。
    # 新規銘柄追加日を勝手にQ5 Day0にはしない。過去BACKFILL_DAYS_BACK取引日
    # (=computed全体)以内に実際のQ5エントリー(前日Q5でない→当日Q5)があれば、
    # そのエントリー日をDay0としてq5_price_pathを実際の取引日の終値で復元する
    # (休場日はdates自体に存在しないため自動的に読み飛ばされる)。無ければ
    # q5_price_path/q5_warningは一切作らない(=通常の現在Q判定のみを表示する、
    # 既存の非Q5新規銘柄と同じ状態)。
    q5_entry_idx = _find_recent_q5_entry_index(computed, entry_before_window)
    if q5_entry_idx is not None:
        price_path = [
            {"date": e["date"], "price": e["price"]} for e in computed[q5_entry_idx:]
        ]
        just_completed_day5 = len(price_path) == MAX_Q5_PRICE_PATH_DAYS
        warning = None
        if just_completed_day5:
            # Day5まで既にデータがある場合のみ、通常の日次更新と全く同じ
            # _advance_q5_warning(Day5 <= -7.5%の条件・40取引日失効)を適用する。
            # 新規銘柄のためprev_warning=None(それ以前の警告状態は存在しない)。
            warning = _advance_q5_warning(None, price_path, True, price_path[-1]["date"])
        new_state["q5_price_path"] = price_path
        new_state["q5_warning"] = warning
        print(
            f"[Q1-5][OK] {ticker}: 過去のQ5エントリー({price_path[0]['date']})からq5_price_pathを復元"
            f"(Day0〜Day{len(price_path) - 1}、warning={'あり' if warning else 'なし'})"
        )

    store.set_quintile_state(ticker, new_state)
    print(
        f"[Q1-5][OK] {ticker}: バックフィル完了({len(computed)}/{BACKFILL_DAYS_BACK + 1}日分計算、"
        f"history {len(compact_history)}件、current_q={last_entry['q']})"
    )
    return {"ok": True, "error_type": None, "message": None}


def run_quintile_refresh(api_key, watchlist):
    """Q1〜Q5判定の日次処理本体。1銘柄の取得失敗が他銘柄の処理を止めない、
    未来データを一切参照しない、という既存run_refreshと同じ設計原則を踏襲する。
    戻り値: {"fetched": [...], "failed": [...], "rate_limited": bool} または、
    APIキー未設定/予算切れで何もしなかった場合は None。
    """
    if not api_key:
        print("[Q1-5][WARN] TWELVEDATA_API_KEYが未設定のため、Q1〜Q5処理をスキップします。")
        return None

    date_key = _today_str()
    budget_left, _ = remaining_td_budget()
    if budget_left <= 0:
        print("[Q1-5][WARN] Twelve Data予算を使い切ったため、Q1〜Q5処理をスキップします。")
        return None

    fetched = {}
    failed = []
    rate_limited = False

    # ① SPY取得(rel_strength_spy特徴量の鮮度を保つため、ローテーションと無関係に毎日取得)
    budget_left, _ = remaining_td_budget()
    if budget_left > 0:
        result, hit_rate_limit, _error_type, _message = _td_fetch_one("SPY", api_key, date_key)
        if result:
            fetched["SPY"] = result
        else:
            failed.append("SPY")
            rate_limited = rate_limited or hit_rate_limit

    # ② 当日のreference group取得(SPYを除いた48銘柄、14日ローテーション)
    if not rate_limited:
        group_idx = quintile_logic.rotation_group_for_date(date_key)
        rotation_tickers = quintile_logic.tickers_for_group(group_idx)
        print(f"[Q1-5][INFO] 本日のローテーショングループ: {group_idx} ({rotation_tickers})")
        for ticker in rotation_tickers:
            if rate_limited:
                break
            budget_left, _ = remaining_td_budget()
            if budget_left <= 0:
                print("[Q1-5][WARN] Twelve Data予算切れのため、ローテーション取得を中断します。")
                break
            result, hit_rate_limit, _error_type, _message = _td_fetch_one(ticker, api_key, date_key)
            if result:
                fetched[ticker] = result
            else:
                failed.append(ticker)
                rate_limited = rate_limited or hit_rate_limit

    # ③ ユーザー監視銘柄取得(最大15、毎日)
    if not rate_limited:
        for ticker in watchlist[: logic.MAX_TICKERS]:
            if rate_limited:
                break
            budget_left, _ = remaining_td_budget()
            if budget_left <= 0:
                print("[Q1-5][WARN] Twelve Data予算切れのため、ユーザー監視銘柄の取得を中断します。")
                break
            if ticker in fetched:
                continue  # 参照母集団のローテーションと重複している場合は再取得しない
            result, hit_rate_limit, error_type, message = _td_fetch_one(ticker, api_key, date_key)
            if result:
                fetched[ticker] = result
            else:
                failed.append(ticker)
                rate_limited = rate_limited or hit_rate_limit
                # 2026-09-17(SKHY/AXT障害調査を受けて): Alpha Vantage撤去(3c3f3f5)で
                # 落ちていた呼び出し。旧指標側の株価取得(=ここ)自体が失敗した場合、
                # 失敗理由をticker_recordに書き残さないと、is_stale/last_errorが
                # ずっと空のまま(=UIに「⚪ データなし」しか出ず原因不明)になる。
                # Q1〜5側の計算・保存には一切影響しない(このfor文はraw_watchlist_data
                # 経由で旧指標に渡す生データ取得のみを担当)。
                _mark_stale(ticker, error_type, message)

    if rate_limited:
        print("[Q1-5][WARN] Twelve DataがRATE_LIMITを返したため、以降の新規取得を中断しました。")

    # ④⑤ 9特徴量・pred_score計算(取得できた銘柄のみ、未来データは一切参照しない)
    spy_dates, spy_closes = None, None
    if "SPY" in fetched:
        spy_dates, spy_closes, _ = fetched["SPY"]

    scores_today = {}
    for ticker, (dates, closes, volumes) in fetched.items():
        try:
            features = quintile_logic.compute_features(dates, closes, volumes, spy_dates, spy_closes)
            score = quintile_logic.knn_predict_score(features)
        except Exception as e:
            print(f"[Q1-5][NG] {ticker}: 特徴量/pred_score計算に失敗: {e}", file=sys.stderr)
            continue
        scores_today[ticker] = score
        # ⑥ reference pool(quintile:refpool:<TICKER>)更新。当日取得できた銘柄のみ。
        store.set_refpool_score(ticker, {
            "pred_score": score, "last_updated": date_key, "features": features,
        })

    # 実取引日ベース化(2026-09-22正式仕様、土日・米国市場休場日調査を受けて変更)。
    # 「今日」はTwelve Dataが実際に返した最新取引日(dates[-1])とし、曜日・祝日の
    # 個別判定は一切行わない(GitHub Actionsが土日にも起動する設計はそのまま、
    # Twelve Data側が新しい取引日を返さないことを利用してskipする)。
    # SPYが取得できていればSPYの最終日を優先し(毎日必ず取得する対象のため最も
    # 信頼できる)、SPYが取得できなかった日は取得できた他の銘柄の最終日で代用する。
    # 1件も取得できなかった日(全銘柄失敗・レート制限等)はactual_trading_date=None
    # となり、以降のpool_history・Q1〜5状態更新を丸ごとskipする(新しい実データが
    # 何もない以上、古い日付キーで空更新するより安全なため)。
    actual_trading_date = None
    if "SPY" in fetched:
        actual_trading_date = fetched["SPY"][0][-1]
    else:
        for _dates, _closes, _volumes in fetched.values():
            actual_trading_date = _dates[-1]
            break

    pool_history = store.get_pool_history()
    prev_trading_date = None
    if pool_history and pool_history.get("dates"):
        prev_trading_date = pool_history["dates"][-1]

    quintile_updated = False
    if actual_trading_date is not None and actual_trading_date != prev_trading_date:
        quintile_updated = True

        # ⑦ rolling 252日pool更新。REFERENCE_UNIVERSE全49銘柄について、当日取得分は
        # 新しい値を、それ以外は直近のrefpoolキャッシュ値(前回そのティッカーが
        # ローテーション/ユーザー監視で取得された時点の値)を使う。
        scores_by_ticker = {}
        for sym in quintile_logic.REFERENCE_UNIVERSE:
            if sym in scores_today:
                scores_by_ticker[sym] = scores_today[sym]
            else:
                cached = store.get_refpool_score(sym)
                if cached and cached.get("pred_score") is not None:
                    scores_by_ticker[sym] = cached["pred_score"]

        pool_history = quintile_logic.update_pool_history(pool_history, actual_trading_date, scores_by_ticker)
        store.set_pool_history(pool_history)

        bounds = quintile_logic.pool_percentile_bounds(pool_history)

        # q5_price_pathの欠測補完・起点整合(2026-09-29追加)に使う実取引日
        # カレンダー。pool_historyは直上でその日の分まで更新済みのため、
        # ここで1回だけソートして全ティッカーの_update_quintile_state呼び出しに
        # 使い回す(app.py側の_trading_days_calendar()と同じデータソース)。
        trading_calendar_dates = sorted(pool_history.get("dates", []))

        # ⑧⑨ ユーザー監視銘柄のQ1〜Q5判定・状態履歴更新 ⑩ Redis保存(set_quintile_state内で実施)
        if bounds is not None:
            for ticker in watchlist[: logic.MAX_TICKERS]:
                score = scores_today.get(ticker)
                if score is None:
                    cached = store.get_refpool_score(ticker)
                    score = cached.get("pred_score") if cached else None
                if score is None:
                    continue  # まだ一度もTwelve Dataで取得できていない銘柄は判定待ちのまま

                # データ不足(2026-09-22、SKHY調査を受けて追加): 当日実際に取得できた
                # 銘柄について、ma200_devを含む9特徴量が全て計算可能になる
                # MIN_HISTORY_FOR_FULL_FEATURES(200件)に満たない場合は、中央値補完
                # だらけの不正確なQで確定させず「データ不足」として判定を保留する。
                # 既存のcurrent_q/history/q5_price_path/q5_warningは一切上書きしない。
                if ticker in fetched:
                    ticker_dates = fetched[ticker][0]
                    if not quintile_logic.has_min_history_for_quintile(len(ticker_dates)):
                        _mark_quintile_state_insufficient(
                            ticker, actual_trading_date, len(ticker_dates),
                            quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES,
                        )
                        print(
                            f"[Q1-5][PENDING] {ticker}: データ不足のためQ判定を保留"
                            f"({len(ticker_dates)}/{quintile_logic.MIN_HISTORY_FOR_FULL_FEATURES}営業日)"
                        )
                        continue

                # q5_price_path用: 当日実際に取得できた終値があれば渡す(取得できて
                # いない日=fetched未成功の日は前回値のままpriceを渡さず、記録しない)。
                price_today = fetched[ticker][1][-1] if ticker in fetched else None
                # q5_price_pathの欠測補完(2026-09-29追加)用: 当日取得できた銘柄の
                # 全期間分dates/closes(outputsize=full)。取得できていない日はNoneの
                # まま渡し、_update_quintile_state側で起点の食い違いチェックのみ行う
                # (欠測補完はできないが、架空の値は作らない)。
                dates_today = fetched[ticker][0] if ticker in fetched else None
                closes_today = fetched[ticker][1] if ticker in fetched else None

                # β(表示専用、2026-09-25追加): 当日その銘柄・SPYの両方が取得できた
                # 場合のみ計算する(追加のAPI呼び出しは発生しない、既にfetched済みの
                # dates/closesを再利用するだけ)。片方でも欠けていればNoneのまま
                # _update_quintile_stateに渡し、前回値を保持させる。
                beta_today = None
                if ticker in fetched and spy_dates is not None and spy_closes is not None:
                    ticker_dates, ticker_closes, _ticker_volumes = fetched[ticker]
                    try:
                        beta_today = beta_logic.compute_beta(ticker_dates, ticker_closes, spy_dates, spy_closes)
                    except Exception as e:
                        print(f"[BETA][WARN] {ticker}: β計算に失敗しました: {e}", file=sys.stderr)
                        beta_today = None

                state = _update_quintile_state(
                    ticker, score, bounds, actual_trading_date, price=price_today, beta=beta_today,
                    trading_calendar=trading_calendar_dates, dates=dates_today, closes=closes_today,
                )
                print(f"[Q1-5][OK] {ticker}: {state['current_q']} (pred_score={score:.2f})")
    else:
        print(
            f"[Q1-5][INFO] 取引日が進んでいないため(最新取引日={actual_trading_date}、"
            f"前回処理済み={prev_trading_date})、Q1〜Q5状態の更新をスキップします。"
        )

    # 旧指標(stock_logic)側が同じ取得結果を再利用できるよう、監視銘柄分の生データ
    # (dates/closes/volumes)を戻り値に追加する(design 2026-09-17: Alpha Vantage撤去
    # に伴う追加。上記①〜⑩のQ1〜Q5計算そのものには一切影響しない、戻り値への追記のみ)。
    raw_watchlist_data = {
        t: fetched[t] for t in watchlist[: logic.MAX_TICKERS] if t in fetched
    }

    return {
        "fetched": list(fetched.keys()), "failed": failed, "rate_limited": rate_limited,
        "raw_watchlist_data": raw_watchlist_data,
        "quintile_updated": quintile_updated, "trading_date": actual_trading_date,
    }


def _update_legacy_records(raw_watchlist_data, tickers):
    """run_quintile_refreshが監視銘柄向けに取得済みのTwelve Data生データ
    (dates/closes/volumes)を再利用し、旧指標(stock_logic.build_result、
    株価・RSI・前日比・1ヶ月騰落率)を計算してRedisへ保存する(design
    2026-09-17: Alpha Vantage撤去に伴う追加、新規API呼び出しは発生しない)。

    stock_logic.compute_indicators/build_result自体のロジックは無変更。
    ニュース・時価総額は渡さない(build_resultのデフォルト=None/空のまま)。
    1銘柄の失敗が他銘柄・Q1〜5側の処理に影響しないよう、個別にtry/exceptする。

    戻り値: {"success": [...], "failed": [...]}
    """
    success, failed = [], []
    for ticker in tickers[: logic.MAX_TICKERS]:
        data = raw_watchlist_data.get(ticker)
        if not data:
            continue  # Q1〜5側で取得できなかった銘柄はこちらでも新規取得しない
        dates, closes, volumes = data
        try:
            record = logic.build_result(ticker, dates, closes, volumes)
            record["fetched_at"] = _now_iso()
            record["is_stale"] = False
            record["last_error"] = None
            record["last_error_message"] = None
            record["last_error_at"] = None
            store.set_ticker_record(ticker, record)
            success.append(ticker)
            print(f"[OK] {ticker}: {record['judgment']}（最終取引日 {record['last_trade_date']}）")
        except Exception as e:
            _mark_stale(ticker, "UNKNOWN", str(e))
            failed.append({"ticker": ticker, "type": "UNKNOWN", "message": str(e)})
            print(f"[NG] {ticker}: 判定計算中に予期しないエラー: {e}")
    return {"success": success, "failed": failed}


def main():
    # 2026-09-17: Alpha Vantage完全撤去。TWELVEDATA_API_KEYだけが処理全体の
    # 前提になり、以前のように「Alpha Vantageキーが無いとQ1〜Q5処理にすら
    # 到達しない」という依存関係は解消した。
    td_api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    if not td_api_key:
        print("[ERROR] TWELVEDATA_API_KEYが設定されていません。処理を中止します。", file=sys.stderr)
        return 1

    tickers = store.get_watchlist()
    if not tickers:
        print("[INFO] watchlistが空のためDEFAULT_TICKERSを使用します。")
        tickers = list(logic.DEFAULT_TICKERS)
        try:
            store.set_watchlist(tickers)
        except Exception as e:
            print(f"[WARN] watchlistの初期化に失敗しました: {e}", file=sys.stderr)

    _, td_used_before = remaining_td_budget()
    print(f"[INFO] 本日のTwelve Data使用実績: {td_used_before}回 / 自己申告上限 {td.DAILY_API_BUDGET}回")

    # Q1〜Q5判定(quintile_logic.py、①〜⑩の判定ロジック自体は無変更)。
    # この呼び出しの中で監視銘柄のTwelve Data取得も行われ、その生データが
    # 戻り値のraw_watchlist_dataに含まれる。
    legacy_result = {"success": [], "failed": []}
    try:
        td_result = run_quintile_refresh(td_api_key, tickers)
        if td_result is not None:
            print(
                f"[Q1-5][DONE] 取得成功 {len(td_result['fetched'])}件 / "
                f"失敗 {len(td_result['failed'])}件 / RATE_LIMIT={td_result['rate_limited']} / "
                f"取引日={td_result.get('trading_date')} / "
                f"Q1〜5状態更新={'実施' if td_result.get('quintile_updated') else 'スキップ(取引日が進んでいない)'}"
            )
            # 旧指標側は、Q1〜5側が既に取得済みの生データを再利用するだけ
            # (追加のAPI呼び出しは発生しない)。この処理が失敗してもQ1〜5側の
            # 結果(上のtd_result)には一切影響しない。
            legacy_result = _update_legacy_records(td_result.get("raw_watchlist_data", {}), tickers)
    except Exception as e:
        print(f"[Q1-5][WARN] Q1〜Q5処理で予期しないエラーが発生しました: {e}", file=sys.stderr)

    update_rank_snapshot(tickers)

    _, td_used_after = remaining_td_budget()
    summary = {
        "run_at": _now_iso(),
        "success": legacy_result["success"],
        "failed": legacy_result["failed"],
        "skipped": [],
        "api_calls_used_today": td_used_after,
        "api_budget": td.DAILY_API_BUDGET,
    }
    try:
        store.set_last_refresh(summary)
    except Exception as e:
        print(f"[WARN] last_refreshサマリーの保存に失敗しました: {e}", file=sys.stderr)

    print(
        f"[DONE] 成功 {len(legacy_result['success'])}件 / 失敗 {len(legacy_result['failed'])}件 / "
        f"本日のTwelve Data使用 {td_used_after}回"
    )

    # 一部失敗があってもプロセス自体は正常終了させる（他銘柄は正常に更新済みのため）
    return 0


if __name__ == "__main__":
    sys.exit(main())
