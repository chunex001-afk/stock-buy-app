# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 1/2: 新規
ウォッチリスト銘柄の検出・1銘柄だけのQ5 Day0バックフィル・既存JSON+Redis
差分データのマージ(Phase 1)・日次の保留イベント確定(Phase 2、
resolve_pending_event)を行う純粋関数群。

【絶対に守ること】
- quintile_logic.py / q5_prior_mean_logic.py / q5_universe_logic.py は
  一切変更しない(ここではimportして呼ぶだけ)。
- get_prior_mean()/get_prior_stats()自体は一切変更しない。この関数群が
  組み立てる「data」辞書を、呼び出し側(backfill_new_ticker_prior_mean.py
  やapp.py)がdata=引数として渡すことで利用する想定(Phase 1では呼び出し
  側の配線はまだ行わない、組み立て関数の提供のみ)。
- REFERENCE_UNIVERSE(48銘柄+SPY)のpred_scoreは一切再計算しない。
  Phase 0で保存したbounds_history(日付→[b1,b2,b3,b4])を受け取って使う
  だけで、REFERENCE_UNIVERSE自体にはこのファイルのどこからもアクセスしない。
- Day0の定義(前回Qが既知かつ今日Q5かつ前回Q5でない)はq5_universe_logic.
  detect_day0_tickers(無変更)をそのまま使う。
- ret20/max_dd20の定義はbuild_q5_prior_mean_reference.pyの既存定義
  (Day0終値基準、20営業日後終値までのリターン、Day0自身は
  max_dd20の対象外、終値ベース)と完全に同一にする(意図的に独立実装し、
  既存スクリプトは一切変更しない)。
- Redis/API/ファイルへのアクセスは一切行わない純粋関数のみ(Redis I/Oは
  backfill_new_ticker_prior_mean.py側が担う)。
"""
import quintile_logic
import q5_universe_logic


def detect_new_tickers(watchlist, static_tickers, redis_status_by_ticker):
    """新規にバックフィルが必要な銘柄を検出する(重い処理は一切行わない、
    リストの比較だけ)。

    watchlist: 現在のウォッチリスト(redis_store.get_watchlist()の戻り値、
    無変更)。
    static_tickers: 既存q5_prior_mean_reference.jsonのevents_by_ticker.
    keys()(既存56銘柄、無変更)。
    redis_status_by_ticker: {"<TICKER>": "pending"|"in_progress"|"done"|
    "failed", ...}(各銘柄のpriormean:backfill_status:{TICKER}から呼び出し
    側が組み立てた辞書。キーが存在しない銘柄は一度もバックフィル対象に
    なっていないという意味)。

    戻り値: watchlistの順序を維持した、新規バックフィルが必要な銘柄の
    リスト。以下は除外する:
      - 既存56銘柄(static_tickers)
      - 既にpending/in_progress/doneの銘柄(二重処理防止)
    "failed"だった銘柄は再試行対象として含める。"""
    static_set = set(static_tickers)
    result = []
    for t in watchlist:
        if t in static_set:
            continue
        status = redis_status_by_ticker.get(t)
        if status in ("pending", "in_progress", "done"):
            continue
        result.append(t)
    return result


def _max_dd20(closes, i0):
    """build_q5_prior_mean_reference.pyの_max_dd20と完全に同一の定義
    (Day0終値基準、以降20営業日の終値のうち最も下落した時点のリターン、
    Day0自身は対象外、終値ベース)。既存スクリプトは一切変更していない
    (q5_prior_mean_logic.get_prior_stats()がget_prior_mean()のフィルタ
    条件を意図的に重複実装している既存の設計方針と同じ考え方で、この
    Phase 1専用コードも独立して同じ定義を持つ)。"""
    day0_close = closes[i0]
    window = closes[i0 + 1: i0 + 21]
    if not window:
        return None
    return min((c / day0_close - 1) * 100 for c in window)


def backfill_ticker_day0_events(ticker, dates, closes, scores_by_date, bounds_by_date):
    """新規銘柄1件だけのQ5 Day0履歴をバックフィルする。REFERENCE_UNIVERSE
    48銘柄は一切参照しない(bounds_by_dateとしてPhase 0が既に計算済みの
    ものを受け取るだけ)。

    ticker: 対象銘柄(Day0検出のdictキーとして使うだけ)。
    dates, closes: この銘柄自身の価格データ(終値)。
    scores_by_date: {"YYYY-MM-DD": float, ...}(この銘柄自身のpred_score。
    quintile_logic.compute_features/knn_predict_scoreで呼び出し側が
    既に計算済みのものを渡す。この関数自体はスコア計算を行わない)。
    bounds_by_date: Phase 0のpriormean:bounds_history(無変更のまま渡す)。

    戻り値: {
      "resolved_events": [{"date","ret20","max_dd20"}, ...]
        (20営業日経過済みでret20が確定しているものだけ)、
      "unresolved_day0_dates": [...]
        (Day0は検出されたがまだ20営業日経過していない日付)、
      "day0_count": int(確定+未確定の合計Day0件数)}

    【未来データ混入防止】
    日付を昇順に1日ずつ処理し、その日までのQ状態(last_known_q)だけを
    使ってDay0判定する(q5_universe_logic.detect_day0_tickersの既存仕様
    どおり、前回Qが未知の初回は推定しない)。ret20は「i0+20が実際に
    closes配列の範囲内にある(=20営業日が実際に経過した)」場合だけ計算し、
    未経過のものはunresolved_day0_datesに入れるだけでret20は一切計算
    しない(架空の値で補完しない、既存q5_universe_logic.fill_checkpointsと
    同じcausalな設計方針)。"""
    idx_of_date = {d: i for i, d in enumerate(dates)}
    last_known_q = None
    day0_dates = []
    for date_str in sorted(bounds_by_date.keys()):
        score = scores_by_date.get(date_str)
        if score is None:
            continue
        bounds = bounds_by_date[date_str]
        q_today = quintile_logic.assign_quintile(score, bounds)
        prev_q = {ticker: last_known_q} if last_known_q is not None else {}
        today_q = {ticker: q_today}
        if q5_universe_logic.detect_day0_tickers(prev_q, today_q):
            day0_dates.append(date_str)
        last_known_q = q_today

    resolved_events = []
    unresolved_day0_dates = []
    for d0 in day0_dates:
        i0 = idx_of_date.get(d0)
        if i0 is None:
            continue
        exit_idx = i0 + 20
        if exit_idx >= len(closes):
            unresolved_day0_dates.append(d0)
            continue
        ret20 = (closes[exit_idx] / closes[i0] - 1) * 100
        resolved_events.append({
            "date": d0,
            "ret20": round(ret20, 4),
            "max_dd20": round(_max_dd20(closes, i0), 4),
        })

    return {
        "resolved_events": resolved_events,
        "unresolved_day0_dates": unresolved_day0_dates,
        "day0_count": len(day0_dates),
    }


def dedup_events(existing_events, new_events):
    """1銘柄分のイベントリスト同士を、日付(=Day0日、一意キーは暗黙に
    (ticker, day0_date)だがこの関数は既にticker単位のリストを扱うため
    実質dateだけで判定する)をキーに重複排除してマージする。

    既存イベント(existing_events、Redisに既に保存済みのもの)の値を常に
    優先し、新規計算分(new_events)で上書きしない(同じticker・同じ
    backfillスクリプトを誤って2回実行しても、既存イベントが変化しない
    ことを保証する、冪等性の中核)。戻り値はdate昇順にソート済み。"""
    by_date = {e["date"]: e for e in new_events}
    by_date.update({e["date"]: e for e in existing_events})
    return sorted(by_date.values(), key=lambda e: e["date"])


def merge_reference_data(static_data, redis_events_by_ticker):
    """既存のq5_prior_mean_reference.json(static_data、無変更のまま
    q5_prior_mean_logic.load_reference_data()で読んだもの)と、Redis側の
    新規銘柄イベント(redis_events_by_ticker、{"<TICKER>": [...]}形式)を
    論理的に統合し、get_prior_mean()/get_prior_stats()にそのまま
    data=として渡せる辞書を返す。

    【絶対に守ること】
    - get_prior_mean()/get_prior_stats()自体は一切変更しない。この関数は
      それらに渡す「data」を組み立てるだけの、完全に新規・独立した関数。
    - static_dataに既に存在するtickerは、redis_events_by_tickerに同名の
      キーがあっても絶対に上書きしない(既存56銘柄のデータは常に
      static_data側が優先される、万一の事故を防ぐ防御的実装)。
    - static_data自体を変更しない(新しいdictを作って返す、in-place変更
      禁止、static_dataのevents_by_ticker辞書もコピーしてから使う)。"""
    merged_events = dict(static_data.get("events_by_ticker", {}))
    for ticker, events in redis_events_by_ticker.items():
        if ticker in merged_events:
            continue  # 既存56銘柄は絶対に上書きしない
        merged_events[ticker] = events
    merged = dict(static_data)
    merged["events_by_ticker"] = merged_events
    return merged


def resolve_pending_event(day0_price, close_by_date_map, day0_date, trading_calendar):
    """Phase 2専用: 保留中のDay0イベント(day0_price既知)が20営業日経過した
    際、close_by_date_map({"YYYY-MM-DD": close, ...}、その銘柄を新規に
    フェッチして得た日付→終値)からDay0+1〜Day0+20の終値を探して
    ret20/max_dd20を確定する。

    ret20/max_dd20の定義はbuild_q5_prior_mean_reference.py・Phase 1の
    backfill_ticker_day0_eventsと完全に同一(Day0終値基準、Day0自身は
    max_dd20の対象外、終値ベース)。

    trading_calendar: 取引日の昇順リスト(quintile:pool_historyの"dates"、
    既存のrefresh.py/refresh_q5_universe.pyが使っているのと同じカレンダー)。
    day0_dateがtrading_calendarに含まれない、またはDay0+1〜Day0+20の
    いずれかの日の終値がclose_by_date_mapに存在しない(=取得範囲外、
    データ未着)場合はNoneを返す(架空の値で補完しない、次回以降に委ねる。
    既存q5_universe_logic.fill_checkpointsの「対象日が見つからなければ
    nullのまま次回以降に委ねる」という設計方針と同一)。"""
    if day0_date not in trading_calendar:
        return None
    day0_idx = trading_calendar.index(day0_date)
    window_dates = trading_calendar[day0_idx + 1: day0_idx + 21]
    if len(window_dates) < 20:
        return None
    window_closes = [close_by_date_map.get(d) for d in window_dates]
    if any(c is None for c in window_closes):
        return None
    ret20 = (window_closes[-1] / day0_price - 1) * 100
    max_dd20 = min((c / day0_price - 1) * 100 for c in window_closes)
    return {"date": day0_date, "ret20": round(ret20, 4), "max_dd20": round(max_dd20, 4)}
