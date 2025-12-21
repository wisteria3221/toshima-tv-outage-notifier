"""X（Twitter）通知モジュール"""

import logging

import tweepy

from .config import (
    DRY_RUN,
    MONTHLY_TWEET_LIMIT,
    get_x_credentials,
    validate_x_credentials,
)
from .scraper import OutageInfo
from .state_manager import StateManager, StatusChange

logger = logging.getLogger(__name__)


class XNotifier:
    """X（Twitter）通知クラス"""

    MAX_TWEET_LENGTH = 280

    def __init__(self):
        """初期化"""
        self.client: tweepy.Client | None = None

        if not DRY_RUN:
            self.client = self._create_client()

    def _create_client(self) -> tweepy.Client | None:
        """Tweepy Clientを作成

        Returns:
            tweepy.Client または None（認証情報不足時）
        """
        if not validate_x_credentials():
            logger.error("X API認証情報が設定されていません")
            return None

        creds = get_x_credentials()

        try:
            client = tweepy.Client(
                consumer_key=creds["consumer_key"],
                consumer_secret=creds["consumer_secret"],
                access_token=creds["access_token"],
                access_token_secret=creds["access_token_secret"],
            )
            logger.info("X APIクライアントを初期化しました")
            return client

        except Exception as e:
            logger.error(f"X APIクライアントの初期化に失敗: {e}")
            return None

    def notify_new_outage(self, outage: OutageInfo) -> bool:
        """新規障害を通知

        Args:
            outage: 障害情報

        Returns:
            投稿成功時True
        """
        message = self._format_new_outage_message(outage)
        return self._post_tweet(message)

    def notify_status_change(self, change: StatusChange) -> bool:
        """ステータス変更を通知

        Args:
            change: ステータス変更情報

        Returns:
            投稿成功時True
        """
        message = self._format_status_change_message(change)
        return self._post_tweet(message)

    def _format_new_outage_message(self, outage: OutageInfo) -> str:
        """新規障害用メッセージをフォーマット

        Args:
            outage: 障害情報

        Returns:
            フォーマットされたメッセージ
        """
        lines = [
            "【としまテレビ 障害情報】",
            outage.title,
        ]

        if outage.date:
            lines.append(f"日時: {outage.date}")

        if outage.area:
            lines.append(f"地域: {outage.area}")

        lines.append(f"詳細: {outage.url}")

        message = "\n".join(lines)
        return self._truncate_message(message)

    def _format_status_change_message(self, change: StatusChange) -> str:
        """ステータス変更用メッセージをフォーマット

        Args:
            change: ステータス変更情報

        Returns:
            フォーマットされたメッセージ
        """
        outage = change.outage
        new_status = change.new_status or "進行中"

        # ステータスに応じてヘッダーを変更
        if new_status in ["復旧", "終了", "完了"]:
            header = f"【としまテレビ {new_status}情報】"
            status_text = f"{outage.title} が{new_status}しました"
        else:
            header = "【としまテレビ 障害情報更新】"
            status_text = f"{outage.title}（{new_status}）"

        lines = [
            header,
            status_text,
        ]

        if outage.area:
            lines.append(f"地域: {outage.area}")

        lines.append(f"詳細: {outage.url}")

        message = "\n".join(lines)
        return self._truncate_message(message)

    def _truncate_message(self, message: str) -> str:
        """メッセージを最大文字数に切り詰め

        Args:
            message: 元のメッセージ

        Returns:
            切り詰められたメッセージ
        """
        if len(message) <= self.MAX_TWEET_LENGTH:
            return message

        # URLを保持しつつ切り詰め
        truncated = message[: self.MAX_TWEET_LENGTH - 3] + "..."
        logger.warning(
            f"メッセージを切り詰めました: {len(message)} -> {len(truncated)}文字"
        )
        return truncated

    def _post_tweet(self, message: str) -> bool:
        """ツイートを投稿

        Args:
            message: 投稿するメッセージ

        Returns:
            投稿成功時True
        """
        if DRY_RUN:
            logger.info(f"[DRY RUN] ツイートをスキップ:\n{message}")
            return True

        if self.client is None:
            logger.error("X APIクライアントが初期化されていません")
            return False

        try:
            response = self.client.create_tweet(text=message)
            tweet_id = response.data.get("id") if response.data else "unknown"
            logger.info(f"ツイートを投稿しました: ID={tweet_id}")
            return True

        except tweepy.TweepyException as e:
            logger.error(f"ツイート投稿に失敗: {e}")
            return False


def can_send_notification(state_manager: StateManager) -> bool:
    """通知を送信可能かチェック（月間制限）

    Args:
        state_manager: 状態管理オブジェクト

    Returns:
        送信可能ならTrue
    """
    count = state_manager.get_notification_count_this_month()

    if count >= MONTHLY_TWEET_LIMIT:
        logger.warning(f"月間投稿制限に達しました: {count}/{MONTHLY_TWEET_LIMIT}")
        return False

    remaining = MONTHLY_TWEET_LIMIT - count
    logger.debug(f"今月の残り投稿可能数: {remaining}")
    return True


def should_notify_change(
    state_manager: StateManager,
    change_type: str,
) -> bool:
    """変更を通知すべきかどうか判定

    制限に近づいている場合は重要な通知のみに絞る

    Args:
        state_manager: 状態管理オブジェクト
        change_type: 変更タイプ（"new" または "status_change"）

    Returns:
        通知すべきならTrue
    """
    count = state_manager.get_notification_count_this_month()

    # 96%以上使用: 新規障害のみ
    if count >= int(MONTHLY_TWEET_LIMIT * 0.96):
        return change_type == "new"

    # 90%以上使用: 新規障害と重要なステータス変更
    if count >= int(MONTHLY_TWEET_LIMIT * 0.90):
        return change_type in ["new"]

    return True
