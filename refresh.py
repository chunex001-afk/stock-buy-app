"""日次の株価自動更新ジョブ、および手動更新から共通利用される更新ロジック。

GitHub Actions の schedule（.github/workflows/daily-refresh.yml）から
1日1回 `python refresh.py` として実行される想定のエントリポイント。
Render Web Service（app.py）はこのスクリプトが書き込んだUpstash Redis
のデータを読むだけで、自らAlpha Vantageへ新規アクセスはしない
（ただし app.py は本モジュールの `run_refresh` を import し、限定的な
手動更新にのみ再利用する）。

設計上の原則:
- 1銘柄の取得失敗が他銘柄の処理を止めない（銘柄ごとにtry/except）
- Alpha Vantage無料枠(25 req/day)を超えないよう、自己申告の予算
  (stock_logic.DAILY_API_BUDGET) を使い切ったら残りは前回データのまま
  スキップする
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


def run_refresh(tickers, api_key):
    """指定銘柄群を取得しRedisへ保存する。1銘柄の失敗は他に影響しない。

    GitHub Actionsの日次ジョブ（全銘柄）からも、app.pyの限定的な手動更新
    （1〜数銘柄）からも呼ばれる共通ロジック。

    戻り値: {"success": [...], "failed": [...], "skipped": [...]}
    """
    date_key = _today_str()
    tickers = [t.strip().upper() for t in tickers if t and t.strip()][: logic.MAX_TICKERS]

    success, failed, skipped = [], [], []

    for ticker in tickers:
        budget_left, _ = remaining_budget()
        if budget_left <= 0:
            print(f"[WARN] {ticker}: API予算を使い切ったためスキップ（前回データを維持）")
            skipped.append(ticker)
            continue

        try:
            dates, closes, volumes = logic.fetch_daily_series(ticker, api_key)
            store.incr_api_budget(date_key, 1)

            record = logic.build_result(ticker, dates, closes, volumes, news_items=[])
            record["fetched_at"] = _now_iso()
            record["is_stale"] = False
            record["last_error"] = None
            record["last_error_message"] = None
            record["last_error_at"] = None

            # 既存レコードにニュースが付いていれば引き継ぐ（無ければ空のまま）
            old = store.get_ticker_record(ticker) or {}
            record["news"] = old.get("news", [])

            store.set_ticker_record(ticker, record)
            success.append(ticker)
            print(f"[OK] {ticker}: {record['judgment']}（最終取引日 {record['last_trade_date']}）")

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

    # ニュースはまとめて1コール（予算が残っていて、対象銘柄が複数ある場合のみ）
    news_items = []
    budget_left, _ = remaining_budget()
    if budget_left > 0 and success:
        try:
            news_items = logic.fetch_news(success, api_key)
            store.incr_api_budget(date_key, 1)
        except Exception as e:
            print(f"[WARN] ニュース取得に失敗しました: {e}", file=sys.stderr)

    if news_items:
        for ticker in success:
            try:
                rec = store.get_ticker_record(ticker) or {}
                matched = [n for n in news_items if ticker in n.get("tickers", [])][:3]
                if matched:
                    rec["news"] = matched
                    store.set_ticker_record(ticker, rec)
            except Exception as e:
                print(f"[WARN] {ticker}: ニュースの反映に失敗しました: {e}", file=sys.stderr)

    return {"success": success, "failed": failed, "skipped": skipped}


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
