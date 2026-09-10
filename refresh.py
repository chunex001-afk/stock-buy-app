"""日次の株価自動更新ジョブ、および手動更新・銘柄追加時の即時取得から共通利用される更新ロジック。

GitHub Actions の schedule（.github/workflows/daily-refresh.yml）から
1日1回 `python refresh.py` として実行される想定のエントリポイント。
Render Web Service（app.py）はこのスクリプトが書き込んだUpstash Redis
のデータを読むだけで、日常的には自らAlpha Vantageへ新規アクセスはしない。
ただし app.py は本モジュールの `run_refresh` を import し、
(1) 未取得/失敗銘柄だけの限定的な手動更新、および
(2) 銘柄追加直後の即時取得（1銘柄のみ）
の2箇所で再利用する。

設計上の原則:
- 1銘柄の取得失敗が他銘柄の処理を止めない（銘柄ごとにtry/except）
- Alpha Vantage無料枠(25 req/day)を超えないよう、自己申告の予算
  (stock_logic.DAILY_API_BUDGET) を使い切ったら残りは前回データのまま
  スキップする
- 株価取得を最優先し、ニュース・時価総額（OVERVIEW）は株価取得後に
  予算が残っている場合だけ呼ぶ（ニュースは対象銘柄まとめて1コール、
  時価総額は1回の実行につき最大1銘柄のみ・30日に1回程度の頻度）
- Redis接続断など想定外の例外でもプロセス全体をクラッシュさせない
"""

import os
import sys
from datetime import datetime, timezone

import redis_store as store
import stock_logic as logic


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def remaining_budget():
    """(残りコール数, 本日の使用済みコール数) を返す。"""
    used = store.get_api_budget(_today_str())
    return logic.DAILY_API_BUDGET - used, used


def _mark_stale(ticker, error_type, message):
    """取得失敗時、前回の正常データを保持したまま失敗理由だけを上書きする。"""
    try:
        rec = store.get_ticker_record(ticker) or {"ticker": ticker}
        rec["is_stale"] = True
        rec["last_error"] = error_type
        rec["last_error_message"] = message
        rec["last_error_at"] = _now_iso()
        store.set_ticker_record(ticker, rec)
    except Exception as e:  # Redis書き込み自体の失敗もジョブを止めない
        print(f"[WARN] {ticker}: 失敗記録の保存にも失敗しました: {e}", file=sys.stderr)


def _is_market_cap_stale(rec):
    if rec.get("market_cap") is None:
        return True
    fetched_at = rec.get("market_cap_fetched_at")
    if not fetched_at:
        return True
    try:
        dt = datetime.fromisoformat(fetched_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days >= 30


def _pick_and_fetch_stale_cap(tickers, api_key, date_key):
    """時価総額が未取得/30日以上古い銘柄のうち先頭1件だけOVERVIEWを取得する。
    1回の実行につきAPIコールは最大1回に制限してAlpha Vantageの無料枠を守る。"""
    budget_left, _ = remaining_budget()
    if budget_left <= 0:
        return None, None

    for ticker in tickers:
        rec = store.get_ticker_record(ticker) or {}
        if not _is_market_cap_stale(rec):
            continue
        try:
            info = logic.fetch_overview(ticker, api_key)
            store.incr_api_budget(date_key, 1)
            return ticker, info
        except logic.ApiError as e:
            store.incr_api_budget(date_key, 1)
            print(f"[WARN] {ticker}: 時価総額（OVERVIEW）取得に失敗: {e.error_type} - {e.message}")
            return None, None
        except Exception as e:
            print(f"[WARN] {ticker}: 時価総額（OVERVIEW）取得中に予期しないエラー: {e}", file=sys.stderr)
            return None, None
    return None, None


def run_refresh(tickers, api_key):
    """指定銘柄群を取得しRedisへ保存する。1銘柄の失敗は他に影響しない。

    GitHub Actionsの日次ジョブ（全銘柄）からも、app.pyの限定的な手動更新
    （未取得/失敗分）・銘柄追加時の即時取得（1銘柄）からも呼ばれる共通ロジック。
    株価取得を最優先し、ニュース・時価総額は株価取得後に予算が残っていれば
    追加で取得する（＝APIキーの少ない銘柄追加時でも株価だけは即時反映されやすい）。

    戻り値: {"success": [...], "failed": [...], "skipped": [...]}
    """
    date_key = _today_str()
    tickers = [t.strip().upper() for t in tickers if t and t.strip()][: logic.MAX_TICKERS]

    success, failed, skipped = [], [], []
    fetched_price = {}

    for ticker in tickers:
        budget_left, _ = remaining_budget()
        if budget_left <= 0:
            print(f"[WARN] {ticker}: API予算を使い切ったためスキップ（前回データを維持）")
            skipped.append(ticker)
            continue

        try:
            dates, closes, volumes = logic.fetch_daily_series(ticker, api_key)
            store.incr_api_budget(date_key, 1)
            fetched_price[ticker] = (dates, closes, volumes)
        except logic.ApiError as e:
            store.incr_api_budget(date_key, 1)
            _mark_stale(ticker, e.error_type, e.message)
            failed.append({"ticker": ticker, "type": e.error_type, "message": e.message})
            print(f"[NG] {ticker}: {e.error_type} - {e.message}")
        except Exception as e:
            # 想定外の例外。予算は消費していない可能性が高いため加算しない。
            _mark_stale(ticker, "UNKNOWN", str(e))
            failed.append({"ticker": ticker, "type": "UNKNOWN", "message": str(e)})
            print(f"[NG] {ticker}: UNKNOWN - {e}")

    # ニュースは対象銘柄まとめて1コール（予算が残っていて、価格取得に成功した銘柄がある場合のみ）
    news_items = []
    budget_left, _ = remaining_budget()
    if budget_left > 0 and fetched_price:
        try:
            news_items = logic.fetch_news(list(fetched_price.keys()), api_key)
            store.incr_api_budget(date_key, 1)
        except Exception as e:
            print(f"[WARN] ニュース取得に失敗しました: {e}", file=sys.stderr)

    # 時価総額（OVERVIEW）は1回の実行につき最大1銘柄のみ
    cap_ticker, cap_info = (None, None)
    if fetched_price:
        cap_ticker, cap_info = _pick_and_fetch_stale_cap(list(fetched_price.keys()), api_key, date_key)

    for ticker, (dates, closes, volumes) in fetched_price.items():
        old = store.get_ticker_record(ticker) or {}
        if ticker == cap_ticker and cap_info:
            market_cap = cap_info["market_cap"]
            sector = cap_info["sector"]
            industry = cap_info["industry"]
            company_name = cap_info["name"]
            cap_fetched_at = _now_iso()
        else:
            market_cap = old.get("market_cap")
            sector = old.get("sector", "")
            industry = old.get("industry", "")
            company_name = old.get("company_name", "")
            cap_fetched_at = old.get("market_cap_fetched_at")

        try:
            record = logic.build_result(
                ticker, dates, closes, volumes, news_items=news_items,
                market_cap=market_cap, sector=sector, industry=industry, company_name=company_name,
            )
            record["fetched_at"] = _now_iso()
            record["is_stale"] = False
            record["last_error"] = None
            record["last_error_message"] = None
            record["last_error_at"] = None
            record["market_cap_fetched_at"] = cap_fetched_at

            store.set_ticker_record(ticker, record)
            success.append(ticker)
            print(f"[OK] {ticker}: {record['judgment']}（最終取引日 {record['last_trade_date']}）")
        except Exception as e:
            _mark_stale(ticker, "UNKNOWN", str(e))
            failed.append({"ticker": ticker, "type": "UNKNOWN", "message": str(e)})
            print(f"[NG] {ticker}: 判定計算中に予期しないエラー: {e}")

    return {"success": success, "failed": failed, "skipped": skipped}


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


def main():
    api_key = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ALPHAVANTAGE_API_KEYが設定されていません。処理を中止します。", file=sys.stderr)
        return 1

    tickers = store.get_watchlist()
    if not tickers:
        print("[INFO] watchlistが空のためDEFAULT_TICKERSを使用します。")
        tickers = list(logic.DEFAULT_TICKERS)
        try:
            store.set_watchlist(tickers)
        except Exception as e:
            print(f"[WARN] watchlistの初期化に失敗しました: {e}", file=sys.stderr)

    _, used_before = remaining_budget()
    print(f"[INFO] 本日のAPI使用実績: {used_before}回 / 自己申告上限 {logic.DAILY_API_BUDGET}回")

    result = run_refresh(tickers, api_key)
    update_rank_snapshot(tickers)

    _, used_after = remaining_budget()
    summary = {
        "run_at": _now_iso(),
        "success": result["success"],
        "failed": result["failed"],
        "skipped": result["skipped"],
        "api_calls_used_today": used_after,
        "api_budget": logic.DAILY_API_BUDGET,
    }
    try:
        store.set_last_refresh(summary)
    except Exception as e:
        print(f"[WARN] last_refreshサマリーの保存に失敗しました: {e}", file=sys.stderr)

    print(
        f"[DONE] 成功 {len(result['success'])}件 / 失敗 {len(result['failed'])}件 / "
        f"スキップ {len(result['skipped'])}件 / 本日のAPI使用 {used_after}回"
    )
    # 一部失敗があってもプロセス自体は正常終了させる（他銘柄は正常に更新済みのため）
    return 0


if __name__ == "__main__":
    sys.exit(main())
