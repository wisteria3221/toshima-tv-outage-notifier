"""メイン処理モジュール"""

import logging
import sys
from collections.abc import Callable
from functools import partial
from typing import Literal

from dotenv import load_dotenv

from .config import LOG_LEVEL, STATE_FILE_PATH
from .notifier import XNotifier, can_send_notification, should_notify_change
from .scraper import ToshimaScraper
from .state_manager import StateManager

# 環境変数の読み込み（.envファイルがあれば）
load_dotenv()

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
    # should_notify_change が False の場合は意図的なスキップ（"skipped"）、
    # 投稿が失敗した場合は "failed" として呼び出し側でエラー扱いする。
    if not should_notify_change(state_manager, change_type):
        return "skipped"
    if not notify():
        return "failed"
    state_manager.mark_notified(outage_id, status)
    state_manager.increment_notification_count()
    return "sent"


def main() -> int:
    """メイン処理

    Returns:
        終了コード（0: 成功、1: エラー）
    """
    logger.info("としまテレビ障害情報チェックを開始します")

    try:
        # 1. 状態ファイル読み込み
        logger.info("状態ファイルを読み込んでいます...")
        state_manager = StateManager(STATE_FILE_PATH)

        # 2. 障害情報をスクレイピング
        logger.info("障害情報を取得しています...")
        scraper = ToshimaScraper()
        outages = scraper.fetch_outage_list(max_pages=1)

        if not outages:
            logger.warning("障害情報を取得できませんでした")
            return 1

        logger.info(f"{len(outages)} 件の障害情報を取得しました")

        # 3. 差分検出
        logger.info("差分を検出しています...")
        changes = state_manager.get_changes(outages)

        if not changes.has_changes():
            logger.info("新しい障害やステータス変更はありませんでした")
            # 変更がない場合は状態更新も保存もスキップ
            return 0

        logger.info(
            f"変更を検出: 新規障害 {len(changes.new_outages)} 件、"
            f"ステータス変更 {len(changes.status_changes)} 件"
        )

        # 4. 投稿制限チェック
        if not can_send_notification(state_manager):
            logger.warning("月間投稿制限のため通知をスキップします")
            state_manager.update_outages(outages)
            state_manager.save_state()
            return 0

        # 5. 状態を先に更新する
        # mark_notified() は state に存在する障害しかマークしないため、
        # 通知ループの前に新規障害を state へ登録しておく必要がある。
        # （これより前に呼ぶと新規障害の notified_statuses が常に空のままになる）
        state_manager.update_outages(outages)

        # 6. 通知送信
        logger.info("通知を送信しています...")
        notifier = XNotifier()
        notification_sent = False
        notification_failed = False

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
                notification_failed = True
                logger.error(f"新規障害の通知に失敗しました: {outage.title}")

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
                notification_failed = True
                logger.error(
                    f"ステータス変更の通知に失敗しました: {change.outage.title} "
                    f"({change.old_status or '進行中'} -> {change.new_status or '進行中'})"
                )

        # 7. 通知失敗時はエラー終了する
        # 状態を保存・コミットせずに exit 1 することで、未通知の障害が次回実行で
        # 再度「新規」または「ステータス変更」として検出されリトライされる。
        # 同時に GitHub Actions のジョブが失敗するため、サイレントな取りこぼしを防ぐ。
        if notification_failed:
            logger.error("通知送信に失敗したため状態を保存せず終了します")
            return 1

        # 8. 状態保存
        logger.info("状態を保存しています...")
        saved = state_manager.save_state()

        if saved:
            logger.info("状態ファイルを保存しました")
        else:
            logger.info("状態に変更がないため保存をスキップしました")

        if notification_sent:
            logger.info("通知処理が完了しました")
        else:
            logger.info("通知は送信されませんでした（条件未達成）")

        return 0

    except Exception as e:
        logger.exception(f"予期しないエラーが発生しました: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
