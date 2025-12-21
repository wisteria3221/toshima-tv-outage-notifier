"""状態管理のテスト"""

import pytest

from src.scraper import OutageInfo
from src.state_manager import ChangeResult, StateManager, StatusChange


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
def sample_outage_with_status():
    """ステータス付きの障害情報"""
    return OutageInfo(
        id="100",
        date="2025.12.20",
        status="復旧",
        title="テスト障害",
        area="池袋",
        url="https://www.toshima.co.jp/trouble/detail/100",
    )


class TestStateManager:
    """StateManagerのテスト"""

    def test_create_initial_state(self, temp_state_file):
        """初期状態が正しく作成されること"""
        manager = StateManager(temp_state_file)

        assert manager.state["schema_version"] == "1.0"
        assert manager.state["outages"] == {}
        assert "stats" in manager.state

    def test_save_and_load_state(self, temp_state_file, sample_outage):
        """状態の保存と読み込みが正しく動作すること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()

        # 新しいインスタンスで読み込み
        manager2 = StateManager(temp_state_file)

        assert "100" in manager2.state["outages"]
        assert manager2.state["outages"]["100"]["title"] == "テスト障害"


class TestGetChanges:
    """差分検出のテスト"""

    def test_detect_new_outage(self, temp_state_file, sample_outage):
        """新規障害が検出されること"""
        manager = StateManager(temp_state_file)

        changes = manager.get_changes([sample_outage])

        assert len(changes.new_outages) == 1
        assert changes.new_outages[0].id == "100"
        assert len(changes.status_changes) == 0

    def test_no_changes_for_existing_outage(self, temp_state_file, sample_outage):
        """既存の障害は新規として検出されないこと"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.mark_notified("100", "")

        changes = manager.get_changes([sample_outage])

        assert len(changes.new_outages) == 0
        assert len(changes.status_changes) == 0

    def test_detect_status_change(
        self, temp_state_file, sample_outage, sample_outage_with_status
    ):
        """ステータス変更が検出されること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.mark_notified("100", "")

        changes = manager.get_changes([sample_outage_with_status])

        assert len(changes.new_outages) == 0
        assert len(changes.status_changes) == 1
        assert changes.status_changes[0].old_status == ""
        assert changes.status_changes[0].new_status == "復旧"

    def test_no_duplicate_status_notification(
        self, temp_state_file, sample_outage_with_status
    ):
        """同じステータスは重複通知されないこと"""
        manager = StateManager(temp_state_file)

        # 初回
        manager.update_outages([sample_outage_with_status])
        manager.mark_notified("100", "復旧")

        # 2回目
        changes = manager.get_changes([sample_outage_with_status])

        assert len(changes.status_changes) == 0


class TestMarkNotified:
    """通知済みマークのテスト"""

    def test_mark_notified_adds_status(self, temp_state_file, sample_outage):
        """通知済みステータスが追加されること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.mark_notified("100", "")

        assert "" in manager.state["outages"]["100"]["notified_statuses"]

    def test_mark_notified_does_not_duplicate(self, temp_state_file, sample_outage):
        """同じステータスは重複して追加されないこと"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.mark_notified("100", "")
        manager.mark_notified("100", "")

        assert manager.state["outages"]["100"]["notified_statuses"].count("") == 1


class TestNotificationCount:
    """通知カウントのテスト"""

    def test_increment_notification_count(self, temp_state_file):
        """通知カウントがインクリメントされること"""
        manager = StateManager(temp_state_file)

        assert manager.get_notification_count_this_month() == 0

        manager.increment_notification_count()
        assert manager.get_notification_count_this_month() == 1

        manager.increment_notification_count()
        assert manager.get_notification_count_this_month() == 2


class TestChangeResult:
    """ChangeResultのテスト"""

    def test_has_changes_with_new_outages(self, sample_outage):
        """新規障害がある場合はhas_changesがTrue"""
        result = ChangeResult(new_outages=[sample_outage], status_changes=[])
        assert result.has_changes() is True

    def test_has_changes_with_status_changes(self, sample_outage):
        """ステータス変更がある場合はhas_changesがTrue"""
        change = StatusChange(outage=sample_outage, old_status="", new_status="復旧")
        result = ChangeResult(new_outages=[], status_changes=[change])
        assert result.has_changes() is True

    def test_has_changes_empty(self):
        """変更がない場合はhas_changesがFalse"""
        result = ChangeResult(new_outages=[], status_changes=[])
        assert result.has_changes() is False

    def test_total_changes(self, sample_outage):
        """total_changesが正しい値を返すこと"""
        change = StatusChange(outage=sample_outage, old_status="", new_status="復旧")
        result = ChangeResult(new_outages=[sample_outage], status_changes=[change])
        assert result.total_changes() == 2
