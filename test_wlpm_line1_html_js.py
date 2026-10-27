# -*- coding: utf-8 -*-
"""design 2026-10-28b: watchlist「Q5過去平均」パネルの1行目(JS、
wlpmLine1Html)が、新規銘柄のbackfill_statusに応じて正しい文言・配色
クラスを返すことを、実際にNode.jsでapp.HTML内のJS関数をそのまま実行して
確認する(文字列の有無をチェックするだけの簡易テストではなく、実際の
分岐ロジックを動かして検証する)。

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


def _run_wlpm_line1_html(pm, label, stats, backfill_status):
    """app.HTML内のesc()・wlpmLine1Html()を実際にNode.jsで実行し、戻り値
    (HTML文字列)を取得する。"""
    esc_src = _extract_function(app.HTML, "function esc(")
    wlpm_src = _extract_function(app.HTML, "function wlpmLine1Html(")
    args = json.dumps([pm, label, stats, backfill_status], ensure_ascii=False)
    script = f"""
{esc_src}
{wlpm_src}
const args = {args};
process.stdout.write(wlpmLine1Html(args[0], args[1], args[2], args[3]));
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
    """1〜5: backfill_statusの5パターンで正しい表示文言になることの確認
    (実際のJS関数を実行、文字列照合のみの簡易テストではない)。"""

    def test_1_pending_shows_auto_fetch_message(self):
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "pending")
        self.assertIn("🕐 データ不足｜23:50以降に自動取得", html)
        self.assertIn("wlpm-flag-pending", html)
        self.assertNotIn("wlpm-flag-warn", html)

    def test_2_in_progress_shows_same_auto_fetch_message(self):
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "in_progress")
        self.assertIn("🕐 データ不足｜23:50以降に自動取得", html)
        self.assertIn("wlpm-flag-pending", html)

    def test_3_failed_shows_retry_message(self):
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "failed")
        self.assertIn("⚠️ データ取得失敗｜再試行予定", html)
        self.assertIn("wlpm-flag-warn", html)
        self.assertNotIn("wlpm-flag-pending", html)

    def test_4_done_with_insufficient_n_shows_count_insufficient(self):
        # design 2026-10-28bのユーザー訂正: 「データ不足」ではなく「件数不足」。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, "done")
        self.assertIn("件数不足", html)
        self.assertIn("(過去3件)", html)
        self.assertNotIn("データ不足", html)
        self.assertIn("wlpm-flag-warn", html)

    def test_5_status_none_keeps_legacy_data_insufficient_text(self):
        # 既存56銘柄(例JNJ、backfill_statusはそもそも問い合わせない=null)の
        # 挙動が変わらないこと。
        html = _run_wlpm_line1_html(_INSUFFICIENT_PM, None, None, None)
        self.assertIn("データ不足", html)
        self.assertIn("(過去3件)", html)
        self.assertNotIn("件数不足", html)
        self.assertNotIn("wlpm-flag-pending", html)

    def test_pending_with_zero_n_does_not_show_count_suffix(self):
        pm0 = {"status": "insufficient", "n": 0, "mean": None}
        html = _run_wlpm_line1_html(pm0, None, None, "pending")
        self.assertIn("🕐 データ不足｜23:50以降に自動取得", html)
        self.assertNotIn("(過去", html)

    def test_ok_status_ignores_backfill_status_entirely(self):
        # prior_mean.status === "ok"の場合はbackfillStatusを一切見ない
        # (既存の表示ロジックが変わっていないことの確認)。
        for status in ("pending", "in_progress", "failed", "done", None):
            html = _run_wlpm_line1_html(_OK_PM, None, _OK_STATS, status)
            self.assertIn("+2.4%", html)
            self.assertNotIn("データ不足", html)
            self.assertNotIn("件数不足", html)
            self.assertNotIn("🕐", html)
            self.assertNotIn("⚠️", html)


if __name__ == "__main__":
    unittest.main()
