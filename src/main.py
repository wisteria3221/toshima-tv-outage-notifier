"""メイン処理モジュール"""

import logging
import sys

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
            # 状態を更新して保存（last_check更新のため）
            state_manager.update_outages(outages)
            state_manager.save_state()
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

        # 5. 通知送信
        logger.info("通知を送信しています...")
        notifier = XNotifier()
        notification_sent = False

        # 新規障害の通知
        for outage in changes.new_outages:
            if should_notify_change(state_manager, "new"):
                if notifier.notify_new_outage(outage):
                    state_manager.mark_notified(outage.id, outage.status)
                    state_manager.increment_notification_count()
                    notification_sent = True
                    logger.info(f"新規障害を通知しました: {outage.title}")

        # ステータス変更の通知
        for change in changes.status_changes:
            if should_notify_change(state_manager, "status_change"):
                if notifier.notify_status_change(change):
                    state_manager.mark_notified(change.outage.id, change.new_status)
                    state_manager.increment_notification_count()
                    notification_sent = True
                    logger.info(
                        f"ステータス変更を通知しました: {change.outage.title} "
                        f"({change.old_status or '進行中'} -> {change.new_status or '進行中'})"
                    )

        # 6. 状態保存
        logger.info("状態を保存しています...")
        state_manager.update_outages(outages)
        state_manager.save_state()

        if notification_sent:
            logger.info("通知処理が完了しました")
        else:
            logger.info("通知は送信されませんでした（制限または条件未達成）")

        return 0

    except Exception as e:
        logger.exception(f"予期しないエラーが発生しました: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
