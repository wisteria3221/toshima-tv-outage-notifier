"""メイン処理のテスト"""

import json
import logging
from dataclasses import replace

import pytest

from src.config import MONTHLY_TWEET_LIMIT
from src.main import EXIT_UPSTREAM_UNAVAILABLE, main
from src.scraper import UpstreamRejectedError, UpstreamUnavailableError
from src.state_manager import ChangeResult, StateManager


@pytest.fixture(autouse=True)
def state_file(mocker, temp_state_file):
    """main() が読み書きする状態ファイルを一時ファイルに向ける"""
    mocker.patch("src.main.STATE_FILE_PATH", temp_state_file)
    return temp_state_file


@pytest.fixture
def fetch(mocker):
    """スクレイパーの取得結果を差し替えるモック（return_value / side_effect を設定して使う）"""
    return mocker.patch("src.main.ToshimaScraper.fetch_outage_list")


@pytest.fixture
def allow_sending(mocker):
    """月間上限に達していない状態として扱う"""
    return mocker.patch("src.main.can_send_notification", return_value=True)


def _write_state(state_file, outages=(), notified=(), count=0):
    """本体の StateManager を通して状態ファイルを書き出す

    手書きの JSON だとスキーマ変更に黙って追従しなくなるため、保存形式は本体に任せる。
    """
    manager = StateManager(state_file)
    manager.update_outages(list(outages))
    for outage in outages:
        for status in notified:
            manager.mark_notified(outage.id, status)
    manager.state["stats"]["total_notifications_this_month"] = count
    manager.save_state(force=True)


def _read_state(state_file) -> dict:
    return json.loads(state_file.read_text())


class TestMainFunction:
    """main() 関数のテスト"""

    def test_returns_1_when_no_outages_fetched(self, fetch):
        """ページは取得できたが障害情報を1件も解析できない場合に1を返すこと"""
        fetch.return_value = []
        assert main() == 1

    def test_returns_1_when_state_file_is_corrupt(self, state_file, fetch):
        """状態ファイルが壊れている場合に1を返し、ファイルを書き換えないこと"""
        state_file.write_text("{ broken json", encoding="utf-8")

        assert main() == 1

        fetch.assert_not_called()
        assert state_file.read_text(encoding="utf-8") == "{ broken json"

    def test_returns_2_when_upstream_unavailable(self, state_file, fetch):
        """上流サイトに到達できない場合に2を返し、状態ファイルを作らないこと"""
        fetch.side_effect = UpstreamUnavailableError("timeout")
        assert main() == EXIT_UPSTREAM_UNAVAILABLE
        assert EXIT_UPSTREAM_UNAVAILABLE == 2
        assert not state_file.exists()

    def test_returns_1_when_upstream_rejects(self, state_file, fetch):
        """上流が 4xx で拒否した場合に 2 ではなく 1 を返すこと（恒久的な破損の疑い）"""
        fetch.side_effect = UpstreamRejectedError("HTTP 404")
        assert main() == 1
        assert not state_file.exists()

    def test_returns_0_when_no_changes(self, mocker, fetch, sample_outage):
        """変更がない場合に0を返すこと"""
        fetch.return_value = [sample_outage]
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[], status_changes=[]),
        )
        assert main() == 0

    def test_returns_0_on_successful_notification(
        self, mocker, fetch, allow_sending, sample_outage
    ):
        """DRY_RUN モードで正常に通知が送れた場合に0を返すこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [sample_outage]
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[sample_outage], status_changes=[]),
        )
        mocker.patch("src.main.should_notify_change", return_value=True)
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=True)
        assert main() == 0

    def test_returns_1_on_unexpected_exception(self, fetch):
        """予期しない例外が発生した場合に1を返すこと"""
        fetch.side_effect = RuntimeError("予期しないエラー")
        assert main() == 1

    def test_returns_1_and_rolls_back_failed_outage_when_notify_fails(
        self, mocker, state_file, fetch, allow_sending, sample_outage
    ):
        """投稿が失敗した場合に1を返し、失敗した障害を state に残さないこと

        失敗分を state から外して保存することで、次回実行で再び「新規」として
        検出されリトライされる。
        """
        fetch.return_value = [sample_outage]
        # 投稿が失敗（False）するケース
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=False)

        assert main() == 1

        state = _read_state(state_file)
        assert sample_outage.id not in state["outages"]
        assert state["stats"]["total_notifications_this_month"] == 0

    def test_partial_failure_keeps_successful_marks(
        self, mocker, state_file, fetch, allow_sending, make_outage
    ):
        """2 件中 1 件が失敗した場合、成功分のマークとカウンタは保存されること

        成功分を保存しないと、次回実行で同じ障害を二重に投稿してしまう。
        """
        ok = make_outage("1", title="成功する障害", area="")
        ng = make_outage("2", date="2025.12.21", title="失敗する障害", area="")
        fetch.return_value = [ok, ng]
        mocker.patch(
            "src.main.XNotifier.notify_new_outage",
            side_effect=lambda outage: outage.id == ok.id,
        )

        assert main() == 1

        state = _read_state(state_file)
        assert state["outages"][ok.id]["notified_statuses"] == [""]
        assert ng.id not in state["outages"]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_exception_during_notify_is_treated_as_failure(
        self, mocker, state_file, fetch, allow_sending, make_outage
    ):
        """投稿関数が例外を投げても、成功分を保存し失敗分だけ巻き戻して 1 を返すこと

        tweepy は接続エラーを requests の例外のまま投げる。以前はこれが main() の
        外側まで抜けて状態が保存されず、同じ実行で成功した通知のマークも失われて
        次回二重投稿になっていた。
        """
        ok = make_outage("1", title="成功する障害", area="")
        ng = make_outage("2", date="2025.12.21", title="例外になる障害", area="")
        fetch.return_value = [ok, ng]

        def notify(outage):
            if outage.id == ng.id:
                raise ConnectionError("network down")
            return True

        mocker.patch("src.main.XNotifier.notify_new_outage", side_effect=notify)

        assert main() == 1

        state = _read_state(state_file)
        assert state["outages"][ok.id]["notified_statuses"] == [""]
        assert ng.id not in state["outages"]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_monthly_limit_is_enforced_within_a_single_run(
        self, mocker, state_file, fetch, make_outage
    ):
        """残り 1 件の状態で新規が 2 件あっても、上限を超えて送らないこと

        1 回の実行で複数件送るとその間にカウンタが進むため、can_send_notification を
        1 件ごとに評価しないと上限を超えてしまう。
        """
        mocker.patch("src.notifier.DRY_RUN", True)
        _write_state(state_file, count=MONTHLY_TWEET_LIMIT - 1)
        first = make_outage("1", title="送られる障害", area="")
        second = make_outage("2", date="2025.12.21", title="上限で止まる障害", area="")
        fetch.return_value = [first, second]
        notify = mocker.patch("src.main.XNotifier.notify_new_outage", return_value=True)

        assert main() == 0

        notify.assert_called_once()
        assert notify.call_args.args[0].id == first.id
        state = _read_state(state_file)
        assert state["stats"]["total_notifications_this_month"] == MONTHLY_TWEET_LIMIT
        assert state["outages"][first.id]["notified_statuses"] == [""]
        # 上限で止まった分は状態には保存されるが通知済みにはならない（既存仕様と同じ）
        assert state["outages"][second.id]["notified_statuses"] == []

    def test_status_change_is_notified_and_marked(
        self, mocker, state_file, fetch, allow_sending, sample_outage
    ):
        """既知障害のステータス変更が通知され、新ステータスがマークされること"""
        mocker.patch("src.notifier.DRY_RUN", True)
        _write_state(state_file, [sample_outage], notified=[""])
        fetch.return_value = [replace(sample_outage, status="復旧")]
        notify = mocker.patch(
            "src.main.XNotifier.notify_status_change", return_value=True
        )

        assert main() == 0

        notify.assert_called_once()
        assert notify.call_args.args[0].new_status == "復旧"
        state = _read_state(state_file)
        stored = state["outages"][sample_outage.id]
        assert stored["status"] == "復旧"
        assert stored["notified_statuses"] == ["", "復旧"]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_status_change_failure_restores_old_status(
        self, mocker, state_file, fetch, allow_sending, sample_outage
    ):
        """ステータス変更の投稿が失敗した場合、旧ステータスのまま保存されること

        旧ステータスに戻しておくことで、次回実行で同じ変更が再検出される。
        """
        _write_state(state_file, [sample_outage], notified=[""])
        fetch.return_value = [replace(sample_outage, status="復旧")]
        mocker.patch("src.main.XNotifier.notify_status_change", return_value=False)

        assert main() == 1

        stored = _read_state(state_file)["outages"][sample_outage.id]
        assert stored["status"] == ""
        assert stored["notified_statuses"] == [""]

    def test_skipped_change_is_stored_without_mark(
        self, mocker, state_file, fetch, allow_sending, sample_outage
    ):
        """レート制限でスキップした変更は、通知済みにせず新ステータスで保存されること"""
        _write_state(state_file, [sample_outage], notified=[""])
        fetch.return_value = [replace(sample_outage, status="復旧")]
        mocker.patch("src.main.should_notify_change", return_value=False)
        notify = mocker.patch("src.main.XNotifier.notify_status_change")

        assert main() == 0

        notify.assert_not_called()
        stored = _read_state(state_file)["outages"][sample_outage.id]
        assert stored["status"] == "復旧"
        assert stored["notified_statuses"] == [""]

    def test_skipped_notifications_are_summarized_once(
        self, mocker, fetch, make_outage, caplog
    ):
        """上限でスキップした件数は 1 回の WARNING にまとめ、件ごとには警告しないこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [
            make_outage(str(i), title=f"障害{i}", area="") for i in range(3)
        ]
        # 件ごとのチェックで全件スキップさせる（ループ前のまとめ判定は無い）
        mocker.patch(
            "src.main.can_send_notification", side_effect=[False, False, False]
        )
        notify = mocker.patch("src.main.XNotifier.notify_new_outage")

        with caplog.at_level(logging.WARNING):
            assert main() == 0

        notify.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "3 件の通知を投稿制限のためスキップしました" in warnings[0].getMessage()

    def test_new_outage_marks_notified_status_end_to_end(
        self, mocker, state_file, fetch, allow_sending, sample_outage
    ):
        """新規障害の通知後、notified_statuses が保存されること（順序バグの回帰テスト）

        update_outages を通知ループの前に呼ぶことで、mark_notified が新規障害を
        正しくマークできる。以前は update_outages が後だったため常に空のままだった。
        """
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [sample_outage]
        # get_changes / mark_notified / update_outages は本物を使う

        assert main() == 0

        state = _read_state(state_file)
        outage = state["outages"][sample_outage.id]
        assert outage["notified_statuses"] == [sample_outage.status]
        assert state["stats"]["total_notifications_this_month"] == 1
