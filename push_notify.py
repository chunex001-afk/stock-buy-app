# -*- coding: utf-8 -*-
"""Web Push通知(design 2026-10-22)。

「GitHubへpushされた」ではなく「Renderで新しいcommitが実際にLiveになった
(=新しいプロセスが起動した)」ことを基準に、Windowsデスクトップへ
「🔔 アプリが最新版に更新されました」の通知を送る機能。

設計上の方針(すべてapp.py本体・Q1〜Q5/Q5シグナル/BUY・SELL/Risk-off/
実績比較・株価API取得・GitHub Actionsには一切影響しない、完全に独立した
モジュールにする):

- 「dispatch(送信を試みた)」と「notified(実際にユーザーに表示できたと
  確認できた)」を別のRedisキーで管理する。
  - push:dispatched_commit は、サーバーが push送信を試みたcommitを記録する。
    multi-worker/手動再起動時に同じcommitへ何度も送信しないための重複防止。
  - push:notified_commit は、Service Workerがpushを実際に受信して
    showNotification()できた時、または後述のフォアグラウンド起動時
    フォールバックが通知を表示した時にだけ更新される。「送信を試みた」
    だけでは更新しない(=Web Pushが実際に端末へ届かなかった場合、
    次にアプリを開いた時にフォールバック通知を出すための判断材料になる)。
- 起動時のdeploy検知処理は例外を一切外へ伝播させず、アプリ起動(Flaskの
  リクエスト処理開始)を絶対にブロックしない。呼び出し元(app.py)は
  バックグラウンドスレッドで起動する。
- Redis(Upstash REST、既存のredis_store.py)以外の新しい永続化先は追加しない。
"""

import json
import logging
import os
import re
import threading

logger = logging.getLogger("push_notify")

try:
    from pywebpush import webpush, WebPushException
    _PYWEBPUSH_AVAILABLE = True
except Exception:  # pragma: no cover - 依存未インストールでもアプリ全体は起動させる
    webpush = None
    WebPushException = Exception
    _PYWEBPUSH_AVAILABLE = False

_SEND_TIMEOUT = 5  # 秒。1件あたりのpush送信がハングしないための上限。
_MAX_SUBSCRIPTIONS = 10  # 個人利用アプリ前提。購読情報が無限に増えないための上限。

_ENDPOINT_RE = re.compile(r"^https://[^\s]+$")


def get_vapid_public_key():
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def _vapid_private_key():
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip()


def _vapid_claims():
    sub = os.environ.get("VAPID_CLAIMS_EMAIL", "").strip()
    return {"sub": f"mailto:{sub}"} if sub else {"sub": "mailto:admin@example.com"}


def is_configured():
    """push通知機能が使える状態か(鍵未設定・依存未インストールなら無効)。
    ブラウザの購読登録フロー(/api/push/public-key)にはVAPID_PUBLIC_KEYが
    必須なため、ここではVAPID_PUBLIC_KEYの有無も見る。送信(webpush呼び出し)
    自体はVAPID_PRIVATE_KEYだけで行えるため、それだけで足りる箇所は
    _can_send_push()を使う(design 2026-10-22、GitHub Actions側に
    VAPID_PUBLIC_KEYを置かずに済ませるため)。"""
    return bool(_PYWEBPUSH_AVAILABLE and get_vapid_public_key() and _vapid_private_key())


def _can_send_push():
    """実際にpush送信(webpush呼び出し)ができる最小条件。is_configured()とは
    異なりVAPID_PUBLIC_KEYの有無は見ない(送信そのものには使わないため)。"""
    return bool(_PYWEBPUSH_AVAILABLE and _vapid_private_key())


def get_current_commit():
    """Render上で現在稼働しているcommit(RENDER_GIT_COMMIT)。
    ローカル開発環境など未設定の場合はNone。"""
    commit = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    return commit or None


def short_commit(commit):
    return (commit or "")[:7]


# ---------------------------------------------------------------------------
# デプロイ検知(起動時に1回だけ呼ぶ)
# ---------------------------------------------------------------------------

def run_deploy_check_async(store):
    """app.py起動時に呼ぶ。バックグラウンドスレッドで実行し、
    Flaskのリクエスト処理開始を一切待たせない。スレッド内の例外は
    すべてこの関数内で握り、呼び出し元には絶対に伝播させない。"""
    t = threading.Thread(target=_safe_deploy_check, args=(store,), daemon=True)
    t.start()
    return t


def _safe_deploy_check(store):
    try:
        check_and_dispatch_deploy_notification(store)
    except Exception:
        logger.exception("push_notify: デプロイ検知処理で予期しない例外が発生しました(通知機能のみ影響、アプリ本体には影響しません)")


def check_and_dispatch_deploy_notification(store):
    if not is_configured():
        return
    commit = get_current_commit()
    if not commit:
        logger.info("push_notify: RENDER_GIT_COMMITが取得できないため、デプロイ検知をスキップします")
        return

    dispatched = store.get_push_dispatched_commit()
    if dispatched is None:
        # この機能が初めて動いた起動(=導入デプロイ自体)。
        # 導入デプロイについては通知しない。両方のマーカーを現在値でシードする。
        store.set_push_dispatched_commit(commit)
        store.set_push_notified_commit(commit)
        logger.info("push_notify: 初回シード(導入デプロイ) commit=%s", short_commit(commit))
        return

    if dispatched == commit:
        # 既にこのcommitへの送信を試みている(同じcommitでの再起動/他workerが処理済み)。
        return

    if not store.try_acquire_push_dispatch_lock(commit):
        # 他のworker/プロセスが同時に処理中。
        return

    # ロックを取れた側が送信を担当する。送信前にdispatchedを更新し、
    # (送信自体が失敗しても)同じcommitへの再送信ループに陥らないようにする。
    store.set_push_dispatched_commit(commit)
    _send_push_to_all(store, commit)


def _send_push_to_all(store, commit):
    payload = {
        "title": "Stock Buy App",
        "body": "アプリが最新版に更新されました。",
        "commit": short_commit(commit),
    }
    _dispatch_push_to_all(store, payload, log_context=f"commit={short_commit(commit)}")


def _dispatch_push_to_all(store, payload, log_context=""):
    """購読者全員へのpush送信の共通処理(取得→送信→無効購読の削除)。
    デプロイ通知(_send_push_to_all)・日次株価更新完了通知
    (notify_refresh_success)の両方から呼ばれる、payload非依存の送信処理のみを
    担当する部分(2026-10-22の日次株価更新通知追加に伴い、元々
    _send_push_to_all内にあった処理をそのまま切り出したもので、送信ロジック・
    404/410時の購読削除・それ以外の失敗時に購読を保持する挙動は一切変更して
    いない)。"""
    subs = store.get_push_subscriptions() or []
    if not subs:
        logger.info("push_notify: 購読者がいないため送信をスキップします %s", log_context)
        return

    data = json.dumps(payload, ensure_ascii=False)

    still_valid = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=data,
                vapid_private_key=_vapid_private_key(),
                vapid_claims=dict(_vapid_claims()),
                timeout=_SEND_TIMEOUT,
            )
            still_valid.append(sub)
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                logger.info("push_notify: 無効なsubscriptionを削除します status=%s", status)
                continue
            logger.warning("push_notify: push送信に失敗しました(一時的エラーとして購読は保持) status=%s", status)
            still_valid.append(sub)
        except Exception:
            logger.exception("push_notify: push送信中に予期しない例外が発生しました(購読は保持)")
            still_valid.append(sub)

    if still_valid != subs:
        store.set_push_subscriptions(still_valid)


# ---------------------------------------------------------------------------
# 日次株価更新 完了通知(design 2026-10-22、refresh.py専用)。
#
# 「GitHub Actionsがpushされた」でも「Renderにデプロイされた」でもなく、
# 「daily-refresh.ymlが実際に新しい取引日の株価データをRedisへ反映できた」
# ことを基準に送る、上のデプロイ検知とは完全に独立した通知。
#
# 呼び出し側(refresh.py)は、SPY取得も含め取引日そのものが特定できなかった
# 回(APIキー未設定・SPY取得失敗等)はtrading_date=Noneでこの関数を呼ばない
# 設計にする(Q1〜Q5判定ロジック・既存のdaily-refresh処理・一部銘柄のみの
# 取得失敗を許容する既存方針には一切影響しない、読み取り専用の追加呼び出し)。
#
# 同じtrading_dateへの重複送信は、Redisの原子的なSET NXロック
# (push:refresh_notify_lock:<trading_date>、try_acquire_push_refresh_notify_lock)
# で同時実行を1プロセスに限定したうえで、push:refresh_notified_dateと
# 比較して防ぐ。notified_dateは「実際に送信処理(_dispatch_push_to_all)が
# 完了した後」にだけ更新するため、送信処理自体が例外で失敗した場合は
# notified_dateが更新されず、次回の実行(同じtrading_date)で再送を試みる
# ことができる(デプロイ通知側のdispatched_commitとは異なり、こちらは
# 「送信を試みた」ではなく「送信処理が完了した」ことを基準にする設計。
# 個々の購読者への送信失敗は_dispatch_push_to_all内で既に握られて次の
# 購読者へ処理を継続するため、それだけでは再送対象にならない)。
# ---------------------------------------------------------------------------

def notify_refresh_success(store, trading_date):
    """refresh.pyのmain()から、取引日が特定できた回にだけ呼ぶ想定。
    例外は一切外へ伝播させない(daily-refresh本体の終了コード・既存処理には
    絶対に影響させないため)。"""
    try:
        _notify_refresh_success(store, trading_date)
    except Exception:
        logger.exception(
            "push_notify: 株価更新通知処理で予期しない例外が発生しました"
            "(通知機能のみ影響、daily-refresh本体には影響しません)"
        )


def _notify_refresh_success(store, trading_date):
    if not _can_send_push():
        return
    if not trading_date:
        return

    notified = store.get_push_refresh_notified_date()
    if notified == trading_date:
        return  # この取引日は既に通知済み(同日内の2回目以降の実行)

    if not store.try_acquire_push_refresh_notify_lock(trading_date):
        return  # 他のプロセス/ワーカーが同時に処理中

    payload = {
        "title": "📈 Stock Buy App",
        "body": "株価データを更新しました",
        "kind": "refresh_success",
        "trading_date": trading_date,
    }
    _dispatch_push_to_all(store, payload, log_context=f"trading_date={trading_date}")
    store.set_push_refresh_notified_date(trading_date)


# ---------------------------------------------------------------------------
# 購読登録・解除
# ---------------------------------------------------------------------------

def _validate_subscription(sub):
    if not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if not isinstance(endpoint, str) or not _ENDPOINT_RE.match(endpoint) or len(endpoint) > 2000:
        return False
    if not isinstance(keys, dict):
        return False
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        return False
    if not (0 < len(p256dh) <= 256) or not (0 < len(auth) <= 256):
        return False
    return True


def add_subscription(store, sub):
    """購読情報を保存する(必要最小限: endpoint/keys.p256dh/keys.auth のみ)。"""
    if not _validate_subscription(sub):
        return False, "invalid subscription"

    minimal = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
    }

    subs = store.get_push_subscriptions() or []
    subs = [s for s in subs if s.get("endpoint") != minimal["endpoint"]]
    subs.append(minimal)
    if len(subs) > _MAX_SUBSCRIPTIONS:
        subs = subs[-_MAX_SUBSCRIPTIONS:]
    ok = store.set_push_subscriptions(subs)
    return bool(ok), None if ok else "redis write failed"


def remove_subscription(store, endpoint):
    if not isinstance(endpoint, str) or not endpoint:
        return False
    subs = store.get_push_subscriptions() or []
    new_subs = [s for s in subs if s.get("endpoint") != endpoint]
    if len(new_subs) == len(subs):
        return False
    return bool(store.set_push_subscriptions(new_subs))


# ---------------------------------------------------------------------------
# フォアグラウンド・フォールバック(アプリを開いた時に未通知の更新がないか確認)
# ---------------------------------------------------------------------------

def get_push_status(store):
    """Web Pushが端末に届かなかった場合のフォールバック用。
    現在のcommitに対してnotified_commitがまだ追いついていなければ
    pending_notification=Trueを返す(フロント側はこれを見てその場で通知を出す)。"""
    current = get_current_commit()
    if not current:
        return {"current_commit": None, "pending_notification": False}
    notified = store.get_push_notified_commit()
    pending = notified is not None and notified != current
    return {"current_commit": current, "pending_notification": pending}


def ack_notified(store, claimed_commit):
    """Service Workerのpush受信確認、またはフォアグラウンド・フォールバック通知の
    表示後に呼ぶ。サーバー自身が知っている現在のcommitと一致する場合のみ
    notified_commitを更新する(クライアントが任意の値を書き込めないようにするため)。"""
    current = get_current_commit()
    if not current or claimed_commit != current:
        return False
    return bool(store.set_push_notified_commit(current))
