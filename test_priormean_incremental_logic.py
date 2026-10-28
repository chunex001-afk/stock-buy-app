# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)Phase 1のテスト。

Redis(Upstash)へは一切アクセスしない(他の*_state系テストと同じ方針)。
redis_store.get_priormean_bounds_history/get_priormean_backfill_status/
set_priormean_backfill_status/get_priormean_events/set_priormean_eventsを
フェイクのインメモリ辞書に差し替え、backfill_new_ticker_prior_mean.
backfill_one_ticker()をFORM(実データ、.q5_price_cache/FORM.json)に対して
実際に実行する。

FORMの価格データ(.q5_price_cache/FORM.json)・bounds_history(Phase 0が
生成した_priormean_bounds_history_local_copy.json)は、いずれもこの
テストファイルが新規に作ったものではなく、既存の研究・Phase 0成果物を
そのまま読み込むだけ。

このテストは、FORM価格データ・Phase 0生成物が無い環境では意味を成さない
ため、どちらかが欠けている場合は明示的にskipする(CI等、将来的に
.q5_price_cache/FORM.jsonを含まない環境でも全体のテストスイートが失敗
しないようにするため)。
"""
import hashlib
import json
import os
import unittest
from unittest import mock

import q5_prior_mean_logic as pml
import priormean_incremental_logic as inc
import redis_store as store
import backfill_new_ticker_prior_mean as backfill_script

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCE_JSON_PATH = os.path.join(BASE_DIR, "q5_prior_mean_reference.json")
BOUNDS_HISTORY_PATH = os.path.join(BASE_DIR, "_priormean_bounds_history_local_copy.json")
FORM_PRICE_CACHE_PATH = os.path.join(BASE_DIR, ".q5_price_cache", "FORM.json")

EXPECTED_REFERENCE_JSON_SHA256 = (
    "db74cb856596c6e346437edc63714dbc89c7ae1a15de2750da07dd9a85ee587b"
)

# 研究セッションで確認済みの期待値(以前の独立した検証、REFERENCE_UNIVERSE
# の生スコアを使って計算したものと、bounds_history経由で計算したものの
# 両方で一致を確認済み)。
EXPECTED_FORM_DAY0_COUNT = 343
EXPECTED_FORM_RESOLVED_COUNT = 340
EXPECTED_FORM_UNRESOLVED_COUNT = 3
# get_prior_mean/get_prior_stats(anchor="2026-10-27"、340件全部が
# prior扱いになる日付)で実際に計算した値(Phase 1実装時に実測、
# 2026-10-27)。
EXPECTED_FORM_PRIOR_MEAN = 0.6
EXPECTED_FORM_PRIOR_MEDIAN = 0.4
EXPECTED_FORM_PRIOR_WIN_RATE = 51.2
EXPECTED_FORM_LABEL = "🟡 標準"


def _prereqs_available():
    return os.path.exists(BOUNDS_HISTORY_PATH) and os.path.exists(FORM_PRICE_CACHE_PATH)


class _FakeRedisBackend:
    """redis_store.get_json/set_jsonだけを差し替える、インメモリのフェイク
    バックエンド(test_riskoff_state.py等、既存テストと同じ手法)。実際の
    Upstashには一切接続しない。"""

    def __init__(self, bounds_history):
        self.db = {"priormean:bounds_history": bounds_history}

    def get_json(self, key, default=None):
        return self.db.get(key, default)

    def set_json(self, key, value, ex=None):
        self.db[key] = value
        return True


@unittest.skipUnless(_prereqs_available(), "Phase 0のbounds_history / FORM価格キャッシュが無いためスキップ")
class Phase1FormBackfillTests(unittest.TestCase):
    """FORMを実データで検証する一連のテスト(Test1〜9に対応)。

    FORM自身のpred_score計算(quintile_logic.compute_features/
    knn_predict_score、無変更)は1銘柄・約20年分で数分かかるため、
    setUpClassで1回だけ計算してクラス全体で使い回す(実際のスコア計算が
    正しく動くこと自体は、このセッションの研究で既に複数回・独立に
    確認済み〈343/340/3件で完全一致〉。ここでのテストの主眼はPhase 1の
    検出・バックフィル・マージ・冪等性のロジックであり、重い計算を
    テストのたびに繰り返す必要はないため、backfill_new_ticker_prior_mean.
    compute_ticker_scoresだけをモックして使い回す)。"""

    @classmethod
    def setUpClass(cls):
        with open(BOUNDS_HISTORY_PATH, encoding="utf-8") as f:
            cls.bounds_history = json.load(f)
        cls.static_data = pml.load_reference_data()

        import quintile_logic
        dates, closes, volumes = backfill_script.load_price_series("FORM", backfill_script.PRICE_CACHE_DIR_DEFAULT)
        spy_dates, spy_closes, _ = backfill_script.load_price_series("SPY", backfill_script.PRICE_CACHE_DIR_DEFAULT)
        reference_train = quintile_logic.load_reference_data()
        cls.form_scores_by_date = backfill_script.compute_ticker_scores(
            dates, closes, volumes, spy_dates, spy_closes, reference_train,
        )

    def _run_backfill(self, fake_backend):
        with mock.patch.object(store, "get_json", side_effect=fake_backend.get_json), \
             mock.patch.object(store, "set_json", side_effect=fake_backend.set_json), \
             mock.patch.object(backfill_script, "compute_ticker_scores", return_value=self.form_scores_by_date):
            return backfill_script.backfill_one_ticker("FORM")

    # --- Test 2 / 3: FORM Day0件数・確定件数 ---
    def test_form_backfill_counts(self):
        fake = _FakeRedisBackend(self.bounds_history)
        outcome = self._run_backfill(fake)
        self.assertFalse(outcome["skipped"])
        result = outcome["result"]
        self.assertEqual(result["day0_count"], EXPECTED_FORM_DAY0_COUNT)
        self.assertEqual(len(result["resolved_events"]), EXPECTED_FORM_RESOLVED_COUNT)
        self.assertEqual(len(result["unresolved_day0_dates"]), EXPECTED_FORM_UNRESOLVED_COUNT)

    # --- Test 6: 20営業日未満のDay0はret20未確定のまま(確定リストに入らない) ---
    def test_unresolved_events_are_never_finalized(self):
        fake = _FakeRedisBackend(self.bounds_history)
        outcome = self._run_backfill(fake)
        resolved_dates = {e["date"] for e in outcome["result"]["resolved_events"]}
        unresolved_dates = set(outcome["result"]["unresolved_day0_dates"])
        self.assertEqual(resolved_dates & unresolved_dates, set())
        self.assertEqual(len(unresolved_dates), EXPECTED_FORM_UNRESOLVED_COUNT)
        # 保存されたイベント数は確定分だけ(未確定の3件は含まれない)。
        saved_events = fake.db["priormean:events:FORM"]
        self.assertEqual(len(saved_events), EXPECTED_FORM_RESOLVED_COUNT)

    # --- Test 4 / 5: prior_n・prior_mean・prior_median・prior_win_rateが
    #     研究時の値と一致(merge_reference_data経由でget_prior_mean/
    #     get_prior_statsを呼ぶ。両関数自体は無変更のまま使う) ---
    def test_form_prior_stats_via_merged_data(self):
        fake = _FakeRedisBackend(self.bounds_history)
        self._run_backfill(fake)
        form_events = fake.db["priormean:events:FORM"]

        merged = inc.merge_reference_data(self.static_data, {"FORM": form_events})
        anchor = "2026-10-27"
        pm = pml.get_prior_mean("FORM", anchor, data=merged)
        ps = pml.get_prior_stats("FORM", anchor, data=merged)

        self.assertEqual(pm["status"], "ok")
        self.assertEqual(pm["n"], EXPECTED_FORM_RESOLVED_COUNT)
        self.assertEqual(pm["mean"], EXPECTED_FORM_PRIOR_MEAN)
        self.assertEqual(ps["n"], EXPECTED_FORM_RESOLVED_COUNT)
        self.assertEqual(ps["median"], EXPECTED_FORM_PRIOR_MEDIAN)
        self.assertEqual(ps["win_rate"], EXPECTED_FORM_PRIOR_WIN_RATE)

        label = pml.label_prior_mean_with_win_rate(pm, ps)
        self.assertEqual(label, EXPECTED_FORM_LABEL)

    # --- Test 7: 同じFORMを2回バックフィルしても重複しない(冪等性) ---
    def test_rerunning_backfill_is_idempotent(self):
        fake = _FakeRedisBackend(self.bounds_history)
        outcome1 = self._run_backfill(fake)
        self.assertFalse(outcome1["skipped"])
        events_after_first = fake.db["priormean:events:FORM"]
        self.assertEqual(len(events_after_first), EXPECTED_FORM_RESOLVED_COUNT)

        # 2回目: status=doneになっているため即スキップされ、イベントは
        # 一切変化しない。
        outcome2 = self._run_backfill(fake)
        self.assertTrue(outcome2["skipped"])
        self.assertEqual(outcome2["reason"], "already_done")
        events_after_second = fake.db["priormean:events:FORM"]
        self.assertEqual(events_after_second, events_after_first)
        self.assertEqual(len(events_after_second), EXPECTED_FORM_RESOLVED_COUNT)

    def test_dedup_events_does_not_duplicate_same_date(self):
        """priormean_incremental_logic.dedup_events単体でも、同じ日付の
        イベントが2件に増えないことを直接確認する(一意キー=date、
        ticker単位のリストなのでdateだけで判定)。"""
        existing = [{"date": "2026-01-06", "ret20": 1.0, "max_dd20": -1.0}]
        new = [
            {"date": "2026-01-06", "ret20": 999.0, "max_dd20": -999.0},  # 既存と重複する日付
            {"date": "2026-02-10", "ret20": 2.0, "max_dd20": -2.0},
        ]
        merged = inc.dedup_events(existing, new)
        self.assertEqual(len(merged), 2)
        jan6 = next(e for e in merged if e["date"] == "2026-01-06")
        self.assertEqual(jan6["ret20"], 1.0, "既存イベントが新規計算で上書きされてはいけない")

    # --- Test 1 / 8: 既存56銘柄への影響が無いこと ---
    def test_existing_56_tickers_unaffected_by_form_backfill(self):
        with open(REFERENCE_JSON_PATH, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(actual_hash, EXPECTED_REFERENCE_JSON_SHA256,
                          "FORMバックフィル後もq5_prior_mean_reference.jsonは無変更のはず")

        fake = _FakeRedisBackend(self.bounds_history)
        outcome = self._run_backfill(fake)
        form_events = outcome["merged_events"]

        merged = inc.merge_reference_data(self.static_data, {"FORM": form_events})
        for ticker in ("NVDA", "MU", "AEHR", "SNDK", "CRDO", "MXL", "ANET", "PANW", "TSM", "JNJ"):
            for anchor in ("2018-01-01", "2026-10-27"):
                before = pml.get_prior_mean(ticker, anchor, data=self.static_data)
                after = pml.get_prior_mean(ticker, anchor, data=merged)
                self.assertEqual(before, after, f"{ticker}@{anchor} がFORM追加の影響を受けている")

    def test_merge_never_overrides_existing_ticker_even_if_redis_has_same_key(self):
        """防御的実装の確認: static_dataに既にあるticker名がredis側にも
        存在する(通常は起こらないはずの異常系)場合でも、static側が常に
        優先されることを確認する。"""
        poisoned_redis_events = {"NVDA": [{"date": "1999-01-01", "ret20": 9999.0, "max_dd20": 0.0}]}
        merged = inc.merge_reference_data(self.static_data, poisoned_redis_events)
        self.assertEqual(
            merged["events_by_ticker"]["NVDA"],
            self.static_data["events_by_ticker"]["NVDA"],
        )

    # --- Test 9: 未来データ混入防止 ---
    def test_no_future_data_leaks_into_form_prior_mean(self):
        fake = _FakeRedisBackend(self.bounds_history)
        outcome = self._run_backfill(fake)
        form_events = outcome["merged_events"]
        merged = inc.merge_reference_data(self.static_data, {"FORM": form_events})

        # FORMの確定済みイベントのうち、中間あたりの日付をanchorに選ぶ。
        sorted_events = sorted(form_events, key=lambda e: e["date"])
        mid_index = len(sorted_events) // 2
        anchor_date = sorted_events[mid_index]["date"]

        ps = pml.get_prior_stats("FORM", anchor_date, data=merged)
        expected_n = sum(1 for e in form_events if e["date"] < anchor_date)
        self.assertEqual(ps["n"], expected_n)
        # anchor_date以降のイベントが混入していないことを、件数だけでなく
        # 個別に確認する(平均値が未来イベントの影響を受けていないか)。
        prior_only_mean = sum(e["ret20"] for e in form_events if e["date"] < anchor_date) / expected_n
        self.assertAlmostEqual(ps["mean"], round(prior_only_mean, 1), places=1)

    def test_backfill_does_not_recompute_reference_universe(self):
        """48銘柄(REFERENCE_UNIVERSE/ROTATION_UNIVERSE)という属性に実際に
        コード上でアクセスしていないことを、AST(抽象構文木)で静的に確認
        する(docstring中の説明文としての言及〈実際にこのテストが最初に
        引っかかったのはこれが原因〉は許容し、実行時に本当にその属性へ
        アクセスする式が無いことだけを厳密にチェックする)。"""
        import ast
        import inspect

        def _attribute_accesses(module):
            src = inspect.getsource(module)
            tree = ast.parse(src)
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            return names

        for module in (backfill_script, inc):
            accessed = _attribute_accesses(module)
            self.assertNotIn("ROTATION_UNIVERSE", accessed, f"{module.__name__}がROTATION_UNIVERSEにアクセスしている")
            self.assertNotIn("REFERENCE_UNIVERSE", accessed, f"{module.__name__}がREFERENCE_UNIVERSEにアクセスしている")


class DetectNewTickersTests(unittest.TestCase):
    """Redis・実データ不要の軽量な純粋関数テスト。"""

    def test_detects_only_truly_new_tickers(self):
        watchlist = ["NVDA", "FORM", "MXL", "ABC"]
        static_tickers = {"NVDA", "MXL"}
        redis_status = {"ABC": "done"}
        result = inc.detect_new_tickers(watchlist, static_tickers, redis_status)
        self.assertEqual(result, ["FORM"])

    def test_pending_and_in_progress_are_excluded(self):
        watchlist = ["FORM", "XYZ"]
        result = inc.detect_new_tickers(watchlist, set(), {"FORM": "pending", "XYZ": "in_progress"})
        self.assertEqual(result, [])

    def test_failed_tickers_are_retried(self):
        watchlist = ["FORM"]
        result = inc.detect_new_tickers(watchlist, set(), {"FORM": "failed"})
        self.assertEqual(result, ["FORM"])

    def test_watchlist_order_preserved(self):
        watchlist = ["C", "A", "B"]
        result = inc.detect_new_tickers(watchlist, set(), {})
        self.assertEqual(result, ["C", "A", "B"])


class DetectNewTickersForOvernightBackfillTests(unittest.TestCase):
    """design 2026-10-28c(24:00 JST締切・深夜バックフィル)専用の新規関数。
    Redis・実データ不要の軽量な純粋関数テスト。"""

    CUTOFF = "2026-10-28T00:00:00+09:00"  # 今日0:00 JST(=前日24:00 JST)

    def test_added_before_cutoff_is_included(self):
        watchlist = ["FORM"]
        added_at = {"FORM": "2026-10-27T23:50:00+09:00"}  # 前日24:00 JSTより前
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, ["FORM"])

    def test_added_after_cutoff_is_excluded(self):
        watchlist = ["FORM"]
        added_at = {"FORM": "2026-10-28T00:05:00+09:00"}  # 今日0:00 JSTより後
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, [])

    def test_no_added_at_record_is_included_by_default(self):
        """この機能導入前からwatchlistに存在していた銘柄(例: FORM、記録が
        無い)は締切の対象外として常に含める(安全側のデフォルト、永久に
        検出されなくなることを防ぐ)。"""
        watchlist = ["FORM"]
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {}, {}, self.CUTOFF,
        )
        self.assertEqual(result, ["FORM"])

    def test_static_ticker_still_excluded(self):
        watchlist = ["NVDA"]
        added_at = {"NVDA": "2026-10-27T10:00:00+09:00"}
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, {"NVDA"}, {}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, [])

    def test_pending_status_still_excluded_even_if_before_cutoff(self):
        watchlist = ["FORM"]
        added_at = {"FORM": "2026-10-27T10:00:00+09:00"}
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {"FORM": "pending"}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, [])

    def test_failed_ticker_before_cutoff_is_retried(self):
        watchlist = ["FORM"]
        added_at = {"FORM": "2026-10-27T10:00:00+09:00"}
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {"FORM": "failed"}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, ["FORM"])

    def test_mixed_before_and_after_cutoff(self):
        watchlist = ["EARLY", "LATE", "OLD"]
        added_at = {
            "EARLY": "2026-10-27T23:59:59+09:00",  # 前日24:00 JST直前→含める
            "LATE": "2026-10-28T00:00:01+09:00",    # 今日0:00 JST直後→除外
            # "OLD"は記録無し→含める
        }
        result = inc.detect_new_tickers_for_overnight_backfill(
            watchlist, set(), {}, added_at, self.CUTOFF,
        )
        self.assertEqual(result, ["EARLY", "OLD"])


if __name__ == "__main__":
    unittest.main()
