# -*- coding: utf-8 -*-
"""Web Push通知(design 2026-10-22、Renderデプロイ検知→Windowsデスクトップ通知)の
テスト。push_notify.pyの純粋ロジックと、app.pyの/api/push/*エンドポイントを
対象とする。Upstash/Twelve Data/Renderへは一切アクセスしない(FakeStoreで
置き換え、webpush送信はmockで差し替える)。

Q1〜Q5・Q5シグナル・BUY/SELL・Risk-off・実績比較・株価API取得・GitHub Actions
には一切関係しないことを確認する目的のテストも含む(既存テスト全体が無改修で
PASSすることで裏付ける、test_push_notify.py自身はpush_notify.py/app.pyの
push関連コードのみを対象にする)。
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import push_notify


class FakeStore:
    """redis_store.pyのpush関連関数と同じシグネチャを持つ、インメモリの偽物。
    NX(try_acquire_push_dispatch_lock)も「最初に呼んだ側だけ成功する」という
    実際のUpstash SET NXと同じ性質を再現する。"""

    def __init__(self):
        self.dispatched_commit = None
        self.notified_commit = None
        self.subscriptions = []
        self._locks = set()
        self.refresh_notified_date = None
        self._refresh_locks = set()

    def get_push_dispatched_commit(self):
        return self.dispatched_commit

    def set_push_dispatched_commit(self, commit):
        self.dispatched_commit = commit
        return True

    def try_acquire_push_dispatch_lock(self, commit, ttl_seconds=120):
        if commit in self._locks:
            return False
        self._locks.add(commit)
        return True

    def get_push_notified_commit(self):
        return self.notified_commit

    def set_push_notified_commit(self, commit):
        self.notified_commit = commit
        return True

    def get_push_subscriptions(self):
        return list(self.subscriptions)

    def set_push_subscriptions(self, subs):
        self.subscriptions = list(subs)
        return True

    def get_push_refresh_notified_date(self):
        return self.refresh_notified_date

    def set_push_refresh_notified_date(self, trading_date):
        self.refresh_notified_date = trading_date
        return True

    def try_acquire_push_refresh_notify_lock(self, trading_date, ttl_seconds=120):
        if trading_date in self._refresh_locks:
            return False
        self._refresh_locks.add(trading_date)
        return True


SUB_A = {"endpoint": "https://fcm.googleapis.com/send/AAA", "keys": {"p256dh": "p" * 20, "auth": "a" * 10}}
SUB_B = {"endpoint": "https://fcm.googleapis.com/send/BBB", "keys": {"p256dh": "q" * 20, "auth": "b" * 10}}


def _configured_env():
    return {"VAPID_PUBLIC_KEY": "pub-key", "VAPID_PRIVATE_KEY": "priv-key", "RENDER_GIT_COMMIT": "a" * 40}


class DeployDetectionTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_first_boot_seeds_without_sending(self):
        """初回導入commit(dispatched_commitが未設定)では通知しない。"""
        with mock.patch.dict(os.environ, _configured_env()), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_not_called()
        self.assertEqual(self.store.dispatched_commit, "a" * 40)
        self.assertEqual(self.store.notified_commit, "a" * 40)

    def test_new_commit_after_seed_sends_push(self):
        """既に導入済み(dispatched_commitが別commit)で新commitがLiveになったら送信する。"""
        self.store.dispatched_commit = "old" + "0" * 37
        self.store.notified_commit = "old" + "0" * 37
        self.store.subscriptions = [SUB_A]
        env = _configured_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_called_once()
        self.assertEqual(self.store.dispatched_commit, env["RENDER_GIT_COMMIT"])
        # 送信を試みただけではnotified_commitは更新しない(表示確認待ち)。
        self.assertEqual(self.store.notified_commit, "old" + "0" * 37)

    def test_same_commit_not_notified_twice(self):
        """同じcommitについて2回dispatchを呼んでも、2回目は送信しない。"""
        self.store.dispatched_commit = "old" + "0" * 37
        self.store.subscriptions = [SUB_A]
        env = _configured_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_called_once()

    def test_concurrent_workers_send_only_once(self):
        """複数worker相当(同じcommitに対して2回dispatch処理が走る)でも、
        1回目の書き込み(dispatched_commit更新)により2回目は送信しない。"""
        self.store.dispatched_commit = "old" + "0" * 37
        self.store.subscriptions = [SUB_A]
        env = _configured_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_called_once()

    def test_concurrent_workers_both_see_stale_value_lock_still_prevents_double_send(self):
        """2ワーカーが"ほぼ同時に"起動し、どちらも更新前のdispatched_commit
        (stale値)を読んでしまった場合でも、NXロック(try_acquire_push_dispatch_lock)
        が2回目の送信を防ぐことを確認する。dispatched_commitの比較だけに頼った
        実装では防げない、本当のレースを再現するテスト。"""
        stale_value = "old" + "0" * 37

        class StaleReadStore(FakeStore):
            def get_push_dispatched_commit(self):
                return stale_value  # 常にstale値を返す(更新を観測できない worker を模す)

        store = StaleReadStore()
        store.subscriptions = [SUB_A]
        env = _configured_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(store)  # worker 1
            push_notify.check_and_dispatch_deploy_notification(store)  # worker 2(stale読み)
        m.assert_called_once()

    def test_manual_restart_same_commit_does_not_resend(self):
        """手動再起動(commitは変わらない)だけでは再通知しない。"""
        env = _configured_env()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)  # 初回シード
            push_notify.check_and_dispatch_deploy_notification(self.store)  # 再起動(同commit)
        m.assert_not_called()

    def test_missing_render_git_commit_does_nothing(self):
        """RENDER_GIT_COMMITが取得できない(ローカル環境等)場合は何もしない。"""
        env = {"VAPID_PUBLIC_KEY": "pub-key", "VAPID_PRIVATE_KEY": "priv-key"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(push_notify, "webpush") as m:
            os.environ.pop("RENDER_GIT_COMMIT", None)
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_not_called()
        self.assertIsNone(self.store.dispatched_commit)

    def test_not_configured_does_nothing(self):
        """VAPID鍵が未設定(=機能オフ)の場合は何もしない。"""
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "a" * 40}, clear=False), \
             mock.patch.object(push_notify, "webpush") as m:
            os.environ.pop("VAPID_PUBLIC_KEY", None)
            os.environ.pop("VAPID_PRIVATE_KEY", None)
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_not_called()
        self.assertIsNone(self.store.dispatched_commit)


def _refresh_env():
    """GitHub Actions側を想定した環境(design 2026-10-22)。VAPID_PUBLIC_KEYは
    意図的に含めない(送信にはVAPID_PRIVATE_KEYだけで足りることの確認を兼ねる、
    GitHub Actions SecretsにVAPID_PUBLIC_KEYを置かずに済ませる設計のため)。"""
    return {"VAPID_PRIVATE_KEY": "priv-key"}


class RefreshSuccessNotificationTests(unittest.TestCase):
    """日次株価更新 完了通知(push_notify.notify_refresh_success)。
    GitHub Actions(refresh.py)専用で、デプロイ検知(RENDER_GIT_COMMIT)とは
    完全に独立していることを確認する。"""

    def setUp(self):
        self.store = FakeStore()
        self.store.subscriptions = [SUB_A]

    def test_new_trading_date_sends_notification(self):
        """新しい取引日が特定できた回は通知する。VAPID_PUBLIC_KEY無しでも
        送信できる(GitHub Actionsの想定環境)ことを合わせて確認する。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, "2026-10-22")
        m.assert_called_once()
        sent_payload = json.loads(m.call_args.kwargs["data"])
        self.assertEqual(sent_payload["title"], "📈 Stock Buy App")
        self.assertEqual(sent_payload["body"], "株価データを更新しました")
        self.assertEqual(self.store.refresh_notified_date, "2026-10-22")

    def test_same_trading_date_rerun_does_not_notify_twice(self):
        """同じ取引日の2回目・3回目の実行(cronのリトライ枠)では通知しない。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, "2026-10-22")  # 1回目(07:00想定)
            push_notify.notify_refresh_success(self.store, "2026-10-22")  # 2回目(10:00想定)
            push_notify.notify_refresh_success(self.store, "2026-10-22")  # 3回目(13:00想定)
        m.assert_called_once()

    def test_missing_trading_date_does_not_notify(self):
        """trading_date=None(SPY取得失敗などで取引日が特定できなかった=
        全体失敗)の場合は通知しない。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, None)
        m.assert_not_called()
        self.assertIsNone(self.store.refresh_notified_date)

    def test_send_failure_allows_retry_on_next_run(self):
        """1回目のPush送信処理自体が予期しない例外で失敗した場合、
        notified_dateが更新されないため、後続実行(同じtrading_date)で
        再送を試みられる。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True):
            with mock.patch.object(push_notify, "_dispatch_push_to_all", side_effect=RuntimeError("down")):
                try:
                    push_notify.notify_refresh_success(self.store, "2026-10-22")  # 1回目: 失敗
                except Exception as e:  # pragma: no cover
                    self.fail(f"例外が外へ伝播した: {e}")
            self.assertIsNone(self.store.refresh_notified_date)

            # 実際の運用では1回目と2回目の実行(cronのリトライ枠)の間は数時間空く
            # ため、try_acquire_push_refresh_notify_lockの短命ロック(ttl_seconds)
            # は2回目の実行時には自然に失効している。FakeStoreはTTLを再現しない
            # ため、ここでは「ロックが失効した状態」を明示的に再現する。
            self.store._refresh_locks.clear()

            with mock.patch.object(push_notify, "webpush") as m:
                push_notify.notify_refresh_success(self.store, "2026-10-22")  # 2回目: 再送成功
        m.assert_called_once()
        self.assertEqual(self.store.refresh_notified_date, "2026-10-22")

    def test_partial_ticker_failure_still_notifies(self):
        """一部銘柄の取得失敗があっても、取引日更新自体が成功していれば
        (呼び出し元がtrading_dateを渡す限り)通知する(既存のrefresh.py方針を
        変更しない設計のため、この関数自体は成功/部分失敗を区別しない)。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, "2026-10-22")
        m.assert_called_once()

    def test_not_configured_does_nothing(self):
        """VAPID_PRIVATE_KEY未設定(=機能オフ)の場合は何もしない。"""
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, "2026-10-22")
        m.assert_not_called()
        self.assertIsNone(self.store.refresh_notified_date)

    def test_no_subscribers_still_marks_notified(self):
        """購読者がいない場合でも送信処理自体は正常完了したとみなし、
        notified_dateは更新する(_dispatch_push_to_allの既存挙動をそのまま
        踏襲、デプロイ通知側と同じ考え方)。"""
        self.store.subscriptions = []
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(self.store, "2026-10-22")
        m.assert_not_called()
        self.assertEqual(self.store.refresh_notified_date, "2026-10-22")

    def test_concurrent_processes_only_one_sends(self):
        """同じtrading_dateに対する同時実行は、Redisの原子的なNXロック
        (try_acquire_push_refresh_notify_lock)により1プロセストだけが送信する。"""
        store = self.store
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush") as m:
            push_notify.notify_refresh_success(store, "2026-10-22")  # worker 1
            push_notify.notify_refresh_success(store, "2026-10-22")  # worker 2(同時実行を想定)
        m.assert_called_once()

    def test_deploy_notification_unaffected_by_refresh_notification(self):
        """株価更新通知を送っても、デプロイ通知(dispatched_commit/
        notified_commit)の状態には一切影響しない(完全に独立したキーであることの確認)。"""
        with mock.patch.dict(os.environ, _refresh_env(), clear=True), \
             mock.patch.object(push_notify, "webpush"):
            push_notify.notify_refresh_success(self.store, "2026-10-22")
        self.assertIsNone(self.store.dispatched_commit)
        self.assertIsNone(self.store.notified_commit)


class SendFailureResilienceTests(unittest.TestCase):
    """push送信が失敗してもアプリ(=呼び出し元の処理)が落ちないことの確認。"""

    def setUp(self):
        self.store = FakeStore()
        self.store.dispatched_commit = "old" + "0" * 37
        self.store.subscriptions = [SUB_A, SUB_B]

    def test_generic_exception_during_send_does_not_raise(self):
        with mock.patch.dict(os.environ, _configured_env()), \
             mock.patch.object(push_notify, "webpush", side_effect=RuntimeError("network down")):
            try:
                push_notify.check_and_dispatch_deploy_notification(self.store)
            except Exception as e:  # pragma: no cover
                self.fail(f"例外が外へ伝播した: {e}")
        # 一時的エラーとして購読は保持される。
        self.assertEqual(len(self.store.subscriptions), 2)

    def test_safe_deploy_check_never_raises_even_if_store_broken(self):
        broken_store = mock.Mock()
        broken_store.get_push_dispatched_commit.side_effect = RuntimeError("redis down")
        with mock.patch.dict(os.environ, _configured_env()):
            try:
                push_notify._safe_deploy_check(broken_store)
            except Exception as e:  # pragma: no cover
                self.fail(f"_safe_deploy_checkが例外を外へ伝播させた: {e}")

    def test_invalid_subscription_removed_on_410(self):
        from pywebpush import WebPushException

        resp = mock.Mock()
        resp.status_code = 410

        def fake_webpush(**kwargs):
            if kwargs["subscription_info"]["endpoint"] == SUB_A["endpoint"]:
                raise WebPushException("gone", response=resp)
            return "ok"

        with mock.patch.dict(os.environ, _configured_env()), \
             mock.patch.object(push_notify, "webpush", side_effect=fake_webpush):
            push_notify.check_and_dispatch_deploy_notification(self.store)

        remaining = [s["endpoint"] for s in self.store.subscriptions]
        self.assertNotIn(SUB_A["endpoint"], remaining)
        self.assertIn(SUB_B["endpoint"], remaining)

    def test_invalid_subscription_removed_on_404(self):
        from pywebpush import WebPushException

        resp = mock.Mock()
        resp.status_code = 404

        with mock.patch.dict(os.environ, _configured_env()), \
             mock.patch.object(push_notify, "webpush", side_effect=WebPushException("notfound", response=resp)):
            push_notify.check_and_dispatch_deploy_notification(self.store)

        self.assertEqual(self.store.subscriptions, [])

    def test_transient_error_keeps_subscription(self):
        """410/404以外(例: 500)は一時的エラーとみなし、購読を削除しない。"""
        from pywebpush import WebPushException

        resp = mock.Mock()
        resp.status_code = 500

        with mock.patch.dict(os.environ, _configured_env()), \
             mock.patch.object(push_notify, "webpush", side_effect=WebPushException("server error", response=resp)):
            push_notify.check_and_dispatch_deploy_notification(self.store)

        self.assertEqual(len(self.store.subscriptions), 2)


class SubscriptionManagementTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_add_subscription_success(self):
        ok, err = push_notify.add_subscription(self.store, SUB_A)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(self.store.subscriptions), 1)
        self.assertEqual(self.store.subscriptions[0]["endpoint"], SUB_A["endpoint"])

    def test_add_subscription_stores_minimal_fields_only(self):
        noisy = dict(SUB_A)
        noisy["expirationTime"] = None
        noisy["extra_field"] = "should not be stored"
        push_notify.add_subscription(self.store, noisy)
        stored = self.store.subscriptions[0]
        self.assertEqual(set(stored.keys()), {"endpoint", "keys"})
        self.assertEqual(set(stored["keys"].keys()), {"p256dh", "auth"})

    def test_add_subscription_dedup_by_endpoint(self):
        push_notify.add_subscription(self.store, SUB_A)
        push_notify.add_subscription(self.store, SUB_A)
        self.assertEqual(len(self.store.subscriptions), 1)

    def test_add_subscription_rejects_invalid_payload(self):
        for bad in [None, {}, {"endpoint": "not-https"}, {"endpoint": "https://x"},
                    {"endpoint": "https://x", "keys": {"p256dh": "p"}},
                    {"endpoint": "http://insecure", "keys": {"p256dh": "p", "auth": "a"}}]:
            ok, err = push_notify.add_subscription(self.store, bad)
            self.assertFalse(ok, msg=f"should reject: {bad}")
            self.assertIsNotNone(err)
        self.assertEqual(self.store.subscriptions, [])

    def test_max_subscriptions_cap(self):
        for i in range(15):
            sub = {"endpoint": f"https://fcm.googleapis.com/send/{i}",
                   "keys": {"p256dh": "p" * 20, "auth": "a" * 10}}
            push_notify.add_subscription(self.store, sub)
        self.assertLessEqual(len(self.store.subscriptions), push_notify._MAX_SUBSCRIPTIONS)

    def test_remove_subscription(self):
        push_notify.add_subscription(self.store, SUB_A)
        push_notify.add_subscription(self.store, SUB_B)
        removed = push_notify.remove_subscription(self.store, SUB_A["endpoint"])
        self.assertTrue(removed)
        remaining = [s["endpoint"] for s in self.store.subscriptions]
        self.assertEqual(remaining, [SUB_B["endpoint"]])

    def test_remove_nonexistent_subscription_returns_false(self):
        removed = push_notify.remove_subscription(self.store, "https://does-not-exist")
        self.assertFalse(removed)


class ForegroundFallbackTests(unittest.TestCase):
    """アプリ起動時にWeb Pushの未到達を検知するフォールバック経路。"""

    def setUp(self):
        self.store = FakeStore()

    def test_pending_when_notified_behind_current(self):
        self.store.notified_commit = "old" + "0" * 37
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "new" + "0" * 37}):
            status = push_notify.get_push_status(self.store)
        self.assertTrue(status["pending_notification"])
        self.assertEqual(status["current_commit"], "new" + "0" * 37)

    def test_not_pending_when_already_notified(self):
        """Web Pushが既に届いている(notified_commitが最新)場合は二重通知しない。"""
        self.store.notified_commit = "cur0" + "0" * 36
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "cur0" + "0" * 36}):
            status = push_notify.get_push_status(self.store)
        self.assertFalse(status["pending_notification"])

    def test_not_pending_before_first_seed(self):
        """notified_commitが一度も設定されていない(導入前)場合はpending扱いしない。"""
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "a" * 40}):
            status = push_notify.get_push_status(self.store)
        self.assertFalse(status["pending_notification"])

    def test_ack_updates_notified_commit_when_matching_current(self):
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "a" * 40}):
            ok = push_notify.ack_notified(self.store, "a" * 40)
        self.assertTrue(ok)
        self.assertEqual(self.store.notified_commit, "a" * 40)

    def test_ack_rejects_mismatched_commit(self):
        """クライアントが任意の値を送っても、現在のRENDER_GIT_COMMITと
        一致しない限りnotified_commitは更新されない。"""
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "a" * 40}):
            ok = push_notify.ack_notified(self.store, "forged" + "0" * 34)
        self.assertFalse(ok)
        self.assertIsNone(self.store.notified_commit)

    def test_fallback_then_dispatch_does_not_double_send(self):
        """フォールバックでnotified_commitが更新された後、起動時dispatchが
        走っても(dispatched_commitとは独立のため)送信判定自体は変わらないが、
        既にdispatched済みなら再送信しない組み合わせを確認する。"""
        self.store.dispatched_commit = "cur0" + "0" * 36
        self.store.notified_commit = "old" + "0" * 37
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "cur0" + "0" * 36}):
            ok = push_notify.ack_notified(self.store, "cur0" + "0" * 36)
            self.assertTrue(ok)
            status = push_notify.get_push_status(self.store)
        self.assertFalse(status["pending_notification"])

        env = _configured_env()
        env["RENDER_GIT_COMMIT"] = "cur0" + "0" * 36
        with mock.patch.dict(os.environ, env), mock.patch.object(push_notify, "webpush") as m:
            push_notify.check_and_dispatch_deploy_notification(self.store)
        m.assert_not_called()


class PushApiEndpointTests(unittest.TestCase):
    """/api/push/* の実HTTPエンドポイント(Flask test client)。"""

    def setUp(self):
        import app
        self.app = app
        self.client = app.app.test_client()
        self.fake = FakeStore()
        patches = [
            mock.patch.object(app.store, "get_push_dispatched_commit", side_effect=self.fake.get_push_dispatched_commit),
            mock.patch.object(app.store, "set_push_dispatched_commit", side_effect=self.fake.set_push_dispatched_commit),
            mock.patch.object(app.store, "try_acquire_push_dispatch_lock", side_effect=self.fake.try_acquire_push_dispatch_lock),
            mock.patch.object(app.store, "get_push_notified_commit", side_effect=self.fake.get_push_notified_commit),
            mock.patch.object(app.store, "set_push_notified_commit", side_effect=self.fake.set_push_notified_commit),
            mock.patch.object(app.store, "get_push_subscriptions", side_effect=self.fake.get_push_subscriptions),
            mock.patch.object(app.store, "set_push_subscriptions", side_effect=self.fake.set_push_subscriptions),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def test_public_key_endpoint(self):
        with mock.patch.dict(os.environ, _configured_env()):
            r = self.client.get("/api/push/public-key")
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertTrue(j["enabled"])
        self.assertEqual(j["key"], "pub-key")

    def test_public_key_endpoint_disabled_without_vapid_keys(self):
        env = dict(os.environ)
        env.pop("VAPID_PUBLIC_KEY", None)
        env.pop("VAPID_PRIVATE_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.client.get("/api/push/public-key")
        j = r.get_json()
        self.assertFalse(j["enabled"])

    def test_subscribe_endpoint_success(self):
        r = self._post("/api/push/subscribe", {"subscription": SUB_A})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(self.fake.subscriptions), 1)

    def test_subscribe_endpoint_rejects_invalid(self):
        r = self._post("/api/push/subscribe", {"subscription": {"endpoint": "not-https"}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_unsubscribe_endpoint(self):
        self._post("/api/push/subscribe", {"subscription": SUB_A})
        r = self._post("/api/push/unsubscribe", {"endpoint": SUB_A["endpoint"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.fake.subscriptions, [])

    def test_status_endpoint(self):
        self.fake.notified_commit = "old" + "0" * 37
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "new" + "0" * 37}):
            r = self.client.get("/api/push/status")
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertTrue(j["pending_notification"])

    def test_ack_endpoint(self):
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "a" * 40}):
            r = self._post("/api/push/ack", {"commit": "a" * 40})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.fake.notified_commit, "a" * 40)

    def test_subscribe_endpoint_never_raises_500_on_garbage_body(self):
        r = self.client.post("/api/push/subscribe", data="not json", content_type="application/json")
        self.assertIn(r.status_code, (200, 400))


class JsSyntaxTests(unittest.TestCase):
    """Service Worker(SW_JS)・HTML内インラインJSのNode構文チェック。"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def _check_js(self, code):
        if not self.node:
            self.skipTest("node実行環境が見つからないため構文チェックをスキップします")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
            path = f.name
        try:
            result = subprocess.run([self.node, "--check", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
        finally:
            os.unlink(path)

    def test_sw_js_syntax(self):
        import app
        self._check_js(app.SW_JS)

    def test_html_inline_script_syntax(self):
        import app
        import re
        scripts = re.findall(r"<script>(.*?)</script>", app.HTML, re.S)
        self.assertTrue(scripts, "インラインscriptが見つからない")
        for script in scripts:
            self._check_js(script)

    def test_push_related_js_symbols_present(self):
        """今回追加したJS関数がHTML/SW_JSに実際に含まれていることの確認
        (構文チェックだけでは「意図したコードが入っているか」はわからないため)。"""
        import app
        for symbol in ["enablePushNotify", "initPushNotifyUI", "checkPushFallback", "/api/push/subscribe", "/api/push/ack"]:
            self.assertIn(symbol, app.HTML)
        for symbol in ["addEventListener(\"push\"", "addEventListener(\"notificationclick\"", "/api/push/ack"]:
            self.assertIn(symbol, app.SW_JS)


if __name__ == "__main__":
    unittest.main()
