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

import os
import sys
from datetime import datetime, timezone

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


def _update_quintile_state(ticker, score, bounds, date_key, price=None):
    """ユーザー監視銘柄1件のQ状態を判定し、状態が変化した場合のみ履歴に追記する。
    「売り」「失敗」等の否定的な意味は一切持たせず、単なる状態記録として保存する
    (design 13の方針)。current_q/previous_q/history/pred_scoreの計算は無変更。

    2026-09-18追加、同日に5営業日固定クール仕様として正式化: q5_price_path
    (Q5「経過状態」表示専用、design参照)。「現在Q5かどうか」と「Q5後5営業日
    のクール」は別管理: 新規にQ5へ突入した日(前日Q5でない→当日Q5)で、かつ
    直前のクールが既にDay5まで終了している(またはそもそもクールが無い)場合
    にのみ空にリセットして新Day0を開始する。クールの途中でQ4以下に戻っても・
    途中で再びQ5になっても、そのクールの観測(Day0〜Day5)は継続してリセット
    しない。Day0〜Day5(MAX_Q5_PRICE_PATH_DAYS件)に達したらクールは終了し、
    それ以上は追記しない(先頭=Day0は上書き・切り捨てしない)。
    Day5に到達した回だけ、_advance_q5_warningでその時点のQ5起点騰落率を見て
    注意喚起(q5_warning)の発生を判定する。q5_warningはクールの状態とは独立に
    保持・失効する(design参照)。
    いずれもapp.py側の表示専用ロジックが読むだけで、quintile_logic.py・
    history・pred_score・既存のQ5シグナルには一切影響しない。"""
    q = quintile_logic.assign_quintile(score, bounds)
    prev_state = store.get_quintile_state(ticker) or {}
    prev_q = prev_state.get("current_q")
    history = list(prev_state.get("history", []))

    if not history or prev_q != q:
        history.append({"date": date_key, "q": q})
        history = history[-MAX_QUINTILE_STATE_HISTORY:]

    price_path = list(prev_state.get("q5_price_path", []))
    cool_already_ended = len(price_path) >= MAX_Q5_PRICE_PATH_DAYS
    is_new_cool = q == "Q5" and prev_q != "Q5" and (not price_path or cool_already_ended)
    if is_new_cool:
        price_path = []
    just_completed_day5 = False
    if price and (q == "Q5" or price_path) and len(price_path) < MAX_Q5_PRICE_PATH_DAYS:
        if not price_path or price_path[-1]["date"] != date_key:
            price_path.append({"date": date_key, "price": price})
            just_completed_day5 = len(price_path) == MAX_Q5_PRICE_PATH_DAYS

    warning = _advance_q5_warning(prev_state.get("q5_warning"), price_path, just_completed_day5, date_key)

    new_state = {
        "current_q": q,
        "previous_q": prev_q,
        "pred_score": score,
        "history": history,
        "q5_price_path": price_path,
        "q5_warning": warning,
        "last_updated": date_key,
    }
    store.set_quintile_state(ticker, new_state)
    return new_state


# BACKFILL_DAYS_BACK: 新規銘柄追加時に遡って再計算する日数(今日を含めて
# BACKFILL_DAYS_BACK+1日分。app.pyの表示が「今日・昨日・2〜5日前」の6列
# であることに合わせている)。
BACKFILL_DAYS_BACK = 5


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
    computed = []  # [{"date":..., "q":...}, ...] 古い→新しい順
    for k in range(BACKFILL_DAYS_BACK, -1, -1):
        target_idx = n_dates - 1 - k
        if target_idx < 0:
            continue  # その日の株価データ自体がまだ存在しない(上場間もない等)
        target_date = dates[target_idx]

        try:
            features = quintile_logic.compute_features(
                dates[: target_idx + 1], closes[: target_idx + 1], volumes[: target_idx + 1],
            )
        except Exception as e:
            print(f"[Q1-5][WARN] {ticker} {target_date}: 特徴量計算に失敗、この日はスキップ: {e}")
            continue

        # その日"以前"の日付だけにpool_historyを絞り込む(未来のプール更新は
        # 一切参照しない。dates文字列はYYYY-MM-DD形式のため単純な文字列比較で
        # 時系列順と一致する、既存コード各所と同じ前提)。
        filtered_scores_by_date = {
            d: v for d, v in pool_history["scores_by_date"].items() if d <= target_date
        }
        if not filtered_scores_by_date:
            continue  # その日の時点でプールにまだ何も蓄積されていない
        filtered_pool_history = {
            "dates": sorted(filtered_scores_by_date.keys()),
            "scores_by_date": filtered_scores_by_date,
        }
        bounds = quintile_logic.pool_percentile_bounds(filtered_pool_history)
        if bounds is None:
            continue

        try:
            score = quintile_logic.knn_predict_score(features)
        except Exception as e:
            print(f"[Q1-5][WARN] {ticker} {target_date}: pred_score計算に失敗、この日はスキップ: {e}")
            continue

        q = quintile_logic.assign_quintile(score, bounds)
        computed.append({"date": target_date, "q": q, "score": score})

    if not computed:
        print(f"[Q1-5][WARN] {ticker}: 過去分を1日も再計算できませんでした(データ不足)。")
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
        "last_updated": date_key,
    }
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

    pool_history = store.get_pool_history()
    pool_history = quintile_logic.update_pool_history(pool_history, date_key, scores_by_ticker)
    store.set_pool_history(pool_history)

    bounds = quintile_logic.pool_percentile_bounds(pool_history)

    # ⑧⑨ ユーザー監視銘柄のQ1〜Q5判定・状態履歴更新 ⑩ Redis保存(set_quintile_state内で実施)
    if bounds is not None:
        for ticker in watchlist[: logic.MAX_TICKERS]:
            score = scores_today.get(ticker)
            if score is None:
                cached = store.get_refpool_score(ticker)
                score = cached.get("pred_score") if cached else None
            if score is None:
                continue  # まだ一度もTwelve Dataで取得できていない銘柄は判定待ちのまま
            # q5_price_path用: 当日実際に取得できた終値があれば渡す(取得できて
            # いない日=fetched未成功の日は前回値のままpriceを渡さず、記録しない)。
            price_today = fetched[ticker][1][-1] if ticker in fetched else None
            state = _update_quintile_state(ticker, score, bounds, date_key, price=price_today)
            print(f"[Q1-5][OK] {ticker}: {state['current_q']} (pred_score={score:.2f})")

    # 旧指標(stock_logic)側が同じ取得結果を再利用できるよう、監視銘柄分の生データ
    # (dates/closes/volumes)を戻り値に追加する(design 2026-09-17: Alpha Vantage撤去
    # に伴う追加。上記①〜⑩のQ1〜Q5計算そのものには一切影響しない、戻り値への追記のみ)。
    raw_watchlist_data = {
        t: fetched[t] for t in watchlist[: logic.MAX_TICKERS] if t in fetched
    }

    return {
        "fetched": list(fetched.keys()), "failed": failed, "rate_limited": rate_limited,
        "raw_watchlist_data": raw_watchlist_data,
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
                f"失敗 {len(td_result['failed'])}件 / RATE_LIMIT={td_result['rate_limited']}"
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
