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

        assert manager.state["schema_version"] == "1.1"
        assert manager.state["outages"] == {}
        assert "stats" in manager.state
        assert "last_check" not in manager.state  # last_checkは削除済み

    def test_save_and_load_state(self, temp_state_file, sample_outage):
        """状態の保存と読み込みが正しく動作すること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        saved = manager.save_state()

        assert saved is True  # 変更があるので保存される

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


class TestDirtyFlag:
    """Dirty Flagのテスト"""

    def test_dirty_flag_initial_state(self, temp_state_file):
        """初期状態ではdirtyフラグがFalse"""
        manager = StateManager(temp_state_file)
        assert not manager.is_dirty()

    def test_dirty_flag_after_new_outage(self, temp_state_file):
        """新規障害追加後はdirtyフラグがTrue"""
        manager = StateManager(temp_state_file)

        outage = OutageInfo(
            id="1",
            date="2025.12.20",
            status="",
            title="テスト障害",
            area="池袋",
            url="https://example.com/1",
        )

        manager.update_outages([outage])
        assert manager.is_dirty()

    def test_dirty_flag_after_status_change(self, temp_state_file):
        """ステータス変更後はdirtyフラグがTrue"""
        # 既存障害を準備
        manager = StateManager(temp_state_file)

        outage1 = OutageInfo(
            id="1",
            date="2025.12.20",
            status="",
            title="Test",
            area="",
            url="https://example.com/1",
        )
        manager.update_outages([outage1])
        manager.save_state()

        # 新しいStateManagerで読み込み（dirty=Falseにリセット）
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # ステータス変更
        outage2 = OutageInfo(
            id="1",
            date="2025.12.20",
            status="復旧",
            title="Test",
            area="",
            url="https://example.com/1",
        )
        manager2.update_outages([outage2])
        assert manager2.is_dirty()

    def test_no_dirty_flag_for_identical_update(self, temp_state_file):
        """同一データでの更新ではdirtyフラグが立たない"""
        manager = StateManager(temp_state_file)

        outage = OutageInfo(
            id="1",
            date="2025.12.20",
            status="復旧",
            title="Test",
            area="池袋",
            url="https://example.com/1",
        )
        manager.update_outages([outage])
        manager.save_state()

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # 同一データで更新
        manager2.update_outages([outage])
        assert not manager2.is_dirty()

    def test_save_state_only_when_dirty(self, temp_state_file):
        """dirtyフラグがFalseの時は保存をスキップ"""
        manager = StateManager(temp_state_file)

        # 初期状態で保存
        assert manager.save_state(force=True)  # forceで保存
        assert not manager.is_dirty()

        # 変更なしで保存試行
        assert not manager.save_state()  # スキップ
        assert not manager.is_dirty()

    def test_save_state_force_override(self, temp_state_file):
        """force=Trueで強制保存可能"""
        manager = StateManager(temp_state_file)

        assert not manager.is_dirty()
        assert manager.save_state(force=True)

    def test_dirty_flag_reset_after_save(self, temp_state_file):
        """保存後はdirtyフラグがクリアされる"""
        manager = StateManager(temp_state_file)

        outage = OutageInfo(
            id="1",
            date="2025.12.20",
            status="",
            title="Test",
            area="",
            url="https://example.com/1",
        )
        manager.update_outages([outage])
        assert manager.is_dirty()

        manager.save_state()
        assert not manager.is_dirty()

    def test_mark_notified_marks_dirty(self, temp_state_file):
        """mark_notified()でdirtyフラグが立つ"""
        manager = StateManager(temp_state_file)

        outage = OutageInfo(
            id="1",
            date="2025.12.20",
            status="復旧",
            title="Test",
            area="",
            url="https://example.com/1",
        )
        manager.update_outages([outage])
        manager.save_state()

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # 通知マーク
        manager2.mark_notified("1", "復旧")
        assert manager2.is_dirty()

    def test_increment_notification_count_marks_dirty(self, temp_state_file):
        """increment_notification_count()でdirtyフラグが立つ"""
        manager = StateManager(temp_state_file)
        manager.save_state(force=True)

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # カウンター増加
        manager2.increment_notification_count()
        assert manager2.is_dirty()

    def test_month_rollover_marks_dirty(self, temp_state_file):
        """月が変わるとdirtyフラグが立つ"""
        from datetime import UTC, datetime

        manager = StateManager(temp_state_file)

        # 前月の状態を設定
        current_month = datetime.now(UTC).strftime("%Y-%m")
        prev_month = "2025-11"  # 前月を仮定

        manager.state["stats"] = {
            "month": prev_month,
            "total_notifications_this_month": 10,
        }
        manager.save_state(force=True)

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        # 手動で前月に戻す（テスト用）
        manager2.state["stats"]["month"] = prev_month
        manager2._dirty = False  # dirtyフラグをリセット

        # update_outagesを呼ぶと月チェックが走る
        manager2.update_outages([])

        # 月が変わっていればdirty
        if current_month != prev_month:
            assert manager2.is_dirty()
            assert manager2.state["stats"]["month"] == current_month
            assert manager2.state["stats"]["total_notifications_this_month"] == 0


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
