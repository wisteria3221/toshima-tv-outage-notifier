"""障害詳細ページの魚拓取得モジュール（Wayback Machine Save Page Now 2）

Wayback Machine への HTTP はこのモジュールにのみ置く。魚拓はベストエフォートであり、
失敗しても通知・状態保存・終了コードに影響させないため、すべての失敗を
WARNING ログと None に正規化し、呼び出し側に例外を送出しない。
"""

import logging
import time

import requests

from .config import (
    REQUEST_TIMEOUT,
    WAYBACK_IF_NOT_ARCHIVED_WITHIN,
    WAYBACK_POLL_INTERVAL,
    WAYBACK_SAVE_URL,
    WAYBACK_SUBMIT_RETRY_WAIT,
    WAYBACK_TIMEOUT,
    get_wayback_credentials,
    is_transient_status,
)

logger = logging.getLogger(__name__)

# 保存要求の試行回数（初回 + 再試行 1 回）
_SUBMIT_ATTEMPTS = 2

# 非 JSON 応答を警告に載せるときの本文の先頭文字数（原因の切り分け用。全文は出さない）
_BODY_PREVIEW_CHARS = 80


class _TransientError(Exception):
    """一時的な失敗（再試行してよい）を表す内部例外。モジュールの外には出さない"""


class _DeadlineError(Exception):
    """期限内に完了しなかったことを表す内部例外。モジュールの外には出さない"""


def _parse_json_object(response: requests.Response) -> tuple[dict | None, str]:
    """応答本文を JSON オブジェクトとして解析する

    SPN2 は過負荷時やメンテナンス時に HTML を返すことがあり、その場合は解析失敗を
    失敗応答として扱う。JSON ではあるがオブジェクトでない応答も失敗だが、調査の
    手がかりが違うので説明を分ける。

    Returns:
        (オブジェクト, 失敗の説明)。解析できればオブジェクトと空文字、
        できなければ None と説明文
    """
    try:
        payload = response.json()
    except ValueError:
        preview = response.text[:_BODY_PREVIEW_CHARS]
        return None, f"応答が JSON ではありません: {preview!r}"
    if not isinstance(payload, dict):
        return None, f"応答が JSON オブジェクトではありません: {type(payload).__name__}"
    return payload, ""


def _request_timeout(deadline: float) -> float:
    """次の HTTP 要求に使うタイムアウト（秒）。期限までの残り時間に切り詰める

    Raises:
        _DeadlineError: 残り時間が無い場合
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineError("期限を過ぎました")
    return min(REQUEST_TIMEOUT, remaining)


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

    def close(self) -> None:
        """HTTP セッションを閉じる"""
        self.session.close()

    def __enter__(self) -> WaybackArchiver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def archive(self, url: str, deadline: float | None = None) -> str | None:
        """url の魚拓を取り、公開 URL を返す

        無効時・失敗時は None。例外は送出しない。DRY RUN の判定は呼び出し側（main）が
        行い、DRY RUN ではこのメソッドを呼ばない。

        1 件に費やす時間は WAYBACK_TIMEOUT 秒まで（保存要求の再試行と完了確認を含む）。
        deadline が渡されればそれより後には待たない（呼び出し側の実行全体の予算）。
        個々の HTTP 要求のタイムアウトも残り時間に切り詰めるので、期限を超えて
        ブロックしない。期限を過ぎても Wayback 側で後から完了することはあるが記録しない。

        Args:
            url: 魚拓の対象（障害詳細ページの絶対 URL）
            deadline: これ以降は待たない時刻（time.monotonic() 基準）。None なら 1 件の上限のみ

        Returns:
            https://web.archive.org/web/<timestamp>/<url> 形式の公開 URL、または None
        """
        if not self._enabled:
            return None

        item_deadline = time.monotonic() + WAYBACK_TIMEOUT
        if deadline is not None:
            item_deadline = min(item_deadline, deadline)
        try:
            job_id = self._submit(url, item_deadline)
            if job_id is None:
                return None
            timestamp = self._wait_for_capture(job_id, item_deadline)
        except _DeadlineError as e:
            logger.warning(f"魚拓の取得が時間切れになりました: {url} - {e}")
            return None
        if timestamp is None:
            return None
        # 公開 URL の形式は SPN2 の契約どおり。成功ログは件の説明を知る呼び出し側が出す
        return f"https://web.archive.org/web/{timestamp}/{url}"

    def _submit(self, url: str, deadline: float) -> str | None:
        """保存要求を送り job_id を返す。失敗なら WARNING を出して None

        一時的な失敗（5xx・408/429・通信エラー・非 JSON 応答）は WAYBACK_SUBMIT_RETRY_WAIT
        秒待って 1 回だけ再試行する。SPN2 は過負荷時に 503 や HTML のメンテナンスページを
        返すことがあり、1 回で諦めるとその障害の魚拓は（実行をまたいで再試行しないため）
        二度と取れない。認証エラーなどの 4xx と job_id なしの応答は再試行しない。

        Raises:
            _DeadlineError: 要求や再試行の待機が期限に収まらない場合
        """
        error: _TransientError | None = None
        for attempt in range(_SUBMIT_ATTEMPTS):
            if attempt:
                if time.monotonic() + WAYBACK_SUBMIT_RETRY_WAIT >= deadline:
                    raise _DeadlineError(f"保存要求を再試行する前に期限: {error}")
                logger.info(
                    f"魚拓の保存要求を {WAYBACK_SUBMIT_RETRY_WAIT} 秒後に再試行します: "
                    f"{url} - {error}"
                )
                time.sleep(WAYBACK_SUBMIT_RETRY_WAIT)
            try:
                return self._post_save(url, deadline)
            except _TransientError as e:
                error = e
        logger.warning(f"魚拓の保存要求に失敗しました: {url} - {error}")
        return None

    def _post_save(self, url: str, deadline: float) -> str | None:
        """保存要求を 1 回送り job_id を返す

        一時的な失敗は _TransientError を送出する（呼び出し側が再試行）。再試行しても
        変わらない失敗（4xx の拒否、job_id なしの応答）は WARNING を出して None。

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
        timeout = _request_timeout(deadline)
        try:
            response = self.session.post(WAYBACK_SAVE_URL, data=form, timeout=timeout)
        except requests.RequestException as e:
            # requests の例外メッセージには URL は含まれるがヘッダ（認証情報）は含まれない
            raise _TransientError(str(e)) from e

        payload, problem = _parse_json_object(response)
        message = payload.get("message") if payload else None
        if not response.ok:
            detail = f"HTTP {response.status_code} {message or ''}".rstrip()
            if is_transient_status(response.status_code):
                raise _TransientError(detail)
            logger.warning(f"魚拓の保存要求が拒否されました: {url} - {detail}")
            return None
        if payload is None:
            # 200 でも HTML（メンテナンスページ）が返ることがある
            raise _TransientError(f"{problem} HTTP {response.status_code}")
        job_id = payload.get("job_id")
        if not job_id:
            # レート制限やセッション上限では 200 で {"message": ...} だけが返る
            logger.warning(
                f"魚拓の保存要求が受け付けられませんでした: {url} - {message or '理由不明'}"
            )
            return None
        return str(job_id)

    def _fetch_status(
        self, status_url: str, job_id: str, deadline: float
    ) -> dict | None:
        """ステータスを 1 回問い合わせる

        通信エラー・5xx・408/429・非 JSON 応答は一時的な失敗として _TransientError を
        送出する（呼び出し側が期限まで再試行する）。それ以外の 4xx は再試行しても
        変わらないので WARNING を出して None を返す。
        """
        timeout = _request_timeout(deadline)
        try:
            response = self.session.get(status_url, timeout=timeout)
        except requests.RequestException as e:
            raise _TransientError(str(e)) from e
        payload, problem = _parse_json_object(response)
        if not response.ok:
            message = payload.get("message") if payload else None
            detail = f"HTTP {response.status_code} {message or ''}".rstrip()
            if is_transient_status(response.status_code):
                raise _TransientError(detail)
            logger.warning(
                f"魚拓の完了確認が拒否されました: job_id={job_id} - {detail}"
            )
            return None
        if payload is None:
            raise _TransientError(f"{problem} HTTP {response.status_code}")
        return payload

    def _wait_for_capture(self, job_id: str, deadline: float) -> str | None:
        """status が success になるまで WAYBACK_POLL_INTERVAL 秒ごとに問い合わせ、timestamp を返す

        error 応答・想定外のステータス・4xx の拒否なら None。
        問い合わせ自体の通信エラー・5xx・408/429・非 JSON 応答は一時的な要因とみなし、
        期限まで問い合わせを続ける。SPN2 のステータス API は過負荷時に 502 や HTML を
        返すことがあるが、ジョブ自体は動き続けて完了するので、1 回の失敗で諦めると実在する
        魚拓を捨てることになる。401/403/404 などはキーの失効や無効な job_id であり、
        待っても変わらないので即座に諦め、原因が分かるよう HTTP ステータスを警告に出す。

        Raises:
            _DeadlineError: 期限までに success にならない場合
        """
        status_url = f"{WAYBACK_SAVE_URL}/status/{job_id}"
        while True:
            try:
                payload = self._fetch_status(status_url, job_id, deadline)
            except _TransientError as e:
                logger.info(
                    f"魚拓の完了確認に失敗したため再試行します: job_id={job_id} - {e}"
                )
                payload = None
            else:
                if payload is None:
                    # 4xx の拒否。警告は _fetch_status が出している
                    return None
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

            # 次の待機が終わる前に期限を超えるなら諦める（待ってから判定すると期限を
            # 待機間隔ぶん超過する）
            if time.monotonic() + WAYBACK_POLL_INTERVAL >= deadline:
                raise _DeadlineError(
                    f"job_id={job_id} が期限内（1 件あたり最大 {WAYBACK_TIMEOUT} 秒）に"
                    "完了しませんでした"
                )
            time.sleep(WAYBACK_POLL_INTERVAL)
