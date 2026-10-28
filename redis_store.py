"""Upstash Redis (REST API) への薄いアクセスラッパー。

Renderの無料Webサービスはローカルディスクが永続化されないため、
「銘柄一覧」「各銘柄の最新判定結果」「本日のAPI呼び出し回数」「直近の
自動更新サマリー」はすべてここを経由してUpstash Redisに保存する。

接続断・認証エラーなど想定外の事態でも呼び出し元（app.py / refresh.py）を
クラッシュさせないよう、失敗時は例外を投げずに None / False を返す。
"""

import json
import os
import sys

import requests

_TIMEOUT = 10


def _base_url():
    return os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")


def _token():
    return os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def is_configured():
    return bool(_base_url() and _token())


def _headers():
    return {"Authorization": f"Bearer {_token()}"}


def _request(method, path, **kwargs):
    if not is_configured():
        print("redis_store: UPSTASH_REDIS_REST_URL/TOKENが未設定です", file=sys.stderr)
        return None
    url = f"{_base_url()}/{path}"
    try:
        r = requests.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"redis_store: Upstashへの接続に失敗しました ({path}): {e}", file=sys.stderr)
        return None
    except ValueError:
        print(f"redis_store: Upstashから想定外のレスポンスを受け取りました ({path})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 低レベルコマンド
# ---------------------------------------------------------------------------

def get(key):
    res = _request("GET", f"get/{key}")
    return res.get("result") if res else None


def set_(key, value, ex=None):
    path = f"set/{key}"
    if ex:
        path += f"?EX={ex}"
    res = _request("POST", path, data=value.encode("utf-8"))
    return res is not None


def incrby(key, amount=1):
    res = _request("POST", f"incrby/{key}/{amount}")
    return res.get("result") if res else None


def expire(key, seconds):
    res = _request("POST", f"expire/{key}/{seconds}")
    return res is not None


def set_nx(key, value, ex=None):
    """SET key value NX(キーが未設定の場合のみ書き込む)。
    戻り値: 書き込みに成功した(=キーが未設定だった)ならTrue、
    既にキーが存在して書き込まれなかった場合はFalse。
    Upstash/Redisに到達できない場合もFalse(呼び出し元を待たせない・
    誤って「ロックを取れた」と判定しないためのフェイルセーフ)。

    2026-10-28修正(バグ修正、design調査で発見): UpstashのREST API仕様
    (docs.upstash.com)では、NXは値を持たない裸のクエリフラグ
    (例: "set/foo?NX&EX=100")であり、"NX=true"という記法は存在しない。
    以前の実装は誤って"?NX=true"を送っており、Upstashが400 Bad Request
    を返し続けていた(2026-10-22〜、本番で一度も成功していなかった)。
    このバグはRedisのpath/query構造自体には影響せず、NXフラグの表記だけ
    の問題だったため、修正もその1点のみに限定する。"""
    path = f"set/{key}?NX"
    if ex:
        path += f"&EX={ex}"
    res = _request("POST", path, data=value.encode("utf-8"))
    if res is None:
        return False
    return res.get("result") == "OK"


# ---------------------------------------------------------------------------
# JSON値のget/set
# ---------------------------------------------------------------------------

def get_json(key, default=None):
    raw = get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def set_json(key, value, ex=None):
    return set_(key, json.dumps(value, ensure_ascii=False), ex=ex)


# ---------------------------------------------------------------------------
# アプリ固有のヘルパー
# ---------------------------------------------------------------------------

def get_watchlist():
    return get_json("watchlist")


def set_watchlist(tickers):
    return set_json("watchlist", list(tickers))


def get_ticker_record(ticker):
    return get_json(f"ticker:{ticker.upper()}")


def set_ticker_record(ticker, record):
    return set_json(f"ticker:{ticker.upper()}", record)


def get_api_budget(date_str):
    v = get(f"api_budget:{date_str}")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def incr_api_budget(date_str, by=1):
    key = f"api_budget:{date_str}"
    new_val = incrby(key, by)
    # 2日でキーを自動失効させ、Redis上に無限に日付キーが溜まらないようにする
    expire(key, 2 * 24 * 3600)
    return new_val


def get_last_refresh():
    return get_json("last_refresh")


def set_last_refresh(summary):
    return set_json("last_refresh", summary)


def get_last_manual_refresh():
    return get("last_manual_refresh")


def set_last_manual_refresh(iso_timestamp):
    return set_("last_manual_refresh", iso_timestamp)


# ---------------------------------------------------------------------------
# Q1〜Q5判定(Twelve Data、quintile_logic.py)専用のキー。
# IMPLEMENTATION_DESIGN_quintile_q1q5.md 2-7節の設計通り、生の株価履歴は
# 保存しない(pred_score等の軽量な計算結果のみ)。既存キー・既存関数は
# 一切変更していない(追加のみ)。
# ---------------------------------------------------------------------------

def get_refpool_score(ticker):
    """参照母集団(REFERENCE_UNIVERSE)1銘柄の最新pred_score等を取得する。
    戻り値: {"pred_score": float, "last_updated": "YYYY-MM-DD", "features": {...}} または None。"""
    return get_json(f"quintile:refpool:{ticker.upper()}")


def set_refpool_score(ticker, data):
    return set_json(f"quintile:refpool:{ticker.upper()}", data)


def get_pool_history():
    """直近252営業日分の参照母集団プール履歴を取得する。
    戻り値: {"dates": [...], "scores_by_date": {"YYYY-MM-DD": [float, ...]}} または None
    (Noneの場合、quintile_logic.update_pool_historyが新規作成する)。"""
    return get_json("quintile:pool_history")


def set_pool_history(data):
    return set_json("quintile:pool_history", data)


def get_td_api_budget(date_str):
    """Twelve Data専用の当日消費credits数を取得する(Alpha Vantage用の
    get_api_budgetとはキーのprefixが異なる別カウンタ、完全に独立)。"""
    v = get(f"td_api_budget:{date_str}")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def incr_td_api_budget(date_str, by=1):
    key = f"td_api_budget:{date_str}"
    new_val = incrby(key, by)
    expire(key, 2 * 24 * 3600)
    return new_val


def get_quintile_state(ticker):
    """ユーザー監視銘柄1件のQ状態履歴を取得する。
    戻り値: {"current_q": "Q3", "previous_q": "Q4", "history": [...]} または None。"""
    return get_json(f"quintile:state:{ticker.upper()}")


def set_quintile_state(ticker, data):
    return set_json(f"quintile:state:{ticker.upper()}", data)


def get_riskoff_state():
    """Risk-off判定(design 2026-10-16、バックテスト検証済み)の状態を取得する。
    戻り値: {"active": bool, "below_streak": int, "above_streak": int,
    "spy_close": float, "sma200": float, "ratio": float, "last_updated": ...}
    または None(未計算時)。既存のQ1〜Q5判定・購入/売却判定・AI一斉調整判定
    とは完全に独立した表示専用データで、他のキーとは別キーに保存する。"""
    return get_json("riskoff:state")


def set_riskoff_state(data):
    return set_json("riskoff:state", data)


def get_ai_market_decline_state():
    """AI市場下落状況パネル(design 2026-10-23、過去検証済み)の状態を取得する。
    戻り値: {"date": "YYYY-MM-DD", "n_down": int, "n_pop": int, "pct_down": float,
    "last_updated": ...} または None(未計算時)。既存のQ1〜Q5判定・購入/売却判定・
    Risk-off判定・AI一斉調整判定とは完全に独立した表示専用データで、他のキー
    とは別キーに保存する(銘柄ごとの生の株価履歴は保存しない)。"""
    return get_json("ai_market_decline:state")


def set_ai_market_decline_state(data):
    return set_json("ai_market_decline:state", data)


def get_portfolio_state():
    """日次アクション型UI(design 2026-10-16、IMPLEMENTATION_DESIGN_daily_action_ui.md)
    のポートフォリオ記録(ユーザーが「購入を記録」した結果)を取得する。
    戻り値: {"positions": {"<TICKER>": {"entry_date": "YYYY-MM-DD",
    "entry_price": float, "shares": float or None(design 2026-10-18、+50%利益
    100%利確ルール用の任意入力。未入力ならNoneのまま、$ベースの利確計算は行わない)、
    "profit_taken": bool, "profit_taken_date": ... or None,
    "profit_take_sold_shares"/"profit_take_sold_value"/"profit_take_price":
    利確実行時のみ記録(それ以外はキー自体が存在しない)}}, "last_updated": ...}
    または None(未記録時)。既存のQ1〜Q5判定・Day0判定・Risk-off判定とは完全に
    独立したデータで、他のキーとは別キーに保存する。実際の証券口座とは連携しない
    (ユーザーが確認ボタンで記録した内容のみ)。"""
    return get_json("portfolio:state")


def set_portfolio_state(data):
    return set_json("portfolio:state", data)


# ---------------------------------------------------------------------------
# Q5 48銘柄(quintile_logic.ROTATION_UNIVERSE)の将来成績をforward-onlyで
# 蓄積する専用機能(design 2026-10-21)。既存のQ1〜Q5判定・BUY/SELL判定・
# +50%利確・Risk-off・AI一斉調整・portfolio:state・watchlistとは完全に
# 独立した別キーで、ここに追加する関数も既存関数には一切影響しない。
# 新しい別ジョブ(refresh_q5_universe.py)専用の読み書きのみ。
# ---------------------------------------------------------------------------

def get_priormean_bounds_history():
    """prior_mean差分更新の設計(2026-10-27、design調査・Phase 0実装)専用の
    保存データを取得する。REFERENCE_UNIVERSE(quintile_logic.REFERENCE_UNIVERSE、
    48銘柄+SPY、無変更)の過去の日次Q1〜Q5 bounds時系列
    ({"YYYY-MM-DD": [b1,b2,b3,b4], ...}、quintile_logic.pool_percentile_bounds
    の戻り値をそのまま日付ごとに保存したもの)。

    新規ウォッチリスト銘柄を将来バックフィルする際(Phase 1以降で実装予定)、
    REFERENCE_UNIVERSE全体のpred_scoreを再計算せずに済むようにするための
    前提データであり、現時点ではどの判定ロジックからも参照されない独立した
    データ(build_priormean_bounds_history.pyが書き込むだけ)。

    既存のQ1〜Q5判定・Q5 Day0判定・prior_mean計算・ラベル判定・Q5ランキング・
    BUY判定・Risk-off・switch_recommendation_logic・AI市場下落判定のいずれにも
    一切接続しない。TTLは意図的に付けない(長期保持が必要な履歴データのため)。
    または None(未生成時)。"""
    return get_json("priormean:bounds_history")


def set_priormean_bounds_history(data):
    return set_json("priormean:bounds_history", data)


def get_priormean_events(ticker):
    """prior_mean差分更新Phase 1(design調査・2026-10-27確認済み)。新規
    ウォッチリスト銘柄(既存q5_prior_mean_reference.jsonの56銘柄には含まれ
    ない銘柄)のQ5 Day0履歴を取得する。

    戻り値: [{"date","ret20","max_dd20"}, ...](既存の
    q5_prior_mean_reference.jsonのevents_by_ticker[ticker]と完全に同一の
    形式)、または None(未バックフィル時)。

    既存56銘柄のevents_by_ticker(q5_prior_mean_reference.json)とは完全に
    別のキー空間であり、このキーの内容が既存56銘柄のデータに混ざることは
    ない(呼び出し側がpriormean_incremental_logic.merge_reference_dataで
    論理的に結合する時だけ、get_prior_mean/get_prior_statsに渡す一時的な
    dictの中で合流する。ファイル自体は別々のまま)。TTLは付けない。"""
    return get_json(f"priormean:events:{ticker.upper()}")


def set_priormean_events(ticker, events):
    return set_json(f"priormean:events:{ticker.upper()}", events)


def get_priormean_backfill_status(ticker):
    """prior_mean差分更新Phase 1専用。新規銘柄のバックフィル進行状況を
    取得する。戻り値: "pending" | "in_progress" | "done" | "failed" の
    いずれか、または None(一度もバックフィル処理の対象になっていない=
    「新規銘柄」として検出されうる状態)。"""
    return get_json(f"priormean:backfill_status:{ticker.upper()}")


def set_priormean_backfill_status(ticker, status):
    return set_json(f"priormean:backfill_status:{ticker.upper()}", status)


def get_priormean_pending_backfill():
    """prior_mean差分更新Phase 2専用。新規にバックフィルが必要だと検出
    された銘柄のキュー。戻り値: ["<TICKER>", ...](検出順)、または None
    (未初期化時)。refresh_priormean_incremental.py(日次検出)が追加し、
    run_pending_priormean_backfill.py(別ワークフロー、1回1銘柄処理)が
    1件ずつ取り出して消費する。既存のwatchlist/quintile:state等とは
    完全に独立したキーで、どの判定ロジックからも参照されない。"""
    return get_json("priormean:pending_backfill")


def set_priormean_pending_backfill(tickers):
    return set_json("priormean:pending_backfill", list(tickers))


def get_priormean_watchlist_added_at():
    """24:00 JST締切の深夜バックフィル専用(design 2026-10-28c)。
    watchlistへ銘柄が追加された時刻(JST、ISO8601文字列)を銘柄ごとに記録
    した辞書。戻り値: {"<TICKER>": "2026-10-27T23:50:00+09:00", ...}、または
    {}(未初期化時)。app.add_watchlist()が追加時に書き込み、
    priormean_incremental_logic.detect_new_tickers_for_overnight_backfillが
    24:00 JST以前に追加されたかどうかの判定だけに使う(この機能導入前から
    watchlistに存在していた銘柄はこの辞書にキーが無く、その場合は締切の
    対象外として常に検出対象に含める、既存のwatchlist/quintile:state等の
    判定ロジックには一切接続しない)。"""
    return get_json("priormean:watchlist_added_at", default={}) or {}


def set_priormean_watchlist_added_at(ticker, iso_timestamp):
    """1銘柄分の追加時刻だけを読み取り→マージ→書き込みする(他の銘柄の
    記録を上書きしない)。失敗してもwatchlist本体の追加処理は失敗させない
    設計(呼び出し元が戻り値を見てbest-effortに扱う)。"""
    current = get_priormean_watchlist_added_at()
    current = dict(current)
    current[ticker.upper()] = iso_timestamp
    return set_json("priormean:watchlist_added_at", current)


def try_acquire_priormean_backfill_lock(ticker, ttl_seconds=600):
    """prior_mean即時dispatchバックフィル専用(design 2026-10-28d)。同一
    tickerに対する同時dispatch/cronの競合を1プロセスだけに限定するための
    短命ロック(try_acquire_push_dispatch_lock/try_acquire_push_refresh_
    notify_lockと完全に同じSET NXパターン)。明示的な解放は行わず、TTL失効
    に委ねる(既存のpush系ロックと同じ方針、必要以上に複雑にしない)。
    TTLのデフォルト600秒は、新規銘柄1件のバックフィル実測(KNN計算約3分強
    +Twelve Data取得+GitHub Actions起動分)に十分な余裕を持たせた値。"""
    return set_nx(f"priormean:backfill_lock:{ticker.upper()}", "1", ex=ttl_seconds)


def get_priormean_incremental_state():
    """prior_mean差分更新Phase 2専用。日次差分更新(refresh_priormean_
    incremental.py)の進行状態。

    戻り値: {"prev_q": {"<TICKER>": "Q4", ...}(バックフィル済み銘柄の
    前回判定時のQ、Day0検出の起点。quintile:state:{ticker}が既に毎日
    保持しているcurrent_q/previous_qとは別に、このジョブ自身が「最後に
    処理した日の時点のQ」として独立に保持する。理由: quintile:stateは
    本体refresh.pyの都合で上書きされていくため、このジョブが処理に失敗した
    日があっても、このジョブ自身の最終処理地点を正しく追跡するため),
    "pending": [{"ticker","day0_date","day0_price"}, ...](Day0検出済みだが
    まだ20営業日経過していないイベント。(ticker, day0_date)の組ごとに
    1件。確定したらpriormean:events:{ticker}へ移動しここからは削除する),
    "last_processed_date": "YYYY-MM-DD"}
    または None(未初期化時)。

    既存のquintile:state・q5universe:state・watchlist等とは完全に独立した
    キーであり、どのQ1〜Q5判定・BUY判定・Risk-off・switch recommendation
    からも参照されない(読み取りも書き込みもこのジョブ専用)。"""
    return get_json("priormean:incremental_state")


def set_priormean_incremental_state(data):
    return set_json("priormean:incremental_state", data)


def get_q5universe_state():
    """48銘柄の将来成績蓄積状態を取得する。
    戻り値: {"prev_q": {"<TICKER>": "Q4", ...}(前回判定時のQ、Day0検出の
    起点。未知のティッカーはキー自体が存在しない=過去を推定しない),
    "entries": [{"ticker","day0_date","day0_price",
    "day5_date","day5_price","day20_date","day20_price",
    "day60_date","day60_price"}, ...](Day0イベントの配列、株数は持たない。
    同一tickerでも(ticker,day0_date)ごとに別イベント),
    "last_processed_date": "YYYY-MM-DD"(この日付までの処理が完全に成功した
    ことを示す。未完了日はこの値を更新しない),
    "latest_price": {"<TICKER>": float, ...}(design 2026-10-21追加、48銘柄
    の当日終値スナップショット。「Q5直近勢い」=day0からの途中経過リターン
    算出専用、entries自体には追加フィールドを持たせない),
    "latest_price_date": "YYYY-MM-DD"(latest_priceの基準日)}
    または None(未初期化時)。latest_price/latest_price_dateが存在しない
    古いstate(本機能追加前に保存されたもの)でも、.get()で安全にNone/空dict
    として扱われる想定(呼び出し側で対応、マイグレーション不要)。

    prev_q・entries・last_processed_date・latest_price・latest_price_dateを
    1つのJSONにまとめているのは、48銘柄全件の取得・判定が完全に成功した
    場合だけ、これらを同時に(1回のRedis書き込みで)確定させるため(design:
    「全件取得・判定→検証→一括確定」、途中書き込みによる不整合を避ける)。"""
    return get_json("q5universe:state")


def set_q5universe_state(data):
    return set_json("q5universe:state", data)


# ---------------------------------------------------------------------------
# Web Push通知(design 2026-10-22: Renderデプロイ検知→Windowsデスクトップ通知)。
# push_notify.pyからのみ使用する。Q1〜Q5・Q5シグナル・株価API・既存の
# watchlist/portfolio/riskoff等のキーには一切関与しない、完全に独立した
# 名前空間(push:*)。
# ---------------------------------------------------------------------------

def get_push_dispatched_commit():
    """サーバーが最後にpush送信を試みた(dispatchした)commit。
    multi-worker/再起動時の「同じcommitへの二重送信」防止に使う。"""
    return get("push:dispatched_commit")


def set_push_dispatched_commit(commit):
    return set_("push:dispatched_commit", commit)


def try_acquire_push_dispatch_lock(commit, ttl_seconds=120):
    """同一commitに対する同時dispatchを1ワーカーだけに限定するための短命ロック。"""
    return set_nx(f"push:dispatch_lock:{commit}", "1", ex=ttl_seconds)


def get_push_notified_commit():
    """ユーザーが実際に通知を目にした(と確認できた)最後のcommit。
    Service Workerのpushイベント確認(ack)、またはフォアグラウンド起動時の
    フォールバック通知のどちらかが発生した時点で更新される。dispatchedとは
    別の値(送信を試みた≠実際に表示された、を区別するため)。"""
    return get("push:notified_commit")


def set_push_notified_commit(commit):
    return set_("push:notified_commit", commit)


def get_push_subscriptions():
    return get_json("push:subscriptions", default=[])


def set_push_subscriptions(subs):
    return set_json("push:subscriptions", subs)


# 日次株価更新 完了通知(design 2026-10-22、push_notify.notify_refresh_success
# からのみ使用)。デプロイ検知用のdispatched_commit/notified_commitとは別の
# 名前空間(日付ベース)で、同じ取引日への重複送信だけを防ぐためのもの。
def get_push_refresh_notified_date():
    """最後に「株価更新完了」通知を実際に送信し終えた取引日(YYYY-MM-DD)。"""
    return get("push:refresh_notified_date")


def set_push_refresh_notified_date(trading_date):
    return set_("push:refresh_notified_date", trading_date)


def try_acquire_push_refresh_notify_lock(trading_date, ttl_seconds=120):
    """同一取引日に対する同時送信を1プロセスだけに限定するための短命ロック
    (try_acquire_push_dispatch_lockと同じSET NXパターン)。"""
    return set_nx(f"push:refresh_notify_lock:{trading_date}", "1", ex=ttl_seconds)
