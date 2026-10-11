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
    WAYBACK_IF_NOT_ARCHIVED_WITHIN,
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
def no_sleep(mocker):
    """ポーリング間隔の待機をテストでは省略する

    上限判定が壊れてポーリングが止まらなくなった場合、responses は同じ pending 応答を
    無限に返すため、ハングではなく明示的に失敗させる。
    """
    calls = 0

    def _guard(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > _MAX_SLEEP_CALLS:
            raise AssertionError("ポーリングが上限回数を超えて続いています")

    return mocker.patch("src.archiver.time.sleep", side_effect=_guard)


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
    def test_non_json_response_returns_none(self, archiver, no_sleep, caplog):
        """JSON でない応答が続けば 1 回再試行した後に None を返すこと"""
        _add_submit("<html>maintenance</html>")
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        assert len(_warnings(caplog)) == 1
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
    def test_pending_past_deadline_returns_none(
        self, archiver, mocker, no_sleep, caplog
    ):
        """pending のまま上限時間を超えたら None を返し、時間切れを警告すること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"status": "pending"})
        # 開始 0 秒 → 1 回目の pending 後 10 秒（次の待機後も上限内）
        # → 2 回目の pending 後に上限超過
        mocker.patch(
            "src.archiver.time.monotonic",
            side_effect=[0.0, 10.0, WAYBACK_TIMEOUT + 1.0],
        )
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "時間切れ" in warnings[0].getMessage()
        no_sleep.assert_called_once()
        # POST 1 回 + GET 2 回。上限超過後は問い合わせない
        assert len(responses_lib.calls) == 3

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
        self, archiver, mocker, no_sleep, caplog
    ):
        """問い合わせの失敗が上限時間まで続けば None を返し、時間切れを警告すること"""
        _add_submit({"url": TARGET_URL, "job_id": JOB_ID})
        _add_status({"message": "upstream error"}, status=502)
        mocker.patch(
            "src.archiver.time.monotonic",
            side_effect=[0.0, WAYBACK_TIMEOUT + 1.0],
        )
        with caplog.at_level(logging.WARNING):
            assert archiver.archive(TARGET_URL) is None
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "時間切れ" in warnings[0].getMessage()
        no_sleep.assert_not_called()
        assert len(responses_lib.calls) == 2

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
