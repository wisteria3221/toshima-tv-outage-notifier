"""メイン処理モジュール"""

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

# テストで偽の時計に差し替えられるよう、このモジュールの名前で参照する（archiver と同じ）
from time import monotonic
from typing import Literal

from .archiver import WaybackArchiver
from .config import DRY_RUN, LOG_LEVEL, STATE_FILE_PATH, WAYBACK_RUN_BUDGET
from .notifier import XNotifier, can_send_notification, should_notify_change
from .scraper import (
    ToshimaScraper,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from .state_manager import ChangeResult, StateFileError, StateManager

# 終了コード
# GitHub Actions 側でこの値を見て扱いを変える（check-outage.yml を参照）。
EXIT_SUCCESS = 0
# 通知失敗・予期しない例外など、気づくべき失敗（状態は保存済みの場合がある）
EXIT_FAILURE = 1
# 上流サイトに到達できない（一時的、次回実行で再試行）
EXIT_UPSTREAM_UNAVAILABLE = 2

# ロギング設定
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@dataclass
class _PendingNotification:
    """通知ループで 1 件ずつ処理する通知の作業単位

    新規障害とステータス変更は投稿関数と通知済みマークの引数が異なるだけなので、
    ここで同じ形に揃えてループを 1 本にする。
    """

    change_type: str  # "new" または "status_change"
    notify: Callable[[], bool]  # 引数なしで呼べる投稿関数（partial で束縛済み）
    outage_id: str  # 通知済みマーク対象の障害ID
    status: str  # 通知済みマーク対象のステータス
    url: str  # 魚拓の対象（詳細ページ URL。通知本文の「詳細:」行と同じ）
    label: str  # ログ用の種別名（「新規障害」「ステータス変更」）
    description: str  # ログ用の対象の説明（タイトル、旧→新ステータスなど）


def _pending_notifications(
    changes: ChangeResult, notifier: XNotifier
) -> list[_PendingNotification]:
    """差分検出結果を通知の作業単位に揃える（新規障害 → ステータス変更の順）"""
    # partial で投稿関数を束縛（ラムダの遅延束縛を避ける）
    pending = [
        _PendingNotification(
            change_type="new",
            notify=partial(notifier.notify_new_outage, outage),
            outage_id=outage.id,
            status=outage.status,
            url=outage.url,
            label="新規障害",
            description=outage.title,
        )
        for outage in changes.new_outages
    ]
    pending.extend(
        _PendingNotification(
            change_type="status_change",
            notify=partial(notifier.notify_status_change, change),
            outage_id=change.outage.id,
            status=change.new_status,
            url=change.outage.url,
            label="ステータス変更",
            description=f"{change.outage.title} ({change.describe()})",
        )
        for change in changes.status_changes
    )
    return pending


def _process_notification(
    state_manager: StateManager,
    change_type: str,
    notify: Callable[[], bool],
    outage_id: str,
    status: str,
) -> Literal["sent", "skipped", "failed"]:
    """通知可否を判定し、許可された場合のみ投稿・マーク・カウンタ加算を行う。

    Args:
        state_manager: 状態マネージャ
        change_type: 変更種別（"new" または "status_change"）
        notify: 引数なしで呼べる投稿関数。成功時 True / 失敗時 False を返す
        outage_id: 通知済みマーク対象の障害ID
        status: 通知済みマーク対象のステータス

    Returns:
        "sent": 実際に投稿した
        "skipped": レート制限等で意図的に送信しなかった（失敗ではない）
        "failed": 投稿を試みたが失敗した（リトライ対象）
    """
    # 通知可否判定 → 投稿 → 通知済みマーク → カウンタ加算 の順序を保つ。
    # can_send_notification / should_notify_change が False の場合は意図的なスキップ
    # （"skipped"）、投稿が失敗した場合は "failed" として呼び出し側でエラー扱いする。
    # 1 回の実行で複数件送るとその間にカウンタが進むため、月間上限は 1 件ごとに
    # 判定して上限を超えないようにする。
    if not can_send_notification(state_manager) or not should_notify_change(
        state_manager, change_type
    ):
        return "skipped"
    # tweepy は接続エラーやタイムアウトを TweepyException に包まず requests の例外のまま
    # 投げる。ここで捕捉せずに main() の外側まで抜けると状態が保存されず、同じ実行で
    # 成功した通知のマークも失われて次回二重投稿になるため、"failed" として扱う。
    try:
        sent = notify()
    except Exception:
        logger.exception("投稿処理で予期しない例外が発生しました")
        sent = False
    if not sent:
        return "failed"
    state_manager.mark_notified(outage_id, status)
    state_manager.increment_notification_count()
    return "sent"


def _archive_outages(
    state_manager: StateManager, targets: list[_PendingNotification]
) -> tuple[int, int]:
    """対象の詳細ページの魚拓を順に取り、成功した件だけ状態に記録する

    魚拓はベストエフォート。archiver の生成から各件の取得までを例外捕捉で包み、
    予期しない例外もログに残して次の件へ進む（通知の成否・終了コード・状態保存に
    影響させない）。失敗した件は記録せず、以降の実行でも再試行しない。
    1 回の実行で費やす時間は WAYBACK_RUN_BUDGET 秒までとし、期限は archiver にも渡して
    処理中の件もその時点で打ち切る。期限を過ぎたら残りの件は失敗として数えて諦める
    （対象が多い実行で Healthchecks の猶予を超えないため）。

    Args:
        state_manager: 状態マネージャ（成功した件の記録先）
        targets: 通知結果が「送信」または「スキップ」だった作業単位

    Returns:
        (成功件数, 失敗件数)。対象が無い・DRY RUN・archiver が無効（認証情報なし）なら (0, 0)
    """
    if not targets:
        # 対象が無ければ archiver を生成しない（認証情報なしの警告を毎回出さないため）
        return 0, 0
    if DRY_RUN:
        # DRY RUN では archiver を生成せず（要求を送らず）、対象 URL だけログに出す。
        # 試行 0 件として扱い、WARNING の件数サマリは出さない。認証情報の有無より先に
        # 判定するので、キーを持たないローカル環境でも DRY RUN のログは同じになる
        for item in targets:
            logger.info(f"[DRY RUN] 魚拓の取得をスキップ: {item.url}")
        logger.info(f"[DRY RUN] 魚拓の取得を {len(targets)} 件スキップしました")
        return 0, 0

    success_count = 0
    failure_count = 0
    archiver: WaybackArchiver | None = None
    try:
        with WaybackArchiver() as archiver:
            if not archiver.enabled:
                # 認証情報なしの警告は生成時に 1 回出ている。試行 0 件として扱い、
                # 件数サマリは出さない
                return 0, 0
            deadline = monotonic() + WAYBACK_RUN_BUDGET
            for index, item in enumerate(targets):
                if monotonic() >= deadline:
                    remaining = len(targets) - index
                    logger.warning(
                        f"魚拓の時間予算（{WAYBACK_RUN_BUDGET} 秒）を超えたため、"
                        f"残り {remaining} 件の取得を諦めます"
                    )
                    failure_count += remaining
                    break
                try:
                    # 実行全体の期限も渡し、処理中の件がこの期限を超えて待たないようにする
                    archived_url = archiver.archive(item.url, deadline=deadline)
                except Exception:
                    # archive() は例外を出さない契約だが、万一抜けてきても
                    # この件だけ失敗にして次の件へ進む
                    logger.exception(
                        f"魚拓の取得で予期しない例外が発生しました: {item.description}"
                    )
                    archived_url = None
                if archived_url is None:
                    # 失敗の理由は archiver 側が WARNING に出している
                    failure_count += 1
                    continue
                state_manager.record_archive(item.outage_id, item.status, archived_url)
                success_count += 1
                logger.info(f"魚拓を取得しました: {item.description} -> {archived_url}")
    except Exception:
        # archiver の生成やセッションの後始末で例外が出た場合
        logger.exception("魚拓の処理で予期しない例外が発生しました")
        if archiver is not None and not archiver.enabled:
            # 無効な archiver は要求を送っていないので、後始末の例外でも試行 0 件のまま
            return 0, 0
        # まだ成功していない件をすべて失敗として数える
        failure_count = len(targets) - success_count
    return success_count, failure_count


def main() -> int:
    """メイン処理

    Returns:
        終了コード
        - EXIT_SUCCESS (0): 成功
        - EXIT_FAILURE (1): 通知失敗・予期しない例外
        - EXIT_UPSTREAM_UNAVAILABLE (2): 障害情報ページに到達できない（一時的な要因）
    """
    logger.info("としまテレビ障害情報チェックを開始します")

    try:
        # 1. 状態ファイル読み込み
        logger.info("状態ファイルを読み込んでいます...")
        try:
            state_manager = StateManager(STATE_FILE_PATH)
        except StateFileError as e:
            # 壊れた状態ファイルで続行すると全障害を再通知してしまう。
            # 状態は一切変更せずに失敗させ、人が確認してから直す。
            logger.error(f"{e}（状態ファイルを確認してください）")
            return EXIT_FAILURE

        # 2. 障害情報をスクレイピング
        logger.info("障害情報を取得しています...")
        try:
            with ToshimaScraper() as scraper:
                outages = scraper.fetch_outage_list(max_pages=1)
        except UpstreamUnavailableError as e:
            # 上流サイトのタイムアウトや DNS 失敗は当プログラムの不具合ではなく、
            # 状態も変えないので次回実行で自然に再試行される。
            # 通知失敗（EXIT_FAILURE）とは区別し、ワークフロー側では警告扱いにする。
            logger.warning(f"障害情報ページに到達できませんでした: {e}")
            return EXIT_UPSTREAM_UNAVAILABLE
        except UpstreamRejectedError as e:
            # 404（URL 変更）や 403（UA ブロック）は放置すると監視が止まったままになるので、
            # 一時的な不通とは区別して失敗させる。
            logger.error(f"{e}（URL 変更や User-Agent のブロックを確認してください）")
            return EXIT_FAILURE

        if not outages:
            # ページは取れたのに 1 件も解析できない = ページ構造の変更などの恒久的な問題
            logger.error("障害情報を取得できませんでした（ページ構造が変わった可能性）")
            return EXIT_FAILURE

        logger.info(f"{len(outages)} 件の障害情報を取得しました")

        # 3. 差分検出
        logger.info("差分を検出しています...")
        changes = state_manager.get_changes(outages)

        if not changes.has_changes():
            logger.info("新しい障害やステータス変更はありませんでした")
            # 変更がない場合は状態更新も保存もスキップ
            return EXIT_SUCCESS

        logger.info(
            f"変更を検出: 新規障害 {len(changes.new_outages)} 件、"
            f"ステータス変更 {len(changes.status_changes)} 件"
        )

        # 4. 状態を先に更新する
        # mark_notified() は state に存在する障害しかマークしないため、
        # 通知ループの前に新規障害を state へ登録しておく必要がある。
        # （これより前に呼ぶと新規障害の notified_statuses が常に空のままになる）
        # 通知に失敗した障害だけを後で巻き戻せるよう、更新前の状態を控えておく。
        snapshot = state_manager.snapshot_outages()
        state_manager.update_outages(outages)

        # 5. 通知送信
        # 投稿制限（月間上限・90% 到達時の絞り込み）は _process_notification() が
        # 1 件ごとに判定する。ループ前に can_send_notification() でまとめて判定する
        # 早期終了は、結果が件ごとのスキップと同じ（state 更新・保存・終了コード 0）で
        # ログだけが異なり、スキップの意味を変えるときに 2 箇所直す必要があるため置かない。
        logger.info("通知を送信しています...")
        sent_count = 0
        skipped_count = 0
        failed_outage_ids: list[str] = []
        # 魚拓の対象（通知結果が「送信」または「スキップ」の件）。ループ中は通知だけを
        # 行い、魚拓はすべての通知が終わってからまとめて取る（下記 7 を参照）
        archive_targets: list[_PendingNotification] = []

        for item in _pending_notifications(changes, XNotifier()):
            result = _process_notification(
                state_manager,
                item.change_type,
                item.notify,
                item.outage_id,
                item.status,
            )
            if result == "sent":
                sent_count += 1
                logger.info(f"{item.label}を通知しました: {item.description}")
            elif result == "failed":
                failed_outage_ids.append(item.outage_id)
                logger.error(f"{item.label}の通知に失敗しました: {item.description}")
            else:
                skipped_count += 1
            if result != "failed":
                archive_targets.append(item)

        if skipped_count:
            # 月間上限や 90% 到達時の絞り込みで意図的に送らなかった件数をまとめて警告する
            logger.warning(
                f"{skipped_count} 件の通知を投稿制限のためスキップしました"
                f"（今月 {state_manager.get_notification_count_this_month()} 件送信済み）"
            )

        # 6. 通知に失敗した障害だけを更新前の状態に巻き戻す
        # 失敗分を state から外して保存することで、次回実行で再度「新規」または
        # 「ステータス変更」として検出されリトライされる。成功分のマークとカウンタは
        # 保存するので、同じ実行内で成功した通知が次回二重に投稿されることはない。
        if failed_outage_ids:
            state_manager.rollback_outages(failed_outage_ids, snapshot)

        # 7. 通知済みの状態をここで一度保存する（保存・スキップのログは save_state() 側で出す）
        # 次の魚拓は 1 件あたり最大数分かかる。保存を魚拓の後だけにすると、その間に
        # ランナーが落ちたとき投稿済みのマークとカウンタが失われ、次回二重投稿になる。
        logger.info("状態を保存しています...")
        state_manager.save_state()

        # 8. 詳細ページの魚拓を取る
        # 順序は「通知ループ → 巻き戻し → 保存 → 魚拓 → 保存」。
        # - 通知の後に回すのは速報性のため: 魚拓は 1 件あたり最大 WAYBACK_TIMEOUT 秒
        #   かかるので、通知の前や合間に挟むとツイートがその分遅れる
        # - 「失敗」の件を対象にしないのは、巻き戻しで state から外れて次回実行で
        #   再検出されるため（そのときに取る）
        # - 魚拓の結果は終了コード・通知済みマーク・月間カウンタ・failed_outage_ids に
        #   反映しない（ベストエフォート）
        archived_count, archive_failed_count = _archive_outages(
            state_manager, archive_targets
        )
        if archived_count or archive_failed_count:
            archive_summary = (
                f"魚拓を {archived_count} 件取得、{archive_failed_count} 件失敗"
            )
            if archive_failed_count:
                logger.warning(archive_summary)
            else:
                logger.info(archive_summary)

        # 9. 魚拓の記録を保存する（記録が無ければ dirty でないので書き込みはスキップされる）
        state_manager.save_state()

        # 10. 通知失敗があればエラー終了する
        # GitHub Actions のジョブが失敗するため、サイレントな取りこぼしを防ぐ。
        # 状態はすでに保存済みで、ワークフローは終了コード 1 でもコミットする。
        if failed_outage_ids:
            logger.error(
                f"{len(failed_outage_ids)} 件の通知に失敗しました（次回実行で再試行）"
            )
            return EXIT_FAILURE

        logger.info(
            f"通知処理が完了しました（送信 {sent_count} 件、スキップ {skipped_count} 件）"
        )
        return EXIT_SUCCESS

    except Exception as e:
        logger.exception(f"予期しないエラーが発生しました: {e}")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
