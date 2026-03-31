"""通知モジュールのテスト"""

import pytest

from src.notifier import XNotifier, can_send_notification, should_notify_change
from src.scraper import OutageInfo
from src.state_manager import StateManager, StatusChange


@pytest.fixture
def temp_state_file(tmp_path):
    """一時的な状態ファイル"""
    return tmp_path / "state.json"


@pytest.fixture
def sample_outage():
    """テスト用の障害情報"""
    return OutageInfo(
        id="100",
        date="2025.12.20",
        status="",
        title="テスト障害",
        area="池袋",
        url="https://www.toshima.co.jp/trouble/detail/100",
    )


@pytest.fixture
def sample_outage_no_area():
    """地域なしの障害情報"""
    return OutageInfo(
        id="101",
        date="2025.12.21",
        status="",
        title="別エリア障害",
        area="",
        url="https://www.toshima.co.jp/trouble/detail/101",
    )


@pytest.fixture
def sample_outage_no_date():
    """日付なしの障害情報"""
    return OutageInfo(
        id="102",
        date="",
        status="",
        title="日付なし障害",
        area="南池袋",
        url="https://www.toshima.co.jp/trouble/detail/102",
    )


class TestFormatNewOutageMessage:
    """新規障害メッセージフォーマットのテスト"""

    def test_format_includes_correct_header(self, sample_outage):
        """正しいヘッダーが含まれること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage)
        assert "【としまテレビ 障害情報】" in message

    def test_format_includes_title(self, sample_outage):
        """タイトルが含まれること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.title in message

    def test_format_includes_date_when_present(self, sample_outage):
        """日付がある場合に含まれること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.date in message

    def test_format_omits_date_when_empty(self, sample_outage_no_date):
        """日付がない場合に「日時:」行が含まれないこと"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage_no_date)
        assert "日時:" not in message

    def test_format_includes_area_when_present(self, sample_outage):
        """地域がある場合に含まれること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.area in message

    def test_format_omits_area_when_empty(self, sample_outage_no_area):
        """地域がない場合に「地域:」行が含まれないこと"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage_no_area)
        assert "地域:" not in message

    def test_format_includes_url(self, sample_outage):
        """URLが含まれること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        message = notifier._format_new_outage_message(sample_outage)
        assert sample_outage.url in message


class TestFormatStatusChangeMessage:
    """ステータス変更メッセージフォーマットのテスト"""

    def _make_change(self, outage, new_status):
        return StatusChange(outage=outage, old_status="", new_status=new_status)

    def test_resolved_status_uses_resolution_header(self, sample_outage):
        """「復旧」ステータスで復旧ヘッダーが使われること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        change = self._make_change(sample_outage, "復旧")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 復旧情報】" in message

    def test_ended_status_uses_ended_header(self, sample_outage):
        """「終了」ステータスで終了ヘッダーが使われること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        change = self._make_change(sample_outage, "終了")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 終了情報】" in message

    def test_completed_status_uses_completed_header(self, sample_outage):
        """「完了」ステータスで完了ヘッダーが使われること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        change = self._make_change(sample_outage, "完了")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 完了情報】" in message

    def test_in_progress_status_uses_update_header(self, sample_outage):
        """「調査中」ステータスで更新ヘッダーが使われること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        change = self._make_change(sample_outage, "調査中")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 障害情報更新】" in message

    def test_empty_new_status_defaults_to_ongoing(self, sample_outage):
        """空のステータスが「進行中」として扱われること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        change = self._make_change(sample_outage, "")
        message = notifier._format_status_change_message(change)
        assert "【としまテレビ 障害情報更新】" in message
        assert "進行中" in message


class TestTruncateMessage:
    """メッセージ切り詰めのテスト"""

    def test_short_message_unchanged(self):
        """280文字以下のメッセージは変更されないこと"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        short_msg = "あ" * 100
        assert notifier._truncate_message(short_msg) == short_msg

    def test_long_message_truncated(self):
        """280文字超のメッセージが280文字に切り詰められること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        long_msg = "あ" * 300
        result = notifier._truncate_message(long_msg)
        assert len(result) == 280

    def test_truncated_message_ends_with_ellipsis(self):
        """切り詰められたメッセージが「...」で終わること"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        long_msg = "あ" * 300
        result = notifier._truncate_message(long_msg)
        assert result.endswith("...")

    def test_exactly_280_chars_unchanged(self):
        """ちょうど280文字のメッセージは変更されないこと"""
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = None
        exact_msg = "あ" * 280
        assert notifier._truncate_message(exact_msg) == exact_msg


class TestCanSendNotification:
    """月間制限チェックのテスト"""

    def test_can_send_when_under_limit(self, temp_state_file):
        """制限未満の場合に送信可能なこと"""
        manager = StateManager(temp_state_file)
        assert can_send_notification(manager) is True

    def test_cannot_send_at_limit(self, temp_state_file):
        """制限（450件）に達した場合に送信不可なこと"""
        manager = StateManager(temp_state_file)
        for _ in range(450):
            manager.increment_notification_count()
        assert can_send_notification(manager) is False

    def test_cannot_send_over_limit(self, temp_state_file):
        """制限を超えた場合に送信不可なこと"""
        manager = StateManager(temp_state_file)
        for _ in range(451):
            manager.increment_notification_count()
        assert can_send_notification(manager) is False


class TestShouldNotifyChange:
    """変更通知判定のテスト"""

    def test_allows_all_below_90_percent(self, temp_state_file):
        """90%未満の場合、どの変更タイプも通知できること"""
        manager = StateManager(temp_state_file)
        # 89% = int(450 * 0.89) = 400
        for _ in range(400):
            manager.increment_notification_count()
        assert should_notify_change(manager, "new") is True
        assert should_notify_change(manager, "status_change") is True

    def test_new_allowed_at_90_percent(self, temp_state_file):
        """90%以上の場合、新規障害は通知できること"""
        manager = StateManager(temp_state_file)
        # 90% = int(450 * 0.90) = 405
        for _ in range(405):
            manager.increment_notification_count()
        assert should_notify_change(manager, "new") is True

    def test_status_change_blocked_at_90_percent(self, temp_state_file):
        """90%以上の場合、ステータス変更は通知されないこと"""
        manager = StateManager(temp_state_file)
        for _ in range(405):
            manager.increment_notification_count()
        assert should_notify_change(manager, "status_change") is False

    def test_new_allowed_at_96_percent(self, temp_state_file):
        """96%以上の場合、新規障害は通知できること"""
        manager = StateManager(temp_state_file)
        # 96% = int(450 * 0.96) = 432
        for _ in range(432):
            manager.increment_notification_count()
        assert should_notify_change(manager, "new") is True

    def test_status_change_blocked_at_96_percent(self, temp_state_file):
        """96%以上の場合、ステータス変更は通知されないこと"""
        manager = StateManager(temp_state_file)
        for _ in range(432):
            manager.increment_notification_count()
        assert should_notify_change(manager, "status_change") is False


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
        change = StatusChange(outage=sample_outage, old_status="", new_status="復旧")
        result = notifier.notify_status_change(change)
        assert result is True
