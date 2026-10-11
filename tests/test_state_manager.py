"""状態管理のテスト"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from src.state_manager import (
    ChangeResult,
    StateFileError,
    StateManager,
    StatusChange,
)


@pytest.fixture
def sample_outage_with_status(sample_outage):
    """ステータス付き（復旧）の障害情報"""
    return replace(sample_outage, status="復旧")


class TestStateManager:
    """StateManagerのテスト"""

    def test_create_initial_state(self, temp_state_file):
        """初期状態が正しく作成されること"""
        manager = StateManager(temp_state_file)

        assert manager.state["schema_version"] == "1.2"
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


class TestLoadState:
    """状態ファイル読み込みのテスト"""

    def test_missing_file_creates_initial_state(self, temp_state_file):
        """ファイルが存在しない場合は初期状態で開始すること（初回実行）"""
        manager = StateManager(temp_state_file)
        assert manager.state["outages"] == {}

    def test_invalid_json_raises(self, temp_state_file):
        """JSON として不正なファイルは StateFileError を送出すること

        初期状態で続行すると全障害を再通知し、履歴を上書きしてしまう。
        """
        temp_state_file.write_text("{ broken json", encoding="utf-8")
        with pytest.raises(StateFileError):
            StateManager(temp_state_file)

    def test_merge_conflict_marker_raises(self, temp_state_file):
        """マージ衝突マーカーが残ったファイルは StateFileError を送出すること"""
        temp_state_file.write_text(
            '<<<<<<< HEAD\n{"outages": {}}\n=======\n{"outages": {}}\n>>>>>>> main\n',
            encoding="utf-8",
        )
        with pytest.raises(StateFileError):
            StateManager(temp_state_file)

    def test_wrong_shape_raises(self, temp_state_file):
        """outages が辞書でないファイルは StateFileError を送出すること"""
        temp_state_file.write_text('{"outages": []}', encoding="utf-8")
        with pytest.raises(StateFileError):
            StateManager(temp_state_file)

    def test_file_is_not_overwritten_on_error(self, temp_state_file):
        """読み込みに失敗してもファイルの内容が変更されないこと"""
        broken = "{ broken json"
        temp_state_file.write_text(broken, encoding="utf-8")
        with pytest.raises(StateFileError):
            StateManager(temp_state_file)
        assert temp_state_file.read_text(encoding="utf-8") == broken


class TestSaveState:
    """状態ファイル保存（アトミック書き込み）のテスト"""

    def test_no_temp_file_left_after_save(self, temp_state_file, sample_outage):
        """保存後に一時ファイルが残らないこと"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()

        assert temp_state_file.exists()
        assert list(temp_state_file.parent.iterdir()) == [temp_state_file]

    def test_failed_write_keeps_original_file_intact(
        self, temp_state_file, sample_outage, make_outage, mocker
    ):
        """書き込み途中で失敗しても元のファイルが壊れず、一時ファイルも残らないこと

        途中まで書かれたファイルが残ると終了コード 1 でもコミットされ、
        次回実行が StateFileError で止まってしまう。
        """
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()
        original = temp_state_file.read_text(encoding="utf-8")

        other = make_outage("200", date="2025.12.21", title="別の障害", area="")
        manager.update_outages([sample_outage, other])

        def broken_dump(obj, fp, **kwargs):
            fp.write("{ partial")
            raise OSError("disk full")

        mocker.patch("src.state_manager.json.dump", side_effect=broken_dump)
        with pytest.raises(OSError):
            manager.save_state()

        assert temp_state_file.read_text(encoding="utf-8") == original
        assert list(temp_state_file.parent.iterdir()) == [temp_state_file]
        assert manager.is_dirty()


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

    def test_dirty_flag_after_new_outage(self, temp_state_file, sample_outage):
        """新規障害追加後はdirtyフラグがTrue"""
        manager = StateManager(temp_state_file)

        manager.update_outages([sample_outage])
        assert manager.is_dirty()

    def test_dirty_flag_after_status_change(
        self, temp_state_file, sample_outage, sample_outage_with_status
    ):
        """ステータス変更後はdirtyフラグがTrue"""
        # 既存障害を準備
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()

        # 新しいStateManagerで読み込み（dirty=Falseにリセット）
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # ステータス変更
        manager2.update_outages([sample_outage_with_status])
        assert manager2.is_dirty()

    def test_no_dirty_flag_for_identical_update(
        self, temp_state_file, sample_outage_with_status
    ):
        """同一データでの更新ではdirtyフラグが立たない"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage_with_status])
        manager.save_state()

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # 同一データで更新
        manager2.update_outages([sample_outage_with_status])
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

    def test_dirty_flag_reset_after_save(self, temp_state_file, sample_outage):
        """保存後はdirtyフラグがクリアされる"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        assert manager.is_dirty()

        manager.save_state()
        assert not manager.is_dirty()

    def test_mark_notified_marks_dirty(
        self, temp_state_file, sample_outage_with_status
    ):
        """mark_notified()でdirtyフラグが立つ"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage_with_status])
        manager.save_state()

        # 再読み込み
        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        # 通知マーク
        manager2.mark_notified(sample_outage_with_status.id, "復旧")
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
        manager = StateManager(temp_state_file)

        # 現在月と必ず異なる過去の月を設定する
        current_month = datetime.now(UTC).strftime("%Y-%m")
        prev_month = "2000-01"
        assert prev_month != current_month

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

        # 月が変わっているのでdirty
        assert manager2.is_dirty()
        assert manager2.state["stats"]["month"] == current_month
        assert manager2.state["stats"]["total_notifications_this_month"] == 0


class TestRollbackOutages:
    """rollback_outages のテスト"""

    def test_new_outage_is_removed(self, temp_state_file, sample_outage):
        """snapshot に無い新規障害はエントリごと削除されること"""
        manager = StateManager(temp_state_file)
        snapshot = manager.snapshot_outages()
        manager.update_outages([sample_outage])
        manager.mark_notified(sample_outage.id, sample_outage.status)

        manager.rollback_outages([sample_outage.id], snapshot)

        assert sample_outage.id not in manager.state["outages"]

    def test_existing_outage_is_restored(self, temp_state_file, sample_outage):
        """snapshot にある障害は更新前の内容（ステータス・通知履歴）に戻ること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.mark_notified(sample_outage.id, "")
        snapshot = manager.snapshot_outages()

        manager.update_outages([replace(sample_outage, status="復旧")])
        manager.mark_notified(sample_outage.id, "復旧")

        manager.rollback_outages([sample_outage.id], snapshot)

        stored = manager.state["outages"][sample_outage.id]
        assert stored["status"] == ""
        assert stored["notified_statuses"] == [""]

    def test_other_outages_are_untouched(
        self, temp_state_file, sample_outage, make_outage
    ):
        """対象外の障害は巻き戻しの影響を受けないこと"""
        manager = StateManager(temp_state_file)
        snapshot = manager.snapshot_outages()
        other = make_outage("200", date="2025.12.21", title="別の障害", area="")
        manager.update_outages([sample_outage, other])
        manager.mark_notified(other.id, "")

        manager.rollback_outages([sample_outage.id], snapshot)

        assert sample_outage.id not in manager.state["outages"]
        assert manager.state["outages"][other.id]["notified_statuses"] == [""]

    def test_snapshot_is_not_mutated_by_later_updates(
        self, temp_state_file, sample_outage
    ):
        """snapshot が後続の update_outages の影響を受けないこと（ディープコピー）"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        snapshot = manager.snapshot_outages()
        manager.mark_notified(sample_outage.id, "")

        assert snapshot[sample_outage.id]["notified_statuses"] == []


class TestRecordArchive:
    """魚拓記録（record_archive）のテスト"""

    ARCHIVE_URL = "https://web.archive.org/web/20261002051318/https://www.toshima.co.jp/trouble/detail/100"

    def test_new_outage_has_empty_archives(self, temp_state_file, sample_outage):
        """新規障害エントリは空の archives 配列を持つこと"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])

        assert manager.state["outages"]["100"]["archives"] == []

    def test_record_has_status_archived_at_and_url(
        self, temp_state_file, sample_outage
    ):
        """記録が status / archived_at / url だけを持ち、取得日時が UTC であること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        before = datetime.now(UTC)

        manager.record_archive("100", "復旧", self.ARCHIVE_URL)

        archives = manager.state["outages"]["100"]["archives"]
        assert len(archives) == 1
        record = archives[0]
        assert set(record) == {"status", "archived_at", "url"}
        assert record["status"] == "復旧"
        assert record["url"] == self.ARCHIVE_URL
        archived_at = datetime.fromisoformat(record["archived_at"])
        assert archived_at.tzinfo is not None
        assert archived_at.utcoffset().total_seconds() == 0
        assert before <= archived_at <= datetime.now(UTC)

    def test_second_record_keeps_first(self, temp_state_file, sample_outage):
        """別ステータスを追記しても 1 件目の記録が残ること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.record_archive("100", "", self.ARCHIVE_URL)

        manager.record_archive("100", "復旧", self.ARCHIVE_URL + "?v=2")

        archives = manager.state["outages"]["100"]["archives"]
        assert [a["status"] for a in archives] == ["", "復旧"]
        assert archives[0]["url"] == self.ARCHIVE_URL
        assert archives[1]["url"] == self.ARCHIVE_URL + "?v=2"

    def test_same_status_is_appended_again(self, temp_state_file, sample_outage):
        """同じステータスでも追記されること（再検出時の再取得も記録に残す）"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])

        manager.record_archive("100", "復旧", self.ARCHIVE_URL)
        manager.record_archive("100", "復旧", self.ARCHIVE_URL)

        assert len(manager.state["outages"]["100"]["archives"]) == 2

    def test_unknown_outage_is_ignored(self, temp_state_file, sample_outage):
        """state に存在しない障害 ID は無視され、例外も変更も起きないこと"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()
        assert not manager.is_dirty()

        manager.record_archive("999", "復旧", self.ARCHIVE_URL)

        assert "999" not in manager.state["outages"]
        assert manager.state["outages"]["100"]["archives"] == []
        assert not manager.is_dirty()

    def test_record_archive_marks_dirty(self, temp_state_file, sample_outage):
        """追記すると保存が必要な状態（dirty）になること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.save_state()

        manager2 = StateManager(temp_state_file)
        assert not manager2.is_dirty()

        manager2.record_archive("100", "復旧", self.ARCHIVE_URL)
        assert manager2.is_dirty()

    def test_old_schema_without_archives_is_loaded_as_is(
        self, temp_state_file, sample_outage, make_outage
    ):
        """archives を持たない旧形式のファイルをそのまま読み、追記できること"""
        old_state = {
            "schema_version": "1.1",
            "outages": {
                "100": {
                    "id": "100",
                    "date": "2025.12.20",
                    "status": "",
                    "title": "テスト障害",
                    "area": "池袋",
                    "url": "https://www.toshima.co.jp/trouble/detail/100",
                    "first_seen": "2025-12-20T00:00:00+00:00",
                    "last_updated": "2025-12-20T00:00:00+00:00",
                    "notified_statuses": [""],
                }
            },
            "stats": {"total_notifications_this_month": 0, "month": "2025-12"},
        }
        temp_state_file.write_text(
            json.dumps(old_state, ensure_ascii=False), encoding="utf-8"
        )

        manager = StateManager(temp_state_file)
        assert "archives" not in manager.state["outages"]["100"]
        # 版数はメモリ上で現行に置き換えるが、それだけでは保存対象にしない
        assert manager.state["schema_version"] == "1.2"
        assert not manager.is_dirty()

        # 旧形式のエントリにも追記できる（初回追記時に配列を作る）
        manager.record_archive("100", "復旧", self.ARCHIVE_URL)
        archives = manager.state["outages"]["100"]["archives"]
        assert len(archives) == 1
        assert archives[0]["status"] == "復旧"

        # 既存障害の更新でも記録と通知履歴が残る
        manager.update_outages([replace(sample_outage, status="復旧")])
        stored = manager.state["outages"]["100"]
        assert stored["status"] == "復旧"
        assert stored["archives"] == archives
        assert stored["notified_statuses"] == [""]

        # 新規障害には空の archives が作られる
        other = make_outage("200", date="2025.12.21", title="別の障害", area="")
        manager.update_outages([sample_outage, other])
        assert manager.state["outages"]["200"]["archives"] == []

    def test_update_outages_keeps_archives(self, temp_state_file, sample_outage):
        """既存障害の情報更新後も魚拓記録が残ること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.record_archive("100", "", self.ARCHIVE_URL)

        manager.update_outages([replace(sample_outage, status="復旧")])

        stored = manager.state["outages"]["100"]
        assert stored["status"] == "復旧"
        assert len(stored["archives"]) == 1
        assert stored["archives"][0]["url"] == self.ARCHIVE_URL

    def test_rollback_of_other_outage_keeps_archives(
        self, temp_state_file, sample_outage, make_outage
    ):
        """対象外の障害を巻き戻しても、別の障害の魚拓記録が消えないこと"""
        manager = StateManager(temp_state_file)
        other = make_outage("200", date="2025.12.21", title="別の障害", area="")
        snapshot = manager.snapshot_outages()
        manager.update_outages([sample_outage, other])
        manager.record_archive("100", "", self.ARCHIVE_URL)

        manager.rollback_outages(["200"], snapshot)

        assert "200" not in manager.state["outages"]
        archives = manager.state["outages"]["100"]["archives"]
        assert len(archives) == 1
        assert archives[0]["url"] == self.ARCHIVE_URL

    def test_archives_survive_save_and_load(self, temp_state_file, sample_outage):
        """保存して読み直しても魚拓記録が残ること"""
        manager = StateManager(temp_state_file)
        manager.update_outages([sample_outage])
        manager.record_archive("100", "", self.ARCHIVE_URL)
        manager.record_archive("100", "復旧", self.ARCHIVE_URL + "?v=2")
        manager.save_state()

        manager2 = StateManager(temp_state_file)

        archives = manager2.state["outages"]["100"]["archives"]
        assert [a["status"] for a in archives] == ["", "復旧"]
        assert archives[1]["url"] == self.ARCHIVE_URL + "?v=2"
        assert set(archives[0]) == {"status", "archived_at", "url"}


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


class TestStatusChange:
    """StatusChangeのテスト"""

    def test_describe_shows_ongoing_for_empty_status(self, sample_outage):
        """空のステータスは「進行中」と表記されること"""
        change = StatusChange(outage=sample_outage, old_status="", new_status="復旧")
        assert change.describe() == "進行中 -> 復旧"
