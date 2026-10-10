"""メイン処理モジュール"""

import logging
import sys
from collections.abc import Callable
from functools import partial
from typing import Literal

from .config import LOG_LEVEL, STATE_FILE_PATH
from .notifier import XNotifier, can_send_notification, should_notify_change
from .scraper import (
    ToshimaScraper,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)
from .state_manager import StateFileError, StateManager

# 終了コード
# GitHub Actions 側でこの値を見て扱いを変える（check-outage.yml を参照）。
EXIT_SUCCESS = 0
EXIT_FAILURE = (
    1  # 通知失敗・予期しない例外など、気づくべき失敗（状態は保存済みの場合がある）
)
EXIT_UPSTREAM_UNAVAILABLE = 2  # 上流サイトに到達できない（一時的、次回実行で再試行）

# ロギング設定
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


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
    # 月間上限はループ前にも見ているが、1 回の実行で複数件送るとその間にカウンタが
    # 進むため、1 件ごとに再確認して上限を超えないようにする。
    if not can_send_notification(state_manager):
        return "skipped"
    if not should_notify_change(state_manager, change_type):
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

        # 4. 投稿制限チェック
        if not can_send_notification(state_manager):
            logger.warning("月間投稿制限のため通知をスキップします")
            state_manager.update_outages(outages)
            state_manager.save_state()
            return EXIT_SUCCESS

        # 5. 状態を先に更新する
        # mark_notified() は state に存在する障害しかマークしないため、
        # 通知ループの前に新規障害を state へ登録しておく必要がある。
        # （これより前に呼ぶと新規障害の notified_statuses が常に空のままになる）
        # 通知に失敗した障害だけを後で巻き戻せるよう、更新前の状態を控えておく。
        snapshot = state_manager.snapshot_outages()
        state_manager.update_outages(outages)

        # 6. 通知送信
        logger.info("通知を送信しています...")
        notifier = XNotifier()
        notification_sent = False
        skipped_count = 0
        failed_outage_ids: list[str] = []

        # 新規障害の通知
        for outage in changes.new_outages:
            # partial で投稿関数を束縛（ラムダの遅延束縛を避ける）
            result = _process_notification(
                state_manager,
                "new",
                partial(notifier.notify_new_outage, outage),
                outage.id,
                outage.status,
            )
            if result == "sent":
                notification_sent = True
                logger.info(f"新規障害を通知しました: {outage.title}")
            elif result == "failed":
                failed_outage_ids.append(outage.id)
                logger.error(f"新規障害の通知に失敗しました: {outage.title}")
            else:
                skipped_count += 1

        # ステータス変更の通知
        for change in changes.status_changes:
            # partial で投稿関数を束縛（ラムダの遅延束縛を避ける）
            result = _process_notification(
                state_manager,
                "status_change",
                partial(notifier.notify_status_change, change),
                change.outage.id,
                change.new_status,
            )
            if result == "sent":
                notification_sent = True
                logger.info(
                    f"ステータス変更を通知しました: {change.outage.title} "
                    f"({change.old_status or '進行中'} -> {change.new_status or '進行中'})"
                )
            elif result == "failed":
                failed_outage_ids.append(change.outage.id)
                logger.error(
                    f"ステータス変更の通知に失敗しました: {change.outage.title} "
                    f"({change.old_status or '進行中'} -> {change.new_status or '進行中'})"
                )
            else:
                skipped_count += 1

        if skipped_count:
            # 月間上限や 90% 到達時の絞り込みで意図的に送らなかった件数をまとめて警告する
            logger.warning(
                f"{skipped_count} 件の通知を投稿制限のためスキップしました"
                f"（今月 {state_manager.get_notification_count_this_month()} 件送信済み）"
            )

        # 7. 通知に失敗した障害だけを更新前の状態に巻き戻す
        # 失敗分を state から外して保存することで、次回実行で再度「新規」または
        # 「ステータス変更」として検出されリトライされる。成功分のマークとカウンタは
        # 保存するので、同じ実行内で成功した通知が次回二重に投稿されることはない。
        if failed_outage_ids:
            state_manager.rollback_outages(failed_outage_ids, snapshot)

        # 8. 状態保存
        logger.info("状態を保存しています...")
        saved = state_manager.save_state()

        if saved:
            logger.info("状態ファイルを保存しました")
        else:
            logger.info("状態に変更がないため保存をスキップしました")

        # 9. 通知失敗があればエラー終了する
        # GitHub Actions のジョブが失敗するため、サイレントな取りこぼしを防ぐ。
        # 状態はすでに保存済みで、ワークフローは終了コード 1 でもコミットする。
        if failed_outage_ids:
            logger.error(
                f"{len(failed_outage_ids)} 件の通知に失敗しました（次回実行で再試行）"
            )
            return EXIT_FAILURE

        if notification_sent:
            logger.info("通知処理が完了しました")
        else:
            logger.info("通知は送信されませんでした（条件未達成）")

        return EXIT_SUCCESS

    except Exception as e:
        logger.exception(f"予期しないエラーが発生しました: {e}")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
