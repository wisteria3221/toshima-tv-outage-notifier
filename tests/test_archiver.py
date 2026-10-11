"""魚拓取得（WaybackArchiver）のテスト

Wayback Machine への HTTP は responses でモックする。responses は登録していない URL への
要求を ConnectionError にするため、テストから本物の保存要求が漏れることはない。
"""

import logging
from urllib.parse import parse_qs

import pytest
import requests
import responses as responses_lib

from src.archiver import WaybackArchiver
from src.config import (
    REQUEST_TIMEOUT,
    WAYBACK_IF_NOT_ARCHIVED_WITHIN,
    WAYBACK_POLL_INTERVAL,
    WAYBACK_SAVE_URL,
    WAYBACK_SUBMIT_RETRY_WAIT,
    WAYBACK_TIMEOUT,
)

ACCESS_KEY = "test-access-key"
SECRET_KEY = "s3cr3t-wayback-key"
TARGET_URL = "https://www.toshima.co.jp/trouble/detail/121"
JOB_ID = "spn2-job-abc"
STATUS_URL = f"{WAYBACK_SAVE_URL}/status/{JOB_ID}"
TIMESTAMP = "20261002051318"
ARCHIVED_URL = f"https://web.archive.org/web/{TIMESTAMP}/{TARGET_URL}"


# ポーリングの上限判定が壊れたときにテストがハングしないための待機回数の上限
_MAX_SLEEP_CALLS = 100


@pytest.fixture(autouse=True)
def no_sleep(mocker, fake_clock):
    """待機を省き、その秒数だけ偽の時計を進める

    期限の判定は偽の時計で行われるので、待機した分だけ時刻が進めば本物と同じ経路を通る。
    上限判定が壊れてポーリングが止まらなくなった場合、responses は同じ pending 応答を
    無限に返すため、ハングではなく明示的に失敗させる。
    """
    calls = 0

    def _sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > _MAX_SLEEP_CALLS:
            raise AssertionError("ポーリングが上限回数を超えて続いています")
        fake_clock.sleep(seconds)

    return mocker.patch("time.sleep", side_effect=_sleep)


@pytest.fixture
def wayback_credentials(monkeypatch):
    """認証情報を設定して archiver を有効にする

    conftest の autouse フィクスチャが既定で削除しているため、必要なテストだけ設定する。
    """
    monkeypatch.setenv("WAYBACK_ACCESS_KEY", ACCESS_KEY)
    monkeypatch.setenv("WAYBACK_SECRET_KEY", SECRET_KEY)


@pytest.fixture
def archiver(wayback_credentials):
    """認証情報が揃った有効な archiver"""
    return WaybackArchiver()


def _add_submit(payload: dict | str, status: int = 200) -> None:
    """保存要求（POST）の応答を登録する。payload が文字列なら非 JSON 本文として登録する"""
    if isinstance(payload, str):
        responses_lib.add(
            responses_lib.POST, WAYBACK_SAVE_URL, body=payload, status=status
        )
    else:
        responses_lib.add(
            responses_lib.POST, WAYBACK_SAVE_URL, json=payload, status=status
        )


def _add_status(payload: dict | str, status: int = 200) -> None:
    """ステータス問い合わせ（GET）の応答を登録する。同じ URL に複数登録すると順に返る"""
    if isinstance(payload, str):
        responses_lib.add(responses_lib.GET, STATUS_URL, body=payload, status=status)
    else:
        responses_lib.add(responses_lib.GET, STATUS_URL, json=payload, status=status)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class TestWaybackArchiver:
    """生成時の有効・無効判定と DRY RUN、セッションの後始末のテスト"""

    def test_enabled_with_credentials(self, archiver, caplog):
        """認証情報が揃っていれば有効になり、警告を出さないこと"""
        with caplog.at_level(logging.WARNING):
            assert archiver.enabled is True
        assert _warnings(caplog) == []

    def test_session_headers(self, archiver):
        """JSON 受け入れと LOW 形式の認証ヘッダがセッションに設定されること"""
        assert archiver.session.headers["Accept"] == "application/json"
        assert (
            archiver.session.headers["Authorization"]
            == f"LOW {ACCESS_KEY}:{SECRET_KEY}"
        )

    @pytest.mark.parametrize(
        "access_key, secret_key",
        [("", ""), (ACCESS_KEY, ""), ("", SECRET_KEY)],
        ids=["both-missing", "secret-missing", "access-missing"],
    )
    @responses_lib.activate
    def test_disabled_without_credentials(
        self, monkeypatch, caplog, access_key, secret_key
    ):
        """認証情報が欠けていれば無効になり、警告を生成時に 1 回だけ出し、要求を送らないこと"""
        monkeypatch.setenv("WAYBACK_ACCESS_KEY", access_key)
        monkeypatch.setenv("WAYBACK_SECRET_KEY", secret_key)
        with caplog.at_level(logging.WARNING):
            archiver = WaybackArchiver()
            assert archiver.enabled is False
            assert archiver.archive(TARGET_URL) is None
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "認証情報が未設定" in warnings[0].getMessage()
        assert len(responses_lib.calls) == 0
        # 無効時はセッションに認証ヘッダを持たない
        assert "Authorization" not in archiver.session.headers

    def test_context_manager_closes_session(self, wayback_credentials, mocker):
        """with ブロックを抜けるとセッションが閉じられること"""
        with WaybackArchiver() as archiver:
            close = mocker.patch.object(archiver.session, "close")
        close.assert_called_once()

    def test_context_manager_closes_session_when_disabled(self, mocker):
        """無効な archiver でも with ブロックを抜けたときに例外にならないこと"""
        with WaybackArchiver() as archiver:
            assert archiver.enabled is False
        archiver.close()


class TestArchive:
    """archive() の正常系のテスト"""

    @responses_lib.activate
    def test_returns_public_url_after_pending_then_success(self, archiver):
        """pending の後に success になれば公開 URL を返すこと"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "pending"})
        _add_status(
            {"status": "success", "timestamp": TIMESTAMP, "original_url": TARGET_URL}
        )

        result = archiver.archive(TARGET_URL)

        assert result == f"https://web.archive.org/web/{TIMESTAMP}/{TARGET_URL}"
        assert len(responses_lib.calls) == 3

    @responses_lib.activate
    def test_submit_request_shape(self, archiver, no_sleep):
        """保存要求のフォーム項目と認証ヘッダが SPN2 の契約どおりであること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success", "timestamp": TIMESTAMP})

        archiver.archive(TARGET_URL)

        request = responses_lib.calls[0].request
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"LOW {ACCESS_KEY}:{SECRET_KEY}"
        assert request.headers["Accept"] == "application/json"
        assert parse_qs(request.body) == {
            "url": [TARGET_URL],
            "skip_first_archive": ["1"],
            "js_behavior_timeout": ["0"],
            "if_not_archived_within": [str(WAYBACK_IF_NOT_ARCHIVED_WITHIN)],
        }
        # 即座に success なら待機しない
        no_sleep.assert_not_called()

    @responses_lib.activate
    def test_polls_with_same_auth_header(self, archiver):
        """ステータス問い合わせにも同じ認証ヘッダが付くこと"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success", "timestamp": TIMESTAMP})

        archiver.archive(TARGET_URL)

        status_request = responses_lib.calls[1].request
        assert status_request.url == STATUS_URL
        assert (
            status_request.headers["Authorization"] == f"LOW {ACCESS_KEY}:{SECRET_KEY}"
        )


class TestSubmitFailures:
    """保存要求（POST）が失敗したときのテスト。いずれも None を返し例外を外に出さない

    一時的な失敗（5xx・408/429・通信エラー・非 JSON）は 1 回だけ再試行し、
    それ以外の 4xx と job_id なしの応答は再試行しない。
    """

    @responses_lib.activate
    def test_transient_error_is_retried_once_then_succeeds(
        self, archiver, no_sleep, caplog
    ):
        """503 の後に成功すれば公開 URL を返し、警告は出さないこと"""
        _add_submit({"message": "overloaded"}, status=503)
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        with caplog.at_level(logging.INFO):
            assert archiver.archive(TARGET_URL) == ARCHIVED_URL
        assert _warnings(caplog) == []
        assert any("再試行" in r.getMessage() for r in caplog.records)
        no_sleep.assert_called_once_with(WAYBACK_SUBMIT_RETRY_WAIT)
        assert len(responses_lib.calls) == 3

    @responses_lib.activate
    def test_http_429_is_retried_once_then_returns_none(
        self, archiver, no_sleep, caplog
    ):
        """429 が続けば 1 回再試行した後に None を返し、HTTP ステータスを警告に含めること"""
        _add_submit({"message": "Too many requests"}, status=429)
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "429" in warnings[0].getMessage()
        assert "Too many requests" in warnings[0].getMessage()
        no_sleep.assert_called_once()
        assert len(responses_lib.calls) == 2
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_http_401_is_not_retried(self, archiver, no_sleep, caplog):
        """401（認証エラー）は再試行せず None を返し、HTTP ステータスを警告に含めること"""
        _add_submit({"message": "You need to be logged in"}, status=401)
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "401" in warnings[0].getMessage()
        no_sleep.assert_not_called()
        assert len(responses_lib.calls) == 1
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_missing_job_id_returns_none_with_message(self, archiver, caplog):
        """200 でも job_id が無ければ None を返し、応答の message を警告に含めること"""
        _add_submit(
            {"message": "You have already reached the limit of active sessions"}
        )
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "limit of active sessions" in warnings[0].getMessage()
        assert len(responses_lib.calls) == 1
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_connection_error_returns_none(self, archiver, no_sleep, caplog):
        """接続エラーが続けば 1 回再試行した後に None を返し、例外を外に出さないこと"""
        responses_lib.add(
            responses_lib.POST,
            WAYBACK_SAVE_URL,
            body=requests.ConnectionError("connection refused"),
        )
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        assert len(_warnings(caplog)) == 1
        no_sleep.assert_called_once()
        assert len(responses_lib.calls) == 2
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_retry_is_skipped_when_wait_would_pass_deadline(
        self, archiver, no_sleep, fake_clock, caplog
    ):
        """再試行の待機が期限に収まらなければ待たずに諦め、時間切れを警告すること"""
        _add_submit({"message": "overloaded"}, status=503)
        deadline = fake_clock.now + WAYBACK_SUBMIT_RETRY_WAIT - 1
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL, deadline=deadline) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "時間切れ" in warnings[0].getMessage()
        assert "503" in warnings[0].getMessage()
        no_sleep.assert_not_called()
        assert len(responses_lib.calls) == 1

    @responses_lib.activate
    def test_non_json_response_returns_none(self, archiver, no_sleep, caplog):
        """JSON でない応答が続けば 1 回再試行した後に None を返すこと"""
        _add_submit("<html>maintenance</html>")
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        # 原因の切り分け用に本文の先頭を載せる
        assert "JSON ではありません" in warnings[0].getMessage()
        assert "<html>maintenance" in warnings[0].getMessage()
        no_sleep.assert_called_once()
        assert len(responses_lib.calls) == 2


class TestWaitForCaptureFailures:
    """完了確認（GET）が失敗したときのテスト。いずれも None を返し例外を外に出さない"""

    @responses_lib.activate
    def test_error_status_returns_none_with_status_ext(self, archiver, caplog):
        """error 応答では None を返し、status_ext と message を警告に含めること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status(
            {
                "status": "error",
                "status_ext": "error:not-found",
                "message": "The server cannot find the requested resource",
            }
        )
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "error:not-found" in warnings[0].getMessage()
        assert "cannot find the requested resource" in warnings[0].getMessage()
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_pending_past_deadline_returns_none(self, archiver, no_sleep, caplog):
        """pending のまま上限時間を超えたら None を返し、時間切れを警告すること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "pending"})
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "時間切れ" in warnings[0].getMessage()
        assert TARGET_URL in warnings[0].getMessage()
        # 待機の合計が上限を超える手前で諦める（待ってから判定すると間隔ぶん超過する）
        polls = WAYBACK_TIMEOUT // WAYBACK_POLL_INTERVAL
        assert no_sleep.call_count == polls - 1
        assert len(responses_lib.calls) == 1 + polls

    @responses_lib.activate
    def test_run_deadline_caps_item_deadline(
        self, archiver, no_sleep, fake_clock, caplog
    ):
        """呼び出し側の期限が 1 件の上限より早ければ、そちらで打ち切ること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "pending"})
        deadline = fake_clock.now + WAYBACK_POLL_INTERVAL * 2 + 1
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL, deadline=deadline) is None
        assert "時間切れ" in _warnings(caplog)[0].getMessage()
        # 0 秒・5 秒で問い合わせ、10 秒の問い合わせの後は期限に収まらないので待たない
        assert no_sleep.call_count == 2
        assert len(responses_lib.calls) == 1 + 3
        assert fake_clock.now < deadline

    @responses_lib.activate
    def test_request_timeout_is_clamped_to_remaining_time(
        self, archiver, mocker, fake_clock
    ):
        """個々の HTTP 要求のタイムアウトを期限までの残り時間に切り詰めること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        post = mocker.spy(archiver.session, "post")
        get = mocker.spy(archiver.session, "get")
        remaining = REQUEST_TIMEOUT / 2

        assert archiver.archive(TARGET_URL, deadline=fake_clock.now + remaining)

        assert post.call_args.kwargs["timeout"] == remaining
        assert get.call_args.kwargs["timeout"] == remaining

    @responses_lib.activate
    def test_request_timeout_defaults_to_request_timeout(self, archiver, mocker):
        """残り時間に余裕があれば通常の REQUEST_TIMEOUT を使うこと"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        post = mocker.spy(archiver.session, "post")

        assert archiver.archive(TARGET_URL) == ARCHIVED_URL

        assert post.call_args.kwargs["timeout"] == REQUEST_TIMEOUT

    @responses_lib.activate
    def test_deadline_already_passed_sends_nothing(
        self, archiver, no_sleep, fake_clock, caplog
    ):
        """期限を過ぎてから呼ばれたら要求を送らずに None を返し、時間切れを警告すること"""
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL, deadline=fake_clock.now - 1) is None
        assert "時間切れ" in _warnings(caplog)[0].getMessage()
        assert len(responses_lib.calls) == 0
        no_sleep.assert_not_called()

    @responses_lib.activate
    def test_status_http_error_is_retried_until_success(
        self, archiver, no_sleep, caplog
    ):
        """ステータス問い合わせの 5xx は一時的な失敗として再問い合わせし、
        その後 success になれば公開 URL を返すこと（警告は出さない）"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"message": "upstream error"}, status=502)
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        with caplog.at_level(logging.INFO):
            assert archiver.archive(TARGET_URL) == ARCHIVED_URL
        assert _warnings(caplog) == []
        assert any("再試行" in r.getMessage() for r in caplog.records)
        no_sleep.assert_called_once()
        assert len(responses_lib.calls) == 3

    @responses_lib.activate
    def test_status_connection_error_is_retried(self, archiver, caplog):
        """ステータス問い合わせの接続エラーも再問い合わせし、例外を外に出さないこと"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        responses_lib.add(
            responses_lib.GET, STATUS_URL, body=requests.ConnectionError("reset")
        )
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        with caplog.at_level(logging.INFO):
            assert archiver.archive(TARGET_URL) == ARCHIVED_URL
        assert _warnings(caplog) == []
        assert SECRET_KEY not in caplog.text

    @responses_lib.activate
    def test_status_non_json_is_retried(self, archiver, caplog):
        """ステータス応答が JSON でなくても再問い合わせすること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status("<html>busy</html>")
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        with caplog.at_level(logging.INFO):
            assert archiver.archive(TARGET_URL) == ARCHIVED_URL
        assert _warnings(caplog) == []

    @responses_lib.activate
    def test_persistent_status_error_returns_none_at_deadline(
        self, archiver, no_sleep, fake_clock, caplog
    ):
        """問い合わせの失敗が上限時間まで続けば None を返し、時間切れを警告すること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})

        def _slow_502(_request):
            # 1 回目の問い合わせが上限直前までかかった
            fake_clock.advance(WAYBACK_TIMEOUT - 1)
            return 502, {}, '{"message": "upstream error"}'

        responses_lib.add_callback(responses_lib.GET, STATUS_URL, callback=_slow_502)
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "時間切れ" in warnings[0].getMessage()
        no_sleep.assert_not_called()
        assert len(responses_lib.calls) == 2

    @responses_lib.activate
    def test_non_object_json_is_distinguished_from_non_json(self, archiver, caplog):
        """JSON だがオブジェクトでない応答は「JSON ではない」とは別の説明で再試行すること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status([])
        _add_status({"status": "success", "timestamp": TIMESTAMP})
        with caplog.at_level(logging.INFO):
            assert archiver.archive(TARGET_URL) == ARCHIVED_URL
        retry = [r.getMessage() for r in caplog.records if "再試行" in r.getMessage()]
        assert len(retry) == 1
        assert "JSON オブジェクトではありません" in retry[0]
        assert "list" in retry[0]

    @responses_lib.activate
    def test_status_client_error_is_not_retried(self, archiver, no_sleep, caplog):
        """ステータス問い合わせの 404 などは再試行せず None を返し、HTTP ステータスを警告に含めること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"message": "job not found"}, status=404)
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "404" in warnings[0].getMessage()
        assert "job not found" in warnings[0].getMessage()
        no_sleep.assert_not_called()
        assert len(responses_lib.calls) == 2

    @responses_lib.activate
    def test_success_without_timestamp_returns_none(self, archiver, caplog):
        """success でも timestamp が無ければ公開 URL を組み立てられないので None"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "success"})
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        assert len(_warnings(caplog)) == 1
