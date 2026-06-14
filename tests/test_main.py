"""メイン処理のテスト"""

import json

from src.main import main
from src.state_manager import ChangeResult


class TestMainFunction:
    """main() 関数のテスト"""

    def test_returns_1_when_no_outages_fetched(self, mocker, tmp_path):
        """スクレイピングで障害情報が取得できない場合に1を返すこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
        mocker.patch("src.main.ToshimaScraper.fetch_outage_list", return_value=[])
        assert main() == 1

    def test_returns_0_when_no_changes(self, mocker, tmp_path, sample_outage):
        """変更がない場合に0を返すこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            return_value=[sample_outage],
        )
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[], status_changes=[]),
        )
        assert main() == 0

    def test_returns_0_on_successful_notification(
        self, mocker, tmp_path, sample_outage
    ):
        """DRY_RUN モードで正常に通知が送れた場合に0を返すこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
        mocker.patch("src.notifier.DRY_RUN", True)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            return_value=[sample_outage],
        )
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[sample_outage], status_changes=[]),
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        mocker.patch("src.main.should_notify_change", return_value=True)
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=True)
        assert main() == 0

    def test_returns_1_on_unexpected_exception(self, mocker, tmp_path):
        """予期しない例外が発生した場合に1を返すこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            side_effect=RuntimeError("予期しないエラー"),
        )
        assert main() == 1

    def test_returns_1_and_does_not_save_when_notify_fails(
        self, mocker, tmp_path, sample_outage
    ):
        """投稿が失敗した場合に1を返し、マーク・カウンタ加算・状態保存を行わないこと

        状態を保存・コミットしないことで、未通知の障害が次回実行でリトライされる。
        """
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            return_value=[sample_outage],
        )
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[sample_outage], status_changes=[]),
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        mocker.patch("src.main.should_notify_change", return_value=True)
        # 投稿が失敗（False）するケース
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=False)
        mark_notified = mocker.patch("src.main.StateManager.mark_notified")
        increment = mocker.patch("src.main.StateManager.increment_notification_count")
        save_state = mocker.patch("src.main.StateManager.save_state")

        assert main() == 1
        mark_notified.assert_not_called()
        increment.assert_not_called()
        save_state.assert_not_called()

    def test_new_outage_marks_notified_status_end_to_end(
        self, mocker, tmp_path, sample_outage
    ):
        """新規障害の通知後、notified_statuses が保存されること（順序バグの回帰テスト）

        update_outages を通知ループの前に呼ぶことで、mark_notified が新規障害を
        正しくマークできる。以前は update_outages が後だったため常に空のままだった。
        """
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        mocker.patch("src.notifier.DRY_RUN", True)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            return_value=[sample_outage],
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        # get_changes / mark_notified / update_outages は本物を使う

        assert main() == 0

        state = json.loads(state_file.read_text())
        outage = state["outages"][sample_outage.id]
        assert outage["notified_statuses"] == [sample_outage.status]
        assert state["stats"]["total_notifications_this_month"] == 1
