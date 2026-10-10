"""メイン処理のテスト"""

import json

from src.main import EXIT_UPSTREAM_UNAVAILABLE, main
from src.scraper import (
    OutageInfo,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from src.state_manager import ChangeResult


class TestMainFunction:
    """main() 関数のテスト"""

    def test_returns_1_when_no_outages_fetched(self, mocker, tmp_path):
        """ページは取得できたが障害情報を1件も解析できない場合に1を返すこと"""
        mocker.patch("src.main.STATE_FILE_PATH", tmp_path / "state.json")
        mocker.patch("src.main.ToshimaScraper.fetch_outage_list", return_value=[])
        assert main() == 1

    def test_returns_1_when_state_file_is_corrupt(self, mocker, tmp_path):
        """状態ファイルが壊れている場合に1を返し、ファイルを書き換えないこと"""
        state_path = tmp_path / "state.json"
        state_path.write_text("{ broken json", encoding="utf-8")
        mocker.patch("src.main.STATE_FILE_PATH", state_path)
        fetch = mocker.patch("src.main.ToshimaScraper.fetch_outage_list")

        assert main() == 1

        fetch.assert_not_called()
        assert state_path.read_text(encoding="utf-8") == "{ broken json"

    def test_returns_2_when_upstream_unavailable(self, mocker, tmp_path):
        """上流サイトに到達できない場合に2を返し、状態ファイルを作らないこと"""
        state_path = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_path)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            side_effect=UpstreamUnavailableError("timeout"),
        )
        assert main() == EXIT_UPSTREAM_UNAVAILABLE
        assert EXIT_UPSTREAM_UNAVAILABLE == 2
        assert not state_path.exists()

    def test_returns_1_when_upstream_rejects(self, mocker, tmp_path):
        """上流が 4xx で拒否した場合に 2 ではなく 1 を返すこと（恒久的な破損の疑い）"""
        state_path = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_path)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            side_effect=UpstreamRejectedError("HTTP 404"),
        )
        assert main() == 1
        assert not state_path.exists()

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

    def test_returns_1_and_rolls_back_failed_outage_when_notify_fails(
        self, mocker, tmp_path, sample_outage
    ):
        """投稿が失敗した場合に1を返し、失敗した障害を state に残さないこと

        失敗分を state から外して保存することで、次回実行で再び「新規」として
        検出されリトライされる。
        """
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list",
            return_value=[sample_outage],
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        # 投稿が失敗（False）するケース
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=False)

        assert main() == 1

        state = json.loads(state_file.read_text())
        assert sample_outage.id not in state["outages"]
        assert state["stats"]["total_notifications_this_month"] == 0

    def test_partial_failure_keeps_successful_marks(self, mocker, tmp_path):
        """2 件中 1 件が失敗した場合、成功分のマークとカウンタは保存されること

        成功分を保存しないと、次回実行で同じ障害を二重に投稿してしまう。
        """
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        ok = OutageInfo(
            id="1",
            date="2025.12.20",
            status="",
            title="成功する障害",
            area="",
            url="https://www.toshima.co.jp/trouble/detail/1",
        )
        ng = OutageInfo(
            id="2",
            date="2025.12.21",
            status="",
            title="失敗する障害",
            area="",
            url="https://www.toshima.co.jp/trouble/detail/2",
        )
        mocker.patch("src.main.ToshimaScraper.fetch_outage_list", return_value=[ok, ng])
        mocker.patch("src.main.can_send_notification", return_value=True)
        mocker.patch(
            "src.main.XNotifier.notify_new_outage",
            side_effect=lambda outage: outage.id == ok.id,
        )

        assert main() == 1

        state = json.loads(state_file.read_text())
        assert state["outages"][ok.id]["notified_statuses"] == [""]
        assert ng.id not in state["outages"]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_status_change_is_notified_and_marked(
        self, mocker, tmp_path, sample_outage
    ):
        """既知障害のステータス変更が通知され、新ステータスがマークされること"""
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        mocker.patch("src.notifier.DRY_RUN", True)
        self._write_known_outage(state_file, sample_outage, notified=[""])
        resolved = OutageInfo(
            id=sample_outage.id,
            date=sample_outage.date,
            status="復旧",
            title=sample_outage.title,
            area=sample_outage.area,
            url=sample_outage.url,
        )
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list", return_value=[resolved]
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        notify = mocker.patch(
            "src.main.XNotifier.notify_status_change", return_value=True
        )

        assert main() == 0

        notify.assert_called_once()
        assert notify.call_args.args[0].new_status == "復旧"
        state = json.loads(state_file.read_text())
        stored = state["outages"][sample_outage.id]
        assert stored["status"] == "復旧"
        assert stored["notified_statuses"] == ["", "復旧"]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_status_change_failure_restores_old_status(
        self, mocker, tmp_path, sample_outage
    ):
        """ステータス変更の投稿が失敗した場合、旧ステータスのまま保存されること

        旧ステータスに戻しておくことで、次回実行で同じ変更が再検出される。
        """
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        self._write_known_outage(state_file, sample_outage, notified=[""])
        resolved = OutageInfo(
            id=sample_outage.id,
            date=sample_outage.date,
            status="復旧",
            title=sample_outage.title,
            area=sample_outage.area,
            url=sample_outage.url,
        )
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list", return_value=[resolved]
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        mocker.patch("src.main.XNotifier.notify_status_change", return_value=False)

        assert main() == 1

        state = json.loads(state_file.read_text())
        stored = state["outages"][sample_outage.id]
        assert stored["status"] == ""
        assert stored["notified_statuses"] == [""]

    def test_skipped_change_is_stored_without_mark(
        self, mocker, tmp_path, sample_outage
    ):
        """レート制限でスキップした変更は、通知済みにせず新ステータスで保存されること"""
        state_file = tmp_path / "state.json"
        mocker.patch("src.main.STATE_FILE_PATH", state_file)
        self._write_known_outage(state_file, sample_outage, notified=[""])
        resolved = OutageInfo(
            id=sample_outage.id,
            date=sample_outage.date,
            status="復旧",
            title=sample_outage.title,
            area=sample_outage.area,
            url=sample_outage.url,
        )
        mocker.patch(
            "src.main.ToshimaScraper.fetch_outage_list", return_value=[resolved]
        )
        mocker.patch("src.main.can_send_notification", return_value=True)
        mocker.patch("src.main.should_notify_change", return_value=False)
        notify = mocker.patch("src.main.XNotifier.notify_status_change")

        assert main() == 0

        notify.assert_not_called()
        state = json.loads(state_file.read_text())
        stored = state["outages"][sample_outage.id]
        assert stored["status"] == "復旧"
        assert stored["notified_statuses"] == [""]

    @staticmethod
    def _write_known_outage(state_file, outage, notified):
        """既知障害 1 件を含む状態ファイルを書き出す"""
        state = {
            "schema_version": "1.1",
            "outages": {
                outage.id: {
                    "id": outage.id,
                    "date": outage.date,
                    "status": outage.status,
                    "title": outage.title,
                    "area": outage.area,
                    "url": outage.url,
                    "first_seen": "2025-12-20T00:00:00+00:00",
                    "last_updated": "2025-12-20T00:00:00+00:00",
                    "notified_statuses": list(notified),
                }
            },
            "stats": {"total_notifications_this_month": 0, "month": "2000-01"},
        }
        state_file.write_text(json.dumps(state, ensure_ascii=False))

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
