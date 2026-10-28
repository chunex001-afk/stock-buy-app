# -*- coding: utf-8 -*-
"""redis_store.set_nx()のバグ修正(2026-10-28、design調査で発見)の
再発防止テスト。

発見した問題: Upstash REST APIの仕様(docs.upstash.com)では、NXは値を
持たない裸のクエリフラグ(例: "set/foo?NX&EX=100")だが、修正前の実装は
誤って"?NX=true"を送っており、Upstashが400 Bad Requestを返し続けていた
(2026-10-22〜、本番のAAPL実データテストで実測確認済み)。

このテストは、実際のHTTP通信(_request)をmockし、set_nx()/3つの
ラッパー関数が生成する実際のpath文字列を直接assertする(ロジックだけを
モックで固めてしまい、実際のURL形式の誤りに気づけなかった、という今回の
バグの再発を防ぐことが目的)。Upstash・Twelve Dataへは一切アクセスしない。
"""
import re
import unittest
from unittest import mock

import redis_store as store


class SetNxUrlFormatTests(unittest.TestCase):
    """A/B/C: set_nx()が生成する実際のpath文字列の検証。"""

    def test_a_new_key_with_ex_generates_bare_nx_and_ex(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            result = store.set_nx("foo", "1", ex=600)
        self.assertTrue(result)
        m.assert_called_once()
        method, path = m.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "set/foo?NX&EX=600")
        self.assertNotIn("NX=true", path, "NXは値を持たない裸のフラグであるべき(Upstash公式仕様)")

    def test_b_new_key_without_ex_generates_bare_nx_only(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            result = store.set_nx("foo", "1")
        self.assertTrue(result)
        _, path = m.call_args[0]
        self.assertEqual(path, "set/foo?NX")

    def test_c_key_with_colons_is_preserved_correctly(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            store.set_nx("priormean:backfill_lock:AAPL", "1", ex=600)
        _, path = m.call_args[0]
        self.assertEqual(path, "set/priormean:backfill_lock:AAPL?NX&EX=600")

    def test_existing_key_nx_fails_returns_false(self):
        """Upstashは既存keyへのNX失敗時にresult=nullを返す(ドキュメント記載の
        挙動)。この場合はFalseを返すべき(例外ではない)。"""
        with mock.patch.object(store, "_request", return_value={"result": None}):
            result = store.set_nx("foo", "1", ex=600)
        self.assertFalse(result)

    def test_network_failure_returns_false_not_exception(self):
        with mock.patch.object(store, "_request", return_value=None):
            result = store.set_nx("foo", "1", ex=600)
        self.assertFalse(result)


class WrapperFunctionUrlFormatTests(unittest.TestCase):
    """D: 既存の3つのwrapper関数(try_acquire_*)が、修正後のset_nx()経由で
    正しいpathを生成することの確認(実通信部分=_requestをmockし、実際に
    渡されるpath/query stringを直接assertする)。"""

    def test_priormean_backfill_lock_path(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            result = store.try_acquire_priormean_backfill_lock("AAPL")
        self.assertTrue(result)
        _, path = m.call_args[0]
        self.assertEqual(path, "set/priormean:backfill_lock:AAPL?NX&EX=600")

    def test_push_dispatch_lock_path(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            result = store.try_acquire_push_dispatch_lock("abc1234")
        self.assertTrue(result)
        _, path = m.call_args[0]
        self.assertEqual(path, "set/push:dispatch_lock:abc1234?NX&EX=120")

    def test_push_refresh_notify_lock_path(self):
        with mock.patch.object(store, "_request", return_value={"result": "OK"}) as m:
            result = store.try_acquire_push_refresh_notify_lock("2026-10-28")
        self.assertTrue(result)
        _, path = m.call_args[0]
        self.assertEqual(path, "set/push:refresh_notify_lock:2026-10-28?NX&EX=120")


class _FakeUpstashSetNx:
    """Upstashの実際のSET NX+EX挙動を近似するフェイク(_requestレベルで
    差し替える)。pathをパースしてkey/NX/EXの有無を解釈し、インメモリ辞書で
    「キーが既に存在する場合はNXが失敗する」という本物の挙動を再現する。
    TTLの実際の失効は再現しない(テストでは明示的に削除して失効を模す)。"""

    _PATH_RE = re.compile(r"^set/(?P<key>[^?]+)\?(?P<query>.*)$")

    def __init__(self):
        self.store = {}

    def __call__(self, method, path, **kwargs):
        assert method == "POST"
        m = self._PATH_RE.match(path)
        assert m, f"unexpected path: {path}"
        key = m.group("key")
        query = m.group("query")
        has_nx = "NX" in query.split("&")
        if has_nx and key in self.store:
            return {"result": None}  # NXのため既存キーには書き込まない
        self.store[key] = "1"
        return {"result": "OK"}

    def expire_now(self, key):
        """TTL失効を模す(テスト専用、実際のTTLカウントダウンは行わない)。"""
        self.store.pop(key, None)


class LockBehaviorWithRealisticUpstashSimulationTests(unittest.TestCase):
    """④: 新規lock→True、同じkeyの2回目→False、TTL失効後は再取得可能、
    別tickerは相互干渉しない、ことを確認する(修正後のset_nx()を実際に
    通して検証する)。"""

    def setUp(self):
        self.fake = _FakeUpstashSetNx()
        self._patch = mock.patch.object(store, "_request", side_effect=self.fake)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_new_lock_succeeds(self):
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))

    def test_second_call_on_same_key_fails(self):
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.assertFalse(store.try_acquire_priormean_backfill_lock("AAPL"))

    def test_ttl_expiry_allows_reacquisition(self):
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.assertFalse(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.fake.expire_now("priormean:backfill_lock:AAPL")  # TTL失効を模す
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))

    def test_different_tickers_do_not_interfere(self):
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.assertTrue(store.try_acquire_priormean_backfill_lock("MSFT"))
        self.assertFalse(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.assertFalse(store.try_acquire_priormean_backfill_lock("MSFT"))

    def test_different_lock_namespaces_do_not_interfere(self):
        """priormean lockとpush lockは別の名前空間のため干渉しない。"""
        self.assertTrue(store.try_acquire_priormean_backfill_lock("AAPL"))
        self.assertTrue(store.try_acquire_push_dispatch_lock("AAPL"))  # 偶然同じ文字列でも別key


if __name__ == "__main__":
    unittest.main()
