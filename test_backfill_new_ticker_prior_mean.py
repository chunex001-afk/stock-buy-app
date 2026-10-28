# -*- coding: utf-8 -*-
"""prior_mean差分更新の設計(2026-10-27研究・design確認済み)案B:
backfill_new_ticker_prior_mean.load_price_series()のTwelve Data
outputsize=5000フォールバック(新規ウォッチリスト銘柄の初回バックフィル
専用)のテスト。

Twelve Data・Upstashへは一切アクセスしない(twelvedata_client.
fetch_daily_series自体をmockで差し替える、twelvedata_client.py自体は
一切変更・呼び出しもしない)。既存56銘柄は常に.q5_price_cache/<ticker>.json
が存在するため、このフォールバック分岐には到達しない(既存56銘柄の挙動は
無変更)。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import backfill_new_ticker_prior_mean as backfill_script
import priormean_incremental_logic as inc
import q5_prior_mean_logic as pml
import redis_store as store
import twelvedata_client as td

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FORM_PRICE_CACHE_PATH = os.path.join(BASE_DIR, ".q5_price_cache", "FORM.json")
SPY_PRICE_CACHE_PATH = os.path.join(BASE_DIR, ".q5_price_cache", "SPY.json")
BOUNDS_HISTORY_PATH = os.path.join(BASE_DIR, "_priormean_bounds_history_local_copy.json")

# 2026-10-27の研究(調査フェーズ)で実測済みの、outputsize=5000フォールバック
# を使った場合のFORM期待値。完全履歴版(343/340/3、prior_mean+0.6%)とは
# 意図的に異なる値であり、「完全一致させる補正」は入れていないことの確認が
# このテストの主眼(ユーザー確定仕様)。
EXPECTED_FORM_5000_DAY0_COUNT = 334
EXPECTED_FORM_5000_RESOLVED_COUNT = 331
EXPECTED_FORM_5000_UNRESOLVED_COUNT = 3
EXPECTED_FORM_5000_PRIOR_MEAN = 0.7
EXPECTED_FORM_5000_WIN_RATE = 52.0


def _prereqs_available():
    return (
        os.path.exists(FORM_PRICE_CACHE_PATH)
        and os.path.exists(SPY_PRICE_CACHE_PATH)
        and os.path.exists(BOUNDS_HISTORY_PATH)
    )


class _FakeRedisBackend:
    def __init__(self, bounds_history):
        self.db = {"priormean:bounds_history": bounds_history}

    def get_json(self, key, default=None):
        return self.db.get(key, default)

    def set_json(self, key, value, ex=None):
        self.db[key] = value
        return True


class LoadPriceSeriesFallbackTests(unittest.TestCase):
    """load_price_series()のキャッシュ有無による分岐(API呼び出し回数)の
    テスト。既存56銘柄・既存日次refresh(outputsize=300)には一切触れない。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="priormean_fallback_test_")
        self.addCleanup(shutil.rmtree, self._tmp_dir, ignore_errors=True)

    def _write_cache(self, ticker, dates, closes, volumes=None):
        path = os.path.join(self._tmp_dir, f"{ticker}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"dates": dates, "closes": closes, "volumes": volumes or [1] * len(dates)}, f)

    def test_cache_exists_does_not_call_twelvedata(self):
        """1: ローカルキャッシュがあればAPIを一切呼ばない(既存の挙動、無変更)。"""
        self._write_cache("NEWCO", ["2024-01-01", "2024-01-02"], [10.0, 11.0], [100, 200])
        with mock.patch.object(td, "fetch_daily_series") as m:
            dates, closes, volumes = backfill_script.load_price_series("NEWCO", self._tmp_dir)
        m.assert_not_called()
        self.assertEqual(dates, ["2024-01-01", "2024-01-02"])
        self.assertEqual(closes, [10.0, 11.0])
        self.assertEqual(volumes, [100, 200])

    def test_cache_missing_calls_twelvedata_with_outputsize_5000(self):
        """2: ローカルキャッシュが無い場合のみ、outputsize=5000でTwelve Data
        を1回だけ呼ぶ(新規銘柄の初回バックフィル専用)。"""
        fake_result = (["2024-01-01", "2024-01-02"], [10.0, 11.0], [100, 200], [9.5, 10.5], 1)
        with mock.patch.object(td, "fetch_daily_series", return_value=fake_result) as m, \
             mock.patch.dict(os.environ, {"TWELVEDATA_API_KEY": "test-key"}):
            dates, closes, volumes = backfill_script.load_price_series("NEWCO", self._tmp_dir)
        m.assert_called_once_with("NEWCO", "test-key", outputsize=5000)
        # 3: API成功時は、そのまま今回のバックフィル処理に使える形(dates/closes/volumes)で返る。
        self.assertEqual(dates, ["2024-01-01", "2024-01-02"])
        self.assertEqual(closes, [10.0, 11.0])
        self.assertEqual(volumes, [100, 200])

    def test_api_failure_propagates_without_fabricating_data(self):
        """4: Twelve Data取得失敗時は、架空データで補完せず例外を伝播させる
        (呼び出し元run_pending_priormean_backfill.py、既存の例外ハンドラが
        status=failedとして安全に処理する、無変更)。"""
        with mock.patch.object(td, "fetch_daily_series", side_effect=td.TdApiError("NO_DATA", "テスト用失敗")), \
             mock.patch.dict(os.environ, {"TWELVEDATA_API_KEY": "test-key"}):
            with self.assertRaises(td.TdApiError):
                backfill_script.load_price_series("NEWCO", self._tmp_dir)

    def test_missing_api_key_raises_without_calling_api(self):
        """APIキー未設定の場合は、Twelve Dataを呼ばずに安全に失敗する
        (架空データで補完しない)。"""
        with mock.patch.object(td, "fetch_daily_series") as m, \
             mock.patch.dict(os.environ, {"TWELVEDATA_API_KEY": ""}):
            with self.assertRaises(RuntimeError):
                backfill_script.load_price_series("NEWCO", self._tmp_dir)
        m.assert_not_called()

    def test_existing_56_ticker_cache_path_unaffected(self):
        """5: 既存56銘柄(常にローカルキャッシュが存在する)は、APIキーが
        無くてもこのフォールバック分岐に到達せず、正常に動作する。"""
        self._write_cache("AMD", ["2024-01-01"], [100.0], [1])
        with mock.patch.object(td, "fetch_daily_series") as m, \
             mock.patch.dict(os.environ, {"TWELVEDATA_API_KEY": ""}):
            dates, closes, volumes = backfill_script.load_price_series("AMD", self._tmp_dir)
        m.assert_not_called()
        self.assertEqual(closes, [100.0])


@unittest.skipUnless(_prereqs_available(), "FORM/SPY価格キャッシュ・Phase 0のbounds_historyが無いためスキップ")
class NewTickerFallbackFormVerificationTests(unittest.TestCase):
    """9: 「FORMのローカルキャッシュが無く、outputsize=5000のTwelve Data
    フォールバックで初回バックフィルした場合」を実際にbackfill_one_ticker()
    経由(モックなしの実計算、score計算含む)で再現し、2026-10-27の調査
    フェーズで確認した研究結果(day0=334・確定331・prior_mean+0.7%・
    win_rate52.0%)と一致することを確認する。完全履歴版(343/340/3、
    +0.6%)と意図的に異なる値のまま検証する(「完全一致させる」ための
    補正ロジックは追加していないことの確認が目的)。"""

    @classmethod
    def setUpClass(cls):
        with open(FORM_PRICE_CACHE_PATH, encoding="utf-8") as f:
            form_price = json.load(f)
        # outputsize=5000フォールバックを模す: 実際にTwelve Dataへ取得させる
        # 代わりに、FORMの実価格データの直近5000営業日分をfetch_daily_series
        # の戻り値として差し替える(twelvedata_client.fetch_daily_series自体
        # は無変更、呼び出し結果だけを本物のFORM価格データで模擬する)。
        cls.window_dates = form_price["dates"][-5000:]
        cls.window_closes = form_price["closes"][-5000:]
        cls.window_volumes = form_price["volumes"][-5000:]

        cls._tmp_dir = tempfile.mkdtemp(prefix="priormean_fallback_form_test_")
        # SPYは既存ローカルキャッシュをそのまま使う(=API呼び出し0回になる
        # ことを以下のテストで確認する)。FORM.jsonは置かない(フォールバック
        # を発生させるため)。
        shutil.copy(SPY_PRICE_CACHE_PATH, os.path.join(cls._tmp_dir, "SPY.json"))

        with open(BOUNDS_HISTORY_PATH, encoding="utf-8") as f:
            cls.bounds_history = json.load(f)
        cls.static_data = pml.load_reference_data()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def test_form_outputsize_5000_fallback_reproduces_research_result(self):
        fake = _FakeRedisBackend(self.bounds_history)
        fake_td_result = (self.window_dates, self.window_closes, self.window_volumes, None, 1)

        t0 = time.time()
        # design 2026-10-28d: backfill_one_ticker冒頭に追加されたRedis原子的
        # ロック(set_nx)を、このFakeRedisBackend(get_json/set_jsonのみ)の
        # テストでは常に成功させる(ロック機構自体は別のテストで検証する)。
        with mock.patch.object(store, "get_json", side_effect=fake.get_json), \
             mock.patch.object(store, "set_json", side_effect=fake.set_json), \
             mock.patch.object(store, "set_nx", return_value=True), \
             mock.patch.object(td, "fetch_daily_series", return_value=fake_td_result) as m_td, \
             mock.patch.dict(os.environ, {"TWELVEDATA_API_KEY": "test-key"}):
            outcome = backfill_script.backfill_one_ticker("FORM", price_cache_dir=self._tmp_dir)
        elapsed = time.time() - t0

        # 8: API呼び出し回数(TICKER=FORM 1回、SPYはローカルキャッシュ使用のため0回)。
        m_td.assert_called_once_with("FORM", "test-key", outputsize=5000)

        self.assertFalse(outcome["skipped"])
        result = outcome["result"]
        self.assertEqual(result["day0_count"], EXPECTED_FORM_5000_DAY0_COUNT)
        self.assertEqual(len(result["resolved_events"]), EXPECTED_FORM_5000_RESOLVED_COUNT)
        self.assertEqual(len(result["unresolved_day0_dates"]), EXPECTED_FORM_5000_UNRESOLVED_COUNT)

        merged = inc.merge_reference_data(self.static_data, {"FORM": result["resolved_events"]})
        anchor = "2099-01-01"
        pm = pml.get_prior_mean("FORM", anchor, data=merged)
        ps = pml.get_prior_stats("FORM", anchor, data=merged)
        self.assertEqual(pm["status"], "ok")
        self.assertEqual(pm["mean"], EXPECTED_FORM_5000_PRIOR_MEAN)
        self.assertEqual(ps["win_rate"], EXPECTED_FORM_5000_WIN_RATE)

        print(
            f"\n[PERF] FORM outputsize=5000フォールバック(実計算、モックなし): "
            f"取得データ{len(self.window_dates)}日分 / 所要時間={elapsed:.1f}秒 / "
            f"day0={result['day0_count']} 確定={len(result['resolved_events'])} "
            f"prior_mean={pm['mean']}% win_rate={ps['win_rate']}%"
        )


if __name__ == "__main__":
    unittest.main()
