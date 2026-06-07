"""メイン処理のテスト"""

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

    def test_no_mark_when_notify_fails(self, mocker, tmp_path, sample_outage):
        """投稿が失敗した場合にマーク・カウンタ加算が呼ばれないこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
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

        assert main() == 0
        mark_notified.assert_not_called()
        increment.assert_not_called()
