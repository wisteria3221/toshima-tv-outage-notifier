"""通知モジュールのテスト"""

import json
from dataclasses import replace

import pytest
import requests
import tweepy

from src.config import MONTHLY_TWEET_LIMIT
from src.notifier import (
    _RATE_LIMIT_CRITICAL_RATIO,
    _RATE_LIMIT_REDUCED_RATIO,
    XNotifier,
    can_send_notification,
    should_notify_change,
    weighted_length,
)
from src.state_manager import StateManager, StatusChange


def _change(outage, new_status: str) -> StatusChange:
    """進行中（空文字）から new_status へのステータス変更"""
    return StatusChange(outage=outage, old_status="", new_status=new_status)


def _manager_with_count(state_file, count: int) -> StateManager:
    """今月の通知カウンタが count の StateManager を返す"""
    manager = StateManager(state_file)
    manager.state["stats"]["total_notifications_this_month"] = count
    return manager


class TestFormatNewOutageMessage:
    """新規障害メッセージフォーマットのテスト"""

    def test_format_includes_correct_header(self, notifier, sample_outage):
        """正しいヘッダーが含まれること"""
        message = notifier._format_new_outage_message(sample_outage)
        assert "【としまテレビ 障害情報】" in message

    def test_format_includes_title(self, notifier, sample_outage):
        """タイトルが含まれること"""
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.title in message

    def test_format_includes_date_when_present(self, notifier, sample_outage):
        """日付がある場合に含まれること"""
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.date in message

    def test_format_omits_date_when_empty(self, notifier, sample_outage):
        """日付がない場合に「日時:」行が含まれないこと"""
        message = notifier._format_new_outage_message(replace(sample_outage, date=""))
        assert "日時:" not in message

    def test_format_includes_area_when_present(self, notifier, sample_outage):
        """地域がある場合に含まれること"""
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.area in message

    def test_format_omits_area_when_empty(self, notifier, sample_outage):
        """地域がない場合に「地域:」行が含まれないこと"""
        message = notifier._format_new_outage_message(replace(sample_outage, area=""))
        assert "地域:" not in message

    def test_format_includes_url(self, notifier, sample_outage):
        """URLが含まれること"""
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.url in message

    def test_resolved_status_on_new_outage_uses_resolution_header(
        self, notifier, sample_outage
    ):
        """初検出時に既に「復旧」の障害は復旧ヘッダーで通知されること

        進行中として通知すると、復旧ステータスが通知済みになり続報が出ない。
        """
        message = notifier._format_new_outage_message(
            replace(sample_outage, status="復旧")
        )
        lines = message.split("\n")
        assert lines[0] == "【としまテレビ 復旧情報】"
        assert lines[1] == f"{sample_outage.title} が復旧しました"
        assert f"日時: {sample_outage.date}" in lines

    def test_other_status_on_new_outage_is_appended_to_title(
        self, notifier, sample_outage
    ):
        """初検出時に「仮復旧」など解決系以外のステータスがタイトルに付記されること"""
        message = notifier._format_new_outage_message(
            replace(sample_outage, status="仮復旧")
        )
        lines = message.split("\n")
        assert lines[0] == "【としまテレビ 障害情報】"
        assert lines[1] == f"{sample_outage.title}（仮復旧）"

    def test_empty_status_on_new_outage_shows_plain_title(
        self, notifier, sample_outage
    ):
        """ステータスなし（進行中）の新規障害はタイトルだけが表示されること"""
        assert sample_outage.status == ""
        message = notifier._format_new_outage_message(sample_outage)
        assert message.split("\n")[1] == sample_outage.title


class TestFormatStatusChangeMessage:
    """ステータス変更メッセージフォーマットのテスト"""

    def test_resolved_status_uses_resolution_header(self, notifier, sample_outage):
        """「復旧」ステータスで復旧ヘッダーが使われること"""
        change = _change(sample_outage, "復旧")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 復旧情報】" in message

    def test_ended_status_uses_ended_header(self, notifier, sample_outage):
        """「終了」ステータスで終了ヘッダーが使われること"""
        change = _change(sample_outage, "終了")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 終了情報】" in message

    def test_completed_status_uses_completed_header(self, notifier, sample_outage):
        """「完了」ステータスで完了ヘッダーが使われること"""
        change = _change(sample_outage, "完了")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 完了情報】" in message

    def test_in_progress_status_uses_update_header(self, notifier, sample_outage):
        """「調査中」ステータスで更新ヘッダーが使われること"""
        change = _change(sample_outage, "調査中")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 障害情報更新】" in message

    def test_empty_new_status_defaults_to_ongoing(self, notifier, sample_outage):
        """空のステータスが「進行中」として扱われること"""
        change = _change(sample_outage, "")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 障害情報更新】" in message
        assert "進行中" in message


class TestWeightedLength:
    """X の文字数カウント（weighted length）のテスト"""

    def test_ascii_counts_one_each(self):
        """ASCII 文字は 1 文字として数えること"""
        assert weighted_length("abc 123\n") == 8

    def test_japanese_counts_two_each(self):
        """日本語（CJK・全角記号）は 2 文字として数えること"""
        assert weighted_length("あいう") == 6
        assert weighted_length("【障害】") == 8

    def test_url_counts_as_23(self):
        """URL は長さにかかわらず 23 文字として数えること"""
        url = "https://www.toshima.co.jp/trouble/detail/91"
        assert len(url) > 23
        assert weighted_length(url) == 23
        assert weighted_length(f"詳細: {url}") == 2 * 2 + 2 + 23

    def test_empty_string_is_zero(self):
        """空文字は 0 であること"""
        assert weighted_length("") == 0


class TestTruncateMessage:
    """メッセージ切り詰め（最終防衛線）のテスト"""

    def test_short_message_unchanged(self, notifier):
        """上限以下のメッセージは変更されないこと"""
        short_msg = "あ" * 100
        assert notifier._truncate_message(short_msg) == short_msg

    def test_long_japanese_message_truncated_by_weight(self, notifier):
        """日本語 150 文字（len では 280 未満）でも重み付きで上限に収まること"""
        long_msg = "あ" * 150
        result = notifier._truncate_message(long_msg)
        assert weighted_length(result) <= XNotifier.MAX_TWEET_LENGTH
        assert result.endswith("...")

    def test_exactly_at_limit_unchanged(self, notifier):
        """重み付きでちょうど上限のメッセージは変更されないこと"""
        exact_msg = "あ" * 140  # 140 × 2 = 280
        assert weighted_length(exact_msg) == XNotifier.MAX_TWEET_LENGTH
        assert notifier._truncate_message(exact_msg) == exact_msg

    def test_url_line_is_kept_intact(self, notifier):
        """固定行だけで上限を超えても末尾の URL 行は切らないこと"""
        header = "【としまテレビ 障害情報】"
        url_line = "詳細: https://www.toshima.co.jp/trouble/detail/120"
        message = "\n".join([header, "タイトル...", "地域: " + "あ" * 200, url_line])
        assert weighted_length(message) > XNotifier.MAX_TWEET_LENGTH

        result = notifier._truncate_message(message)

        assert weighted_length(result) <= XNotifier.MAX_TWEET_LENGTH
        lines = result.split("\n")
        assert lines[0] == header
        assert lines[-1] == url_line
        assert lines[-2].endswith("...")

    def test_new_outage_with_huge_area_keeps_url(self, notifier, make_outage):
        """地域が異常に長い新規障害でも「詳細: URL」行が末尾に残ること"""
        outage = make_outage("120", title="一部地域 通信障害について", area="あ" * 200)

        message = notifier._format_new_outage_message(outage)

        assert weighted_length(message) <= XNotifier.MAX_TWEET_LENGTH
        assert message.split("\n")[-1] == f"詳細: {outage.url}"


class TestComposeMessageTruncation:
    """長いタイトルでも URL が残る切り詰めのテスト"""

    @pytest.fixture
    def long_outage(self, make_outage):
        """タイトル長を指定して障害情報を作るファクトリ"""

        def _make(title_len: int = 200):
            return make_outage(
                "1", date="2025.12.09", title="長" * title_len, area="池袋本町1丁目付近"
            )

        return _make

    def test_new_outage_long_title_keeps_url_and_header(self, notifier, long_outage):
        """新規障害メッセージが長いタイトルでも URL とヘッダーを残すこと"""
        outage = long_outage()
        message = notifier._format_new_outage_message(outage)
        lines = message.split("\n")
        assert weighted_length(message) <= XNotifier.MAX_TWEET_LENGTH
        assert lines[0] == "【としまテレビ 障害情報】"
        assert lines[-1] == f"詳細: {outage.url}"
        assert "日時: 2025.12.09" in lines
        assert "地域: 池袋本町1丁目付近" in lines
        assert lines[1].endswith("...")
        assert lines[1].startswith("長")

    def test_status_change_long_title_keeps_suffix_and_url(self, notifier, long_outage):
        """ステータス変更メッセージが長いタイトルでも「が復旧しました」と URL を残すこと"""
        outage = long_outage()
        message = notifier._format_status_change_message(_change(outage, "復旧"))
        lines = message.split("\n")
        assert weighted_length(message) <= XNotifier.MAX_TWEET_LENGTH
        assert lines[0] == "【としまテレビ 復旧情報】"
        assert lines[1].endswith("... が復旧しました")
        assert lines[-1] == f"詳細: {outage.url}"

    def test_title_within_limit_is_not_truncated(self, notifier, long_outage):
        """上限に収まるタイトルは切り詰められないこと"""
        outage = long_outage(title_len=50)
        message = notifier._format_new_outage_message(outage)
        assert "..." not in message
        assert outage.title in message

    def test_title_is_truncated_at_weighted_limit(self, notifier, long_outage):
        """len() では収まるが重み付きで超えるタイトルが切り詰められること"""
        # タイトル 130 文字 × 2 = 260 と固定行で 280 を超えるが len() では 280 未満
        outage = long_outage(title_len=130)
        message = notifier._format_new_outage_message(outage)
        assert len(message) < 280
        assert weighted_length(message) <= XNotifier.MAX_TWEET_LENGTH
        assert "..." in message


class TestCanSendNotification:
    """月間制限チェックのテスト"""

    def test_can_send_when_under_limit(self, temp_state_file):
        """制限未満の場合に送信可能なこと"""
        manager = StateManager(temp_state_file)
        assert can_send_notification(manager) is True

    def test_cannot_send_at_limit(self, temp_state_file):
        """制限（450件）に達した場合に送信不可なこと"""
        manager = _manager_with_count(temp_state_file, MONTHLY_TWEET_LIMIT)
        assert can_send_notification(manager) is False

    def test_cannot_send_over_limit(self, temp_state_file):
        """制限を超えた場合に送信不可なこと"""
        manager = _manager_with_count(temp_state_file, MONTHLY_TWEET_LIMIT + 1)
        assert can_send_notification(manager) is False


class TestShouldNotifyChange:
    """変更通知判定のテスト"""

    REDUCED = int(MONTHLY_TWEET_LIMIT * _RATE_LIMIT_REDUCED_RATIO)  # 90% = 405
    CRITICAL = int(MONTHLY_TWEET_LIMIT * _RATE_LIMIT_CRITICAL_RATIO)  # 96% = 432

    def test_allows_all_below_90_percent(self, temp_state_file):
        """90%未満の場合、どの変更タイプも通知できること"""
        manager = _manager_with_count(temp_state_file, self.REDUCED - 1)
        assert should_notify_change(manager, "new") is True
        assert should_notify_change(manager, "status_change") is True

    def test_new_allowed_at_90_percent(self, temp_state_file):
        """90%以上の場合、新規障害は通知できること"""
        manager = _manager_with_count(temp_state_file, self.REDUCED)
        assert should_notify_change(manager, "new") is True

    def test_status_change_blocked_at_90_percent(self, temp_state_file):
        """90%以上の場合、ステータス変更は通知されないこと"""
        manager = _manager_with_count(temp_state_file, self.REDUCED)
        assert should_notify_change(manager, "status_change") is False

    def test_new_allowed_at_96_percent(self, temp_state_file):
        """96%以上の場合、新規障害は通知できること"""
        manager = _manager_with_count(temp_state_file, self.CRITICAL)
        assert should_notify_change(manager, "new") is True

    def test_status_change_blocked_at_96_percent(self, temp_state_file):
        """96%以上の場合、ステータス変更は通知されないこと"""
        manager = _manager_with_count(temp_state_file, self.CRITICAL)
        assert should_notify_change(manager, "status_change") is False


def _forbidden(detail: str) -> tweepy.Forbidden:
    """X API v2 の 403 応答を模した tweepy.Forbidden を生成する"""
    response = requests.Response()
    response.status_code = 403
    response._content = json.dumps(
        {"title": "Forbidden", "type": "about:blank", "status": 403, "detail": detail}
    ).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return tweepy.Forbidden(response)


class TestPostTweetErrors:
    """投稿時のエラーハンドリングのテスト"""

    @pytest.fixture
    def failing_notifier(self, mocker, notifier):
        """create_tweet が指定した例外を投げる XNotifier を作るファクトリ"""
        mocker.patch("src.notifier.DRY_RUN", False)

        def _make(exc):
            notifier.client = mocker.Mock()
            notifier.client.create_tweet.side_effect = exc
            return notifier

        return _make

    def test_tweepy_exception_returns_false(self, failing_notifier):
        """tweepy の例外は False を返すこと"""
        notifier = failing_notifier(tweepy.TweepyException("api error"))
        assert notifier._post_tweet("hello") is False

    def test_duplicate_content_forbidden_is_treated_as_sent(self, failing_notifier):
        """同一内容による 403 は「既に投稿済み」として True を返すこと

        投稿成功後に状態の push が失敗すると次回同じ本文を再投稿して 403 になる。
        これを失敗扱いにすると巻き戻し → 再投稿 → 403 を毎回繰り返してしまう。
        """
        notifier = failing_notifier(
            _forbidden("You are not allowed to create a Tweet with duplicate content.")
        )
        assert notifier._post_tweet("hello") is True

    def test_other_forbidden_returns_false(self, failing_notifier):
        """同一内容以外の 403（権限不足など）は失敗として False を返すこと"""
        notifier = failing_notifier(
            _forbidden(
                "Your client app is not configured with the appropriate permissions."
            )
        )
        assert notifier._post_tweet("hello") is False

    def test_requests_exception_returns_false(self, failing_notifier):
        """requests の接続エラーも例外を伝播させず False を返すこと

        tweepy は接続エラーを TweepyException に包まないため、個別に捕捉が必要。
        """
        notifier = failing_notifier(requests.exceptions.ConnectionError("network down"))
        assert notifier._post_tweet("hello") is False


class TestXNotifierDryRun:
    """DRY_RUN モードでの通知テスト"""

    def test_notify_new_outage_dry_run_returns_true(self, mocker, sample_outage):
        """DRY_RUN モードで新規障害通知がTrueを返すこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        notifier = XNotifier()
        result = notifier.notify_new_outage(sample_outage)
        assert result is True

    def test_notify_status_change_dry_run_returns_true(self, mocker, sample_outage):
        """DRY_RUN モードでステータス変更通知がTrueを返すこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        notifier = XNotifier()
        result = notifier.notify_status_change(_change(sample_outage, "復旧"))
        assert result is True
