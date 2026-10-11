"""障害詳細ページの魚拓取得モジュール（Wayback Machine Save Page Now 2）

Wayback Machine への HTTP はこのモジュールにのみ置く。魚拓はベストエフォートであり、
失敗しても通知・状態保存・終了コードに影響させないため、すべての失敗を
WARNING ログと None に正規化し、呼び出し側に例外を送出しない。
"""

import logging
import time

import requests

from .config import (
    DRY_RUN,
    REQUEST_TIMEOUT,
    WAYBACK_IF_NOT_ARCHIVED_WITHIN,
    WAYBACK_POLL_INTERVAL,
    WAYBACK_SAVE_URL,
    WAYBACK_TIMEOUT,
    get_wayback_credentials,
)

logger = logging.getLogger(__name__)


def _parse_json_object(response: requests.Response) -> dict | None:
    """応答本文を JSON オブジェクトとして解析する。JSON でない・オブジェクトでなければ None

    SPN2 は過負荷時やメンテナンス時に HTML を返すことがあり、
    その場合は解析失敗を失敗応答として扱う。
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class WaybackArchiver:
    """Wayback Machine（SPN2）で URL の魚拓を取るクラス

    認証情報（WAYBACK_ACCESS_KEY / WAYBACK_SECRET_KEY）が揃っているときだけ有効になる。
    認証情報の値はログ・例外メッセージに含めず、セッションのヘッダにだけ設定する。
    """

    def __init__(self) -> None:
        """認証情報を読み、無ければ無効化して WARNING を 1 回出す

        有効なら requests.Session に JSON 受け入れと LOW 形式の認証ヘッダを設定する。
        無効でもセッションは作っておき、close() とコンテキストマネージャが
        有効・無効を区別せずに使えるようにする（ネットワークには触れない）。
        """
        access_key, secret_key = get_wayback_credentials()
        self._enabled = bool(access_key and secret_key)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if self._enabled:
            self.session.headers.update(
                {"Authorization": f"LOW {access_key}:{secret_key}"}
            )
        else:
            # 魚拓の対象がある実行でしか生成されないため、警告は生成時の 1 回で足りる
            logger.warning("魚拓の認証情報が未設定のため取得をスキップします")

    @property
    def enabled(self) -> bool:
        """認証情報が揃っているとき True"""
        return self._enabled

    def _fetch_status(self, status_url: str, job_id: str) -> dict | None:
        """ステータスを 1 回問い合わせる

        通信エラー・5xx・非 JSON 応答は一時的な失敗として INFO に出し None を返す
        （呼び出し側が上限時間まで再試行する。上限超過は呼び出し側が WARNING にする）。
        """
        try:
            response = self.session.get(status_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.info(
                f"魚拓の完了確認に失敗したため再試行します: job_id={job_id} - {e}"
            )
            return None
        payload = _parse_json_object(response)
        if not response.ok or payload is None:
            logger.info(
                f"魚拓の完了確認の応答が不正なため再試行します: job_id={job_id} - "
                f"HTTP {response.status_code}"
            )
            return None
        return payload

    def close(self) -> None:
        """HTTP セッションを閉じる"""
        self.session.close()

    def __enter__(self) -> WaybackArchiver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def archive(self, url: str) -> str | None:
        """url の魚拓を取り、公開 URL を返す

        無効時・DRY RUN 時・失敗時は None。例外は送出しない。

        Args:
            url: 魚拓の対象（障害詳細ページの絶対 URL）

        Returns:
            https://web.archive.org/web/<timestamp>/<url> 形式の公開 URL、または None
        """
        if not self._enabled:
            return None
        if DRY_RUN:
            logger.info(f"[DRY RUN] 魚拓の取得をスキップ: {url}")
            return None

        job_id = self._submit(url)
        if job_id is None:
            return None
        timestamp = self._wait_for_capture(job_id)
        if timestamp is None:
            return None
        # 公開 URL の形式は SPN2 の契約どおり。成功ログは件の説明を知る呼び出し側が出す
        return f"https://web.archive.org/web/{timestamp}/{url}"

    def _submit(self, url: str) -> str | None:
        """保存要求を送り job_id を返す。応答に job_id が無ければ message を WARNING に出して None

        フォーム項目の意味:
        - skip_first_archive=1: 初回キャプチャかどうかの判定を省いて高速化する
        - js_behavior_timeout=0: 詳細ページは静的なので JS の動作待ちを省く
        - if_not_archived_within: この秒数内の既存キャプチャは流用せず新規に取る。
          既定の 1 時間では新規検出から 1 時間以内の復旧で検出時の古いキャプチャが
          返るため、実行間隔（10 分）より短くして変更のたびに新しいキャプチャを作る
        - capture_all は付けない: 詳細ページが削除され 404 になった場合は
          error:not-found で失敗にし、エラーページを魚拓として記録しない
        """
        form = {
            "url": url,
            "skip_first_archive": "1",
            "js_behavior_timeout": "0",
            "if_not_archived_within": str(WAYBACK_IF_NOT_ARCHIVED_WITHIN),
        }
        try:
            response = self.session.post(
                WAYBACK_SAVE_URL, data=form, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            # requests の例外メッセージには URL は含まれるがヘッダ（認証情報）は含まれない
            logger.warning(f"魚拓の保存要求に失敗しました: {url} - {e}")
            return None

        payload = _parse_json_object(response)
        message = payload.get("message") if payload else None
        if not response.ok:
            logger.warning(
                f"魚拓の保存要求が拒否されました: {url} - "
                f"HTTP {response.status_code} {message or ''}".rstrip()
            )
            return None
        if payload is None:
            logger.warning(
                f"魚拓の保存要求の応答が JSON ではありません: {url} - "
                f"HTTP {response.status_code}"
            )
            return None
        job_id = payload.get("job_id")
        if not job_id:
            # レート制限やセッション上限では 200 で {"message": ...} だけが返る
            logger.warning(
                f"魚拓の保存要求が受け付けられませんでした: {url} - {message or '理由不明'}"
            )
            return None
        return str(job_id)

    def _wait_for_capture(self, job_id: str) -> str | None:
        """status が success になるまで WAYBACK_POLL_INTERVAL 秒ごとに問い合わせ、timestamp を返す

        error 応答・想定外のステータス・WAYBACK_TIMEOUT 超過なら None。
        問い合わせ自体の通信エラー・5xx・非 JSON 応答は一時的な要因とみなし、上限時間まで
        問い合わせを続ける。SPN2 のステータス API は過負荷時に 502 や HTML を返すことが
        あるが、ジョブ自体は動き続けて完了するので、1 回の失敗で諦めると実在する魚拓を
        捨てることになる。待ち時間の上限は deadline で別に保証されているので延びない。
        """
        status_url = f"{WAYBACK_SAVE_URL}/status/{job_id}"
        deadline = time.monotonic() + WAYBACK_TIMEOUT
        while True:
            payload = self._fetch_status(status_url, job_id)
            if payload is not None:
                status = payload.get("status")
                if status == "success":
                    timestamp = payload.get("timestamp")
                    if not timestamp:
                        logger.warning(
                            f"魚拓の完了応答に timestamp がありません: job_id={job_id}"
                        )
                        return None
                    return str(timestamp)
                if status == "error":
                    logger.warning(
                        f"魚拓の取得がエラーになりました: job_id={job_id} - "
                        f"{payload.get('status_ext', '')} {payload.get('message', '')}".rstrip()
                    )
                    return None
                if status != "pending":
                    logger.warning(
                        f"魚拓の完了確認が想定外のステータスです: job_id={job_id} - {status}"
                    )
                    return None

            # 次の待機が終わる前に上限を超えるなら諦める（待ってから判定すると上限を
            # 待機間隔ぶん超過する）。Wayback 側で後から完了しても記録しない（再試行しない）
            if time.monotonic() + WAYBACK_POLL_INTERVAL >= deadline:
                logger.warning(
                    f"魚拓の取得が時間切れになりました: job_id={job_id} - "
                    f"{WAYBACK_TIMEOUT} 秒以内に完了しませんでした"
                )
                return None
            time.sleep(WAYBACK_POLL_INTERVAL)
