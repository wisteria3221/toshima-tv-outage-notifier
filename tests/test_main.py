"""メイン処理のテスト"""

import json
import logging
from dataclasses import replace

import pytest

from src.archiver import WaybackArchiver
from src.config import MONTHLY_TWEET_LIMIT, WAYBACK_RUN_BUDGET
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


@pytest.fixture(autouse=True)
def archiver_class(mocker):
    """main() が生成する WaybackArchiver をクラスごとモックに差し替える

    既定では有効（enabled=True）で archive() は成功（公開 URL を返す）。これにより
    認証情報なしの警告や魚拓失敗のサマリが他のテストの WARNING 件数に混ざらず、
    魚拓の失敗を試したいテストは side_effect を None にして return_value を上書きする。
    本物の archiver を使うテストは、このフィクスチャより後に patch し直す。
    """
    cls = mocker.patch("src.main.WaybackArchiver")
    instance = cls.return_value
    instance.enabled = True
    instance.archive.side_effect = _archived
    instance.__enter__.return_value = instance
    return cls


@pytest.fixture
def archiver(archiver_class):
    """モック archiver のインスタンス（archive の呼び出しと戻り値を扱う）"""
    return archiver_class.return_value


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
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warnings == [
            "3 件の通知を投稿制限のためスキップしました（今月 0 件送信済み）"
        ]

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


ARCHIVED_URL = "https://web.archive.org/web/20261002051318/{url}"


def _archived(url: str, deadline: float | None = None) -> str:
    """詳細 URL に対応する魚拓の公開 URL（モック archiver の戻り値用）"""
    return ARCHIVED_URL.format(url=url)


class TestArchiveIntegration:
    """main() の通知ループ後に行う魚拓取得のテスト"""

    def test_archives_each_change_after_all_notifications(
        self, mocker, state_file, fetch, allow_sending, archiver, make_outage
    ):
        """新規 1 件 + ステータス変更 1 件で、通知 2 件の後に魚拓が各詳細 URL で 2 回呼ばれ、
        各ステータスの記録が保存されること"""
        known = make_outage("1", title="既知の障害", area="")
        new = make_outage("2", date="2025.12.21", title="新しい障害", area="")
        _write_state(state_file, [known], notified=[""])
        recovered = replace(known, status="復旧")
        fetch.return_value = [new, recovered]
        archiver.archive.side_effect = _archived

        manager = mocker.Mock()
        manager.attach_mock(
            mocker.patch("src.main.XNotifier.notify_new_outage", return_value=True),
            "notify_new",
        )
        manager.attach_mock(
            mocker.patch("src.main.XNotifier.notify_status_change", return_value=True),
            "notify_change",
        )
        manager.attach_mock(archiver.archive, "archive")

        assert main() == 0

        # 通知 2 件がすべて終わってから魚拓を取り始め、順序は新規 → ステータス変更
        assert [c[0] for c in manager.mock_calls] == [
            "notify_new",
            "notify_change",
            "archive",
            "archive",
        ]
        assert manager.archive.call_args_list == [
            mocker.call(new.url, deadline=mocker.ANY),
            mocker.call(known.url, deadline=mocker.ANY),
        ]

        state = _read_state(state_file)
        new_archives = state["outages"][new.id]["archives"]
        assert len(new_archives) == 1
        assert new_archives[0]["status"] == ""
        assert new_archives[0]["url"] == _archived(new.url)
        assert new_archives[0]["archived_at"]
        known_archives = state["outages"][known.id]["archives"]
        assert len(known_archives) == 1
        assert known_archives[0]["status"] == "復旧"
        assert known_archives[0]["url"] == _archived(known.url)
        # 魚拓は通知済みマークとカウンタに影響しない
        assert state["outages"][new.id]["notified_statuses"] == [""]
        assert state["outages"][known.id]["notified_statuses"] == ["", "復旧"]
        assert state["stats"]["total_notifications_this_month"] == 2

    def test_archives_even_when_notification_is_skipped(
        self, mocker, state_file, fetch, archiver, sample_outage
    ):
        """投稿制限で通知がスキップされても、魚拓は取得して記録されること"""
        fetch.return_value = [sample_outage]
        mocker.patch("src.main.can_send_notification", return_value=False)
        notify = mocker.patch("src.main.XNotifier.notify_new_outage")
        archiver.archive.side_effect = _archived

        assert main() == 0

        notify.assert_not_called()
        archiver.archive.assert_called_once_with(sample_outage.url, deadline=mocker.ANY)
        stored = _read_state(state_file)["outages"][sample_outage.id]
        assert stored["notified_statuses"] == []
        assert [a["url"] for a in stored["archives"]] == [_archived(sample_outage.url)]

    def test_failed_notification_is_not_archived(
        self, mocker, state_file, fetch, allow_sending, archiver, make_outage
    ):
        """通知が失敗した障害は魚拓を取らず、巻き戻し後の状態にも記録が無いこと"""
        ok = make_outage("1", title="成功する障害", area="")
        ng = make_outage("2", date="2025.12.21", title="失敗する障害", area="")
        fetch.return_value = [ok, ng]
        mocker.patch(
            "src.main.XNotifier.notify_new_outage",
            side_effect=lambda outage: outage.id == ok.id,
        )
        archiver.archive.side_effect = _archived

        assert main() == 1

        archiver.archive.assert_called_once_with(ok.url, deadline=mocker.ANY)
        state = _read_state(state_file)
        assert [a["url"] for a in state["outages"][ok.id]["archives"]] == [
            _archived(ok.url)
        ]
        assert ng.id not in state["outages"]

    def test_archive_exception_does_not_affect_exit_code_or_state(
        self, mocker, state_file, fetch, allow_sending, archiver, sample_outage, caplog
    ):
        """魚拓が例外を投げても終了コード 0 で状態が保存され、通知済みマークが残ること"""
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [sample_outage]
        archiver.archive.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.INFO):
            assert main() == 0

        stored = _read_state(state_file)["outages"][sample_outage.id]
        assert stored["notified_statuses"] == [""]
        assert stored["archives"] == []
        assert _read_state(state_file)["stats"]["total_notifications_this_month"] == 1
        assert any(
            r.levelno == logging.ERROR and "boom" in (r.exc_text or "")
            for r in caplog.records
        )

    def test_archive_failure_is_summarized_as_warning(
        self, mocker, state_file, fetch, allow_sending, archiver, sample_outage, caplog
    ):
        """魚拓が None（失敗）でも記録が増えず終了コード 0 で、サマリが WARNING に出ること"""
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [sample_outage]
        archiver.archive.side_effect = None
        archiver.archive.return_value = None

        with caplog.at_level(logging.INFO):
            assert main() == 0

        assert _read_state(state_file)["outages"][sample_outage.id]["archives"] == []
        summaries = [r for r in caplog.records if r.getMessage().startswith("魚拓を")]
        assert [(r.levelno, r.getMessage()) for r in summaries] == [
            (logging.WARNING, "魚拓を 0 件取得、1 件失敗")
        ]

    def test_success_is_logged_and_summarized_as_info(
        self, mocker, state_file, fetch, allow_sending, archiver, sample_outage, caplog
    ):
        """魚拓に成功した件は説明と URL を INFO に出し、失敗が無ければサマリも INFO であること"""
        mocker.patch("src.notifier.DRY_RUN", True)
        fetch.return_value = [sample_outage]
        archiver.archive.side_effect = _archived

        with caplog.at_level(logging.INFO):
            assert main() == 0

        messages = [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("魚拓を")
        ]
        assert messages == [
            (
                logging.INFO,
                f"魚拓を取得しました: {sample_outage.title} -> "
                f"{_archived(sample_outage.url)}",
            ),
            (logging.INFO, "魚拓を 1 件取得、0 件失敗"),
        ]

    def test_missing_credentials_warn_once_and_skip(
        self, mocker, state_file, fetch, allow_sending, sample_outage, caplog
    ):
        """認証情報なし（本物の archiver）では警告 1 回・終了コード 0 で、
        サマリも記録も出ないこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        # autouse のモックより後に本物のクラスに戻す（conftest が認証情報を削除済み）
        mocker.patch("src.main.WaybackArchiver", WaybackArchiver)
        fetch.return_value = [sample_outage]

        with caplog.at_level(logging.INFO):
            assert main() == 0

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1
        assert "認証情報が未設定" in warnings[0]
        assert not any(r.getMessage().startswith("魚拓を") for r in caplog.records)
        stored = _read_state(state_file)["outages"][sample_outage.id]
        assert stored["notified_statuses"] == [""]
        assert stored["archives"] == []

    def test_dry_run_skips_archiving_without_warning(
        self,
        mocker,
        state_file,
        fetch,
        allow_sending,
        archiver_class,
        sample_outage,
        caplog,
    ):
        """DRY RUN では archiver を生成せず（要求を送らず）、対象 URL を INFO に出し、
        失敗として数えず WARNING のサマリも出さないこと"""
        mocker.patch("src.notifier.DRY_RUN", True)
        mocker.patch("src.main.DRY_RUN", True)
        fetch.return_value = [sample_outage]

        with caplog.at_level(logging.INFO):
            assert main() == 0

        archiver_class.assert_not_called()
        assert [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ] == []
        assert not any(r.getMessage().startswith("魚拓を") for r in caplog.records)
        main_messages = [r.getMessage() for r in caplog.records if r.name == "src.main"]
        assert f"[DRY RUN] 魚拓の取得をスキップ: {sample_outage.url}" in main_messages
        assert "[DRY RUN] 魚拓の取得を 1 件スキップしました" in main_messages
        state = _read_state(state_file)
        stored = state["outages"][sample_outage.id]
        assert stored["archives"] == []
        assert stored["notified_statuses"] == [""]
        assert state["stats"]["total_notifications_this_month"] == 1

    def test_state_is_saved_before_archiving(
        self, mocker, state_file, fetch, allow_sending, archiver, sample_outage
    ):
        """魚拓を取る前に通知済みの状態を保存しておくこと

        魚拓は 1 件あたり最大数分かかる。その間にランナーが落ちても、投稿済みの
        マークとカウンタが保存されていれば次回二重投稿にならない。
        """
        fetch.return_value = [sample_outage]
        mocker.patch("src.main.XNotifier._post_tweet", return_value=True)
        seen: dict[str, dict] = {}

        def _archive_and_peek(url: str, deadline: float | None = None) -> str:
            seen["state"] = _read_state(state_file)
            return _archived(url)

        archiver.archive.side_effect = _archive_and_peek

        assert main() == 0

        stored_before = seen["state"]["outages"][sample_outage.id]
        assert stored_before["notified_statuses"] == [""]
        assert stored_before["archives"] == []
        assert seen["state"]["stats"]["total_notifications_this_month"] == 1
        stored_after = _read_state(state_file)["outages"][sample_outage.id]
        assert stored_after["archives"][0]["url"] == _archived(sample_outage.url)

    def test_run_budget_stops_remaining_targets(
        self,
        mocker,
        state_file,
        fetch,
        allow_sending,
        archiver,
        make_outage,
        fake_clock,
        caplog,
    ):
        """魚拓の時間予算を超えたら残りの件を諦め、失敗として数えること

        期限は archiver にも渡す（処理中の件が予算を超えて待たないようにするため）。
        """
        mocker.patch("src.notifier.DRY_RUN", True)
        first, second = make_outage("1"), make_outage("2")
        fetch.return_value = [first, second]
        started_at = fake_clock.now

        def _slow_archive(url: str, deadline: float | None = None) -> str:
            # 1 件目が予算いっぱいまでかかった
            fake_clock.advance(WAYBACK_RUN_BUDGET + 1)
            return _archived(url)

        archiver.archive.side_effect = _slow_archive

        with caplog.at_level(logging.INFO):
            assert main() == 0

        archiver.archive.assert_called_once_with(
            first.url, deadline=started_at + WAYBACK_RUN_BUDGET
        )
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("時間予算" in w and "残り 1 件" in w for w in warnings)
        assert "魚拓を 1 件取得、1 件失敗" in warnings
        state = _read_state(state_file)
        assert state["outages"]["1"]["archives"][0]["url"] == _archived(first.url)
        assert state["outages"]["2"]["archives"] == []
        assert state["outages"]["2"]["notified_statuses"] == [""]

    def test_tweet_text_never_contains_archive_url(
        self, mocker, state_file, fetch, allow_sending, archiver, sample_outage
    ):
        """魚拓の URL はツイート本文に含めないこと"""
        fetch.return_value = [sample_outage]
        post = mocker.patch("src.main.XNotifier._post_tweet", return_value=True)
        archiver.archive.side_effect = _archived

        assert main() == 0

        post.assert_called_once()
        message = post.call_args.args[0]
        assert sample_outage.url in message
        assert "web.archive.org" not in message

    def test_archiver_is_not_created_without_changes(
        self, mocker, fetch, archiver_class, sample_outage
    ):
        """変更が無い実行では archiver を生成しないこと（未設定の警告を毎回出さないため）"""
        fetch.return_value = [sample_outage]
        mocker.patch(
            "src.main.StateManager.get_changes",
            return_value=ChangeResult(new_outages=[], status_changes=[]),
        )

        assert main() == 0

        archiver_class.assert_not_called()

    def test_archiver_is_not_created_when_all_notifications_fail(
        self, mocker, fetch, allow_sending, archiver_class, sample_outage
    ):
        """全件が通知失敗の実行では魚拓の対象が無いので archiver を生成しないこと"""
        fetch.return_value = [sample_outage]
        mocker.patch("src.main.XNotifier.notify_new_outage", return_value=False)

        assert main() == 1

        archiver_class.assert_not_called()
