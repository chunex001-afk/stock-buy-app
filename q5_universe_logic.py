# -*- coding: utf-8 -*-
"""Q5 48銘柄(quintile_logic.ROTATION_UNIVERSE)の将来成績をforward-onlyで
蓄積する機能(design 2026-10-21)の判定ロジック。

既存のQ1〜Q5判定・BUY/SELL判定・+50%利確・Risk-off・AI一斉調整・portfolio・
trade_history・watchlistとは完全に独立しており、ここにある関数はいずれも
Redis/Twelve Dataへアクセスしない純粋関数(引数の辞書・リストも変更せず、
新しいdict/listを返す)。Redisアクセス・Twelve Data取得・完全性チェックは
refresh_q5_universe.py側が担う。

【Day0の定義】
「前回のQ判定が分かっているティッカーについて、今日Q5 かつ 前回Q5でない」
場合のみDay0とする。前回のQが未知(初回実行、またはそのティッカーを
初めて扱う)場合は、過去情報から推定してDay0を作ることを禁止する
(design確定仕様、look-ahead/誤った初期化防止)。

【Day0記録の不変性】
day0_date・day0_priceは一度記録したら以後変更しない。day5/20/60は、
ちょうどその営業日数に到達した日にのみ1回だけ追記する(すでに値が入って
いるフィールドは上書きしない)。Q5から途中で外れても追記は継続する。
"""

HOLD_CHECKPOINTS = (5, 20, 60)


def trading_day_index(calendar_dates, date_str):
    """calendar_dates(ソート済み"YYYY-MM-DD"文字列リスト)の中でdate_strが
    何番目(0始まり)の取引日かを返す。見つからなければNone(架空の取引日は
    作らない、app.py._trading_day_indexと同じ考え方の独立実装)。"""
    import bisect
    idx = bisect.bisect_left(calendar_dates, date_str)
    if idx < len(calendar_dates) and calendar_dates[idx] == date_str:
        return idx
    return None


def detect_day0_tickers(prev_q, today_q):
    """prev_q(前回保存したQ、{"<TICKER>":"Q4",...})とtoday_q(当日判定した
    Q、同形式)を比較し、新たにDay0になったティッカーのリストを返す
    (today_qに現れる順)。

    前回Qが未知(prev_qにそのティッカーのキーが無い)場合は、今日Q5でも
    Day0にしない(初回実行・新規ティッカーで過去を推定しないための仕様)。
    """
    day0 = []
    for ticker, q in today_q.items():
        prior_q = prev_q.get(ticker)
        if prior_q is None:
            continue
        if q == "Q5" and prior_q != "Q5":
            day0.append(ticker)
    return day0


def build_new_entries(day0_tickers, day0_date, prices_by_ticker):
    """新たにDay0になったティッカーについて、day5/20/60が未到達(None)の
    新規entryを作る(株数は持たない、design確定仕様)。"""
    entries = []
    for ticker in day0_tickers:
        entries.append({
            "ticker": ticker,
            "day0_date": day0_date,
            "day0_price": prices_by_ticker[ticker],
            "day5_date": None, "day5_price": None,
            "day20_date": None, "day20_price": None,
            "day60_date": None, "day60_price": None,
        })
    return entries


def fill_checkpoints(entries, trading_calendar, anchor_date, close_by_date_by_ticker):
    """既存entriesのうち、day0から5/20/60営業日目の取引日(trading_calendar
    上のその日付そのもの)にすでに到達している、かつまだ埋まっていない
    チェックポイントについて、そのticker自身の実際のOHLC履歴
    (close_by_date_by_ticker[ticker] = {"YYYY-MM-DD": close, ...}、当日の
    API取得で返ってきた日付→終値のマップ)から、該当するチェックポイント日
    そのものの終値を探して1回だけ追記した新しいリストを返す(引数の
    entriesは変更しない)。

    design 2026-10-21修正: 「今日の価格で代用する」のではなく、本来の
    チェックポイント日(day0+5/20/60営業日目)の実際の終値を遡って取得する。
    これにより、このジョブがちょうどその営業日に実行できず翌日以降に
    復旧した場合でも、正しいチェックポイント日の終値をそのまま記録できる
    (Twelve Dataの1回の取得は過去outputsize分の日付付き終値を含むため、
    追加のAPI呼び出しは発生しない)。

    対象日がまだ到達していない場合、またその対象日がclose_by_date_by_ticker
    側に存在しない場合(取得した履歴の範囲外等)は、無理に埋めずnullのまま
    次回以降に委ねる(「取れない場合だけnullのまま」、design確定仕様)。

    day0_date/day0_price、およびすでに確定済みのdayN_date/dayN_priceは
    一切書き換えない。Q5から外れていても追記対象から除外しない。
    """
    anchor_idx = trading_day_index(trading_calendar, anchor_date)
    updated = []
    for entry in entries:
        new_entry = dict(entry)
        if anchor_idx is not None:
            day0_idx = trading_day_index(trading_calendar, entry["day0_date"])
            if day0_idx is not None:
                ticker_closes = close_by_date_by_ticker.get(entry["ticker"]) or {}
                for n in HOLD_CHECKPOINTS:
                    date_key, price_key = f"day{n}_date", f"day{n}_price"
                    if new_entry[date_key] is not None:
                        continue  # 既に確定済み。再計算・変更しない。
                    target_idx = day0_idx + n
                    if target_idx > anchor_idx or target_idx >= len(trading_calendar):
                        continue  # まだその営業日に到達していない
                    target_date = trading_calendar[target_idx]
                    price = ticker_closes.get(target_date)
                    if price is not None:
                        new_entry[date_key] = target_date
                        new_entry[price_key] = price
                    # 見つからなければ何もしない(nullのまま、次回以降に再試行)
        updated.append(new_entry)
    return updated
