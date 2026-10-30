# -*- coding: utf-8 -*-
"""design 2026-10-28b/2026-10-30: watchlist「Q5過去平均」パネルの1行目(JS、
wlpmLine1Html)が、新規銘柄のbackfill_status・in_reference_universeに応じて
正しい文言・配色クラスを返すことを、実際にNode.jsでapp.HTML内のJS関数を
そのまま実行して確認する(文字列の有無をチェックするだけの簡易テストでは
なく、実際の分岐ロジックを動かして検証する)。

2026-10-30更新: ウォッチリスト追加直後(backfill_status=nullの数十秒間)に
「データ不足対象外」と誤表示される問題の修正に伴い、wlpmLine1Htmlの第5引数
inRef(in_reference_universe)を追加した。backfill_status=nullの挙動が
inRefで分岐するようになったため、関連テストを更新・追加している。

app.py側の表示文言ロジック(JS)自体は、このテストファイルでは一切変更
しない。Pythonのunittestからサブプロセスでnodeを呼ぶだけ。
"""
import json
import subprocess
import unittest

import app

NODE_AVAILABLE = True
try:
    subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
    NODE_AVAILABLE = False


def _extract_function(source, start_marker):
    """指定したfunction宣言の開始文字列から、対応する閉じ波括弧までの
    ソースをそのまま切り出す(テンプレートリテラル内の${...}も含めて
    波括弧の対応を数えるだけの単純な実装、app.HTML自体は一切変更しない
    読み取り専用の抽出)。"""
    start = source.index(start_marker)
    brace_start = source.index("{", start)
    depth = 0
    i = brace_start
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return source[start:i + 1]


def _run_wlpm_line1_html(pm, label, stats, backfill_status, in_ref=True):
    """app.HTML内のesc()・wlpmLine1Html()を実際にNode.jsで実行し、戻り値
    (HTML文字列)を取得する。in_refの既定値はTrue(既存56銘柄相当、
    既存テストの呼び出し互換を保つため)。"""
    esc_src = _extract_function(app.HTML, "function esc(")
    wlpm_src = _extract_function(app.HTML, "function wlpmLine1Html(")
    args = json.dumps([pm, label, stats, backfill_status, in_ref], ensure_ascii=False)
    script = f"""
{esc_src}
{wlpm_src}
const args = {args};
process.stdout.write(wlpmLine1Html(args[0], args[1], args[2], args[3], args[4]));
"""
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, timeout=10, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"node実行エラー: {result.stderr}")
    return result.stdout


_INSUFFICIENT_PM = {"status": "insufficient", "n": 3, "mean": None}
_OK_PM = {"status": "ok", "n": 42, "mean": 2.4}
_OK_STATS = {"status": "ok", "n": 42, "mean": 2.4, "median": 2.0, "win_rate": 55.0}


@unittest.skipUnless(NODE_AVAILABLE, "node.jsが使えないためスキップ")
class WlpmLine1HtmlBackfillStatusTests(unittest.TestCase):
    """1〜6: backfill_status x in_reference_universeの組み合わせで正しい
    表示文言になることの確認(実際のJS関数を実行、文字列照合のみの簡易
    テストではない)。"""

    def test_1_pending_shows_waiting_message(self):
        # design 2026-10-30: pendingとin_progressを別文言に分けた
        # (「取得待ち」と「取得中」を区別するユーザー確定仕様)。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "pending", True)
        self.assertIn("🔄 データ取得待ち…", html)
        self.assertIn("wlpm-flag-pending", html)
        self.assertNotIn("wlpm-flag-warn", html)

    def test_2_in_progress_shows_fetching_message(self):
        # design 2026-10-30b: 即時dispatchは数分で完了するため、
        # 「翌朝7:00以降に表示」ではなく「数分後に表示」に変更。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "in_progress", True)
        self.assertIn("🔄 データ取得中｜数分後に表示", html)
        self.assertIn("wlpm-flag-pending", html)
        self.assertNotIn("データ取得待ち", html)

    def test_3_failed_shows_retry_message(self):
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "failed", True)
        self.assertIn("⚠️ データ取得失敗", html)
        self.assertIn("wlpm-flag-warn", html)
        self.assertNotIn("wlpm-flag-pending", html)

    def test_4_done_with_insufficient_n_and_in_ref_shows_count_insufficient(self):
        # design 2026-10-28bのユーザー訂正: 「データ不足」ではなく「件数不足」。
        # in_reference_universe=True(イベント自体は存在する)の場合。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "done", True)
        self.assertIn("件数不足", html)
        self.assertIn("(過去3件)", html)
        self.assertNotIn("データ不足", html)
        self.assertIn("wlpm-flag-warn", html)

    def test_5_done_without_in_ref_shows_nothing_defers_to_ref_flag(self):
        # design 2026-10-30追加: バックフィル完了済みなのにイベントが1件も
        # 無い(=待っても解決しない本当の対象外)場合は、ここでは何も出さず
        # 呼び出し元のrefFlag(「対象外」)だけに任せる(重複表示防止)。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "done", False)
        self.assertEqual(html, "")

    def test_6_status_none_with_in_ref_keeps_legacy_data_insufficient_text(self):
        # 既存56銘柄(例JNJ、backfill_statusはそもそも問い合わせない=null、
        # in_reference_universe=True)の挙動が変わらないこと。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, None, True)
        self.assertIn("データ不足", html)
        self.assertIn("(過去3件)", html)
        self.assertNotIn("件数不足", html)
        self.assertNotIn("wlpm-flag-pending", html)

    def test_7_status_none_without_in_ref_shows_fetching_message(self):
        # design 2026-10-30追加(今回の修正の本題): ウォッチリスト追加直後、
        # GitHub Actionsがまだbackfill_status="in_progress"を書き込む前の
        # 新規銘柄(backfill_status=null、in_reference_universe=False)は
        # 「データ不足」ではなく「取得中」と表示する。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, None, False)
        self.assertIn("🔄 データ取得中｜数分後に表示", html)
        self.assertIn("wlpm-flag-pending", html)
        self.assertNotIn("データ不足", html)
        self.assertNotIn("wlpm-flag-warn", html)

    def test_pending_with_zero_n_does_not_show_count_suffix(self):
        pm0 = {"status": "insufficient", "n": 0, "mean": None}
        html = _run_wlpm_line1_html(pm0, None, None, "pending", True)
        self.assertIn("🔄 データ取得待ち…", html)
        self.assertNotIn("(過去", html)

    def test_ok_status_ignores_backfill_status_and_in_ref_entirely(self):
        # prior_mean.status === "ok"の場合はbackfillStatus・inRefを一切見ない
        # (既存の表示ロジックが変わっていないことの確認)。
        for status in ("pending", "in_progress", "failed", "done", None):
            for in_ref in (True, False):
                html = _run_wlpm_line1_html(_OK_PM, None, _OK_STATS, status, in_ref)
                self.assertIn("+2.4%", html)
                self.assertNotIn("データ不足", html)
                self.assertNotIn("件数不足", html)
                self.assertNotIn("🔄", html)
                self.assertNotIn("⚠️", html)


def _extract_const(source, start_marker):
    """`const NAME = {...};` 形式の宣言をそのまま切り出す(_extract_function
    と同じ波括弧対応カウント、app.HTML自体は一切変更しない読み取り専用の
    抽出)。"""
    start = source.index(start_marker)
    brace_start = source.index("{", start)
    depth = 0
    i = brace_start
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = source.index(";", i) + 1
    return source[start:end]


def _run_render_watchlist_prior_means(list_items):
    """app.HTML内の関連関数群を実際にNode.jsで実行し、renderWatchlistPrior
    Means()がdocument.getElementByIdで取得した要素へ代入するinnerHTML文字列
    を取得する(documentはinnerHTML代入だけ記録するスタブ)。"""
    esc_src = _extract_function(app.HTML, "function esc(")
    fmt_date_src = _extract_function(app.HTML, "function fmtDate(")
    wlpm1_src = _extract_function(app.HTML, "function wlpmLine1Html(")
    wlpm2_src = _extract_function(app.HTML, "function wlpmLine2Html(")
    wlpm3_src = _extract_function(app.HTML, "function wlpmLine3Html(")
    priority_src = _extract_const(app.HTML, "const WLPM_CATEGORY_PRIORITY")
    sorted_src = _extract_function(app.HTML, "function wlpmSortedList(")
    render_src = _extract_function(app.HTML, "function renderWatchlistPriorMeans(")
    items = json.dumps(list_items, ensure_ascii=False)
    script = f"""
let __out = null;
const document = {{ getElementById: () => ({{ set innerHTML(v) {{ __out = v; }} }}) }};
{esc_src}
{fmt_date_src}
{wlpm1_src}
{wlpm2_src}
{wlpm3_src}
{priority_src}
{sorted_src}
{render_src}
renderWatchlistPriorMeans({items}, "2026-10-29");
process.stdout.write(__out);
"""
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, timeout=10, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"node実行エラー: {result.stderr}")
    return result.stdout


def _watchlist_item(ticker, backfill_status, in_reference_universe):
    return {
        "ticker": ticker,
        "prior_mean": _INSUFFICIENT_PM,
        "prior_mean_label": None,
        "prior_stats": None,
        "prior_stats_recent5y": None,
        "backfill_status": backfill_status,
        "in_reference_universe": in_reference_universe,
    }


@unittest.skipUnless(NODE_AVAILABLE, "node.jsが使えないためスキップ")
class RenderWatchlistPriorMeansRefFlagTests(unittest.TestCase):
    """design 2026-10-30: renderWatchlistPriorMeans()のrefFlag(「対象外」)が、
    backfill_status="done" かつ in_reference_universe=False の時だけ出て、
    それ以外(新規銘柄がまだバックフィル処理の途中)では出ないことの確認
    (wlpmLine1Html単体ではなく、実際にrenderWatchlistPriorMeans全体を実行
    して検証する)。"""

    def test_done_and_not_in_ref_shows_taishogai_flag(self):
        html = _run_render_watchlist_prior_means([_watchlist_item("ZZZZ", "done", False)])
        self.assertIn("対象外", html)
        self.assertNotIn("データ不足", html)

    def test_none_and_not_in_ref_does_not_show_taishogai_flag(self):
        # 今回の修正の本題: ウォッチリスト追加直後(backfill_status=null)は
        # 「対象外」を出さず、wlpmLine1Html側の「🔄 データ取得中｜数分後に
        # 表示」だけを出す。
        html = _run_render_watchlist_prior_means([_watchlist_item("RKLB", None, False)])
        self.assertIn("🔄 データ取得中｜数分後に表示", html)
        self.assertNotIn("対象外", html)

    def test_pending_and_not_in_ref_does_not_show_taishogai_flag(self):
        html = _run_render_watchlist_prior_means([_watchlist_item("RKLB", "pending", False)])
        self.assertIn("🔄 データ取得待ち…", html)
        self.assertNotIn("対象外", html)

    def test_in_progress_and_not_in_ref_does_not_show_taishogai_flag(self):
        html = _run_render_watchlist_prior_means([_watchlist_item("RKLB", "in_progress", False)])
        self.assertIn("🔄 データ取得中｜数分後に表示", html)
        self.assertNotIn("対象外", html)

    def test_failed_and_not_in_ref_does_not_show_taishogai_flag(self):
        html = _run_render_watchlist_prior_means([_watchlist_item("RKLB", "failed", False)])
        self.assertIn("⚠️ データ取得失敗", html)
        self.assertNotIn("対象外", html)

    def test_static_ticker_none_and_in_ref_shows_legacy_data_insufficient(self):
        # 既存56銘柄(backfill_status=null、in_reference_universe=True)は
        # 従来通り「データ不足」のまま、「対象外」は出ない。
        html = _run_render_watchlist_prior_means([_watchlist_item("JNJ", None, True)])
        self.assertIn("データ不足", html)
        self.assertNotIn("対象外", html)
        self.assertNotIn("🔄", html)


if __name__ == "__main__":
    unittest.main()
