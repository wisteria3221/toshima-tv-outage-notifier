"""X（Twitter）通知モジュール"""

import logging
import re

import requests
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

_RATE_LIMIT_CRITICAL_RATIO = 0.96  # 96%: 新規障害のみ
_RATE_LIMIT_REDUCED_RATIO = 0.90  # 90%: 新規障害のみ、ステータス変更スキップ
_RESOLUTION_STATUSES = frozenset(["復旧", "終了", "完了"])

# X の文字数カウント（weighted length）
# https://developer.x.com/en/docs/counting-characters
# - 以下の範囲のコードポイントは 1 文字、それ以外（日本語・絵文字など）は 2 文字として数える
# - URL は長さにかかわらず t.co 短縮後の 23 文字として数える
# Python の len() とは一致しないため、280 文字判定にはこちらを使う。
_SINGLE_WEIGHT_RANGES = (
    (0x0000, 0x10FF),
    (0x2000, 0x200D),
    (0x2010, 0x201F),
    (0x2032, 0x2037),
)
_URL_WEIGHT = 23
_RE_URL = re.compile(r"https?://\S+")
_ELLIPSIS = "..."


def _char_weight(char: str) -> int:
    """1 文字の重み（X のカウント規則）を返す"""
    code = ord(char)
    for start, end in _SINGLE_WEIGHT_RANGES:
        if start <= code <= end:
            return 1
    return 2


def _plain_weighted_length(text: str) -> int:
    """URL を含まないテキストの重み付き文字数"""
    return sum(_char_weight(c) for c in text)


def weighted_length(text: str) -> int:
    """X の規則に従った重み付き文字数を返す

    Args:
        text: 対象テキスト（URL を含んでよい）

    Returns:
        X が投稿時に数える文字数
    """
    length = 0
    pos = 0
    for match in _RE_URL.finditer(text):
        length += _plain_weighted_length(text[pos : match.start()]) + _URL_WEIGHT
        pos = match.end()
    length += _plain_weighted_length(text[pos:])
    return length


def _cut_to_weight(text: str, budget: int) -> str:
    """重み付き文字数が budget 以下に収まるよう先頭から切り出す"""
    result: list[str] = []
    used = 0
    for char in text:
        weight = _char_weight(char)
        if used + weight > budget:
            break
        result.append(char)
        used += weight
    return "".join(result)


class XNotifier:
    """X（Twitter）通知クラス"""

    MAX_TWEET_LENGTH = 280  # 重み付き文字数（weighted_length）での上限

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

        except tweepy.TweepyException as e:
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
        # 初めて検出した時点で既にステータスが付いている障害（30 分の間に発生と復旧が
        # 済んだもの、状態ファイル初期化後の過去エントリなど）は、そのステータスを
        # 反映して通知する。進行中として通知すると、その後の復旧通知が出ないまま
        # （検出時のステータスが通知済みになるため）利用者に誤った状況が伝わる。
        header, title_line_format = self._header_and_title_format(
            status=outage.status,
            default_header="【としまテレビ 障害情報】",
        )

        fixed_lines = []
        if outage.date:
            fixed_lines.append(f"日時: {outage.date}")
        if outage.area:
            fixed_lines.append(f"地域: {outage.area}")
        fixed_lines.append(f"詳細: {outage.url}")

        return self._compose_message(
            header=header,
            title=outage.title,
            title_line_format=title_line_format,
            fixed_lines=fixed_lines,
        )

    def _format_status_change_message(self, change: StatusChange) -> str:
        """ステータス変更用メッセージをフォーマット

        Args:
            change: ステータス変更情報

        Returns:
            フォーマットされたメッセージ
        """
        outage = change.outage

        # 括弧なし（進行中）へ戻った場合もステータス変更として明示する
        header, title_line_format = self._header_and_title_format(
            status=change.new_status or "進行中",
            default_header="【としまテレビ 障害情報更新】",
        )

        fixed_lines = []
        if outage.area:
            fixed_lines.append(f"地域: {outage.area}")
        fixed_lines.append(f"詳細: {outage.url}")

        return self._compose_message(
            header=header,
            title=outage.title,
            title_line_format=title_line_format,
            fixed_lines=fixed_lines,
        )

    @staticmethod
    def _header_and_title_format(status: str, default_header: str) -> tuple[str, str]:
        """ステータスに応じたヘッダーとタイトル行の書式を返す

        Args:
            status: 障害のステータス（空文字は「ステータス表記なし」）
            default_header: 解決系ステータス以外で使うヘッダー

        Returns:
            (ヘッダー, タイトル行の書式) のタプル。書式は ``{title}`` を含む
        """
        if status in _RESOLUTION_STATUSES:
            return f"【としまテレビ {status}情報】", f"{{title}} が{status}しました"
        if status:
            return default_header, f"{{title}}（{status}）"
        return default_header, "{title}"

    def _compose_message(
        self,
        header: str,
        title: str,
        title_line_format: str,
        fixed_lines: list[str],
    ) -> str:
        """ヘッダー・タイトル行・固定行からメッセージを組み立てる

        280 文字（重み付き）を超える場合はタイトルだけを切り詰め、
        ヘッダーや末尾の「詳細: URL」行は必ず残す。

        Args:
            header: 1 行目のヘッダー
            title: 障害タイトル（切り詰め対象）
            title_line_format: タイトル行の書式。``{title}`` を含む
            fixed_lines: タイトル行の後ろに続く固定行（日時・地域・URL）

        Returns:
            280 文字以内に収まったメッセージ
        """

        def build(fitted_title: str) -> str:
            lines = [header, title_line_format.format(title=fitted_title), *fixed_lines]
            return "\n".join(lines)

        message = build(title)
        if weighted_length(message) <= self.MAX_TWEET_LENGTH:
            return message

        # タイトル以外の部分（タイトルを空にしたメッセージ）が占める文字数を差し引き、
        # タイトルに使える残りを求める
        available = self.MAX_TWEET_LENGTH - weighted_length(build(""))
        title_budget = available - _plain_weighted_length(_ELLIPSIS)
        fitted_title = _cut_to_weight(title, max(title_budget, 0)).rstrip() + _ELLIPSIS
        logger.warning(
            f"タイトルを切り詰めました: {weighted_length(title)} -> "
            f"{weighted_length(fitted_title)} 文字（重み付き）"
        )

        # 固定行だけで上限を超える異常時の最終防衛線
        return self._truncate_message(build(fitted_title))

    def _truncate_message(self, message: str) -> str:
        """メッセージを最大文字数（重み付き）に切り詰め

        通常は _compose_message がタイトルを切り詰めるため、ここに到達するのは
        固定行だけで上限を超えるような異常時のみ。

        Args:
            message: 元のメッセージ

        Returns:
            切り詰められたメッセージ
        """
        if weighted_length(message) <= self.MAX_TWEET_LENGTH:
            return message

        budget = self.MAX_TWEET_LENGTH - _plain_weighted_length(_ELLIPSIS)
        truncated = _cut_to_weight(message, budget) + _ELLIPSIS
        logger.warning(
            f"メッセージを切り詰めました: {weighted_length(message)} -> "
            f"{weighted_length(truncated)} 文字（重み付き）"
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

        except tweepy.Forbidden as e:
            # X は直近と同一本文の投稿を 403（duplicate content）で拒否する。
            # 投稿成功後に状態の push が失敗した場合などに起こり、「失敗」扱いにすると
            # 巻き戻し → 次回も同一本文で再投稿 → 再び 403 の無限ループになる。
            # 本文はすでに X 上に存在するので、通知済みとして成功扱いにする。
            if _is_duplicate_content_error(e):
                logger.warning(
                    f"同一内容の投稿が既に存在するため通知済みとして扱います: {e}"
                )
                return True
            logger.error(f"ツイート投稿に失敗: {e}")
            return False

        except (tweepy.TweepyException, requests.RequestException) as e:
            # tweepy は接続エラー等を requests の例外のまま投げるため両方を捕捉する
            logger.error(f"ツイート投稿に失敗: {e}")
            return False


def _is_duplicate_content_error(error: tweepy.Forbidden) -> bool:
    """403 が「同一内容の投稿」による拒否かどうかを判定する

    X API v2 は detail に "duplicate content" を含むメッセージを返す。
    tweepy の例外は str() で API のメッセージを含むため、文字列で判定する。

    Args:
        error: tweepy が送出した Forbidden 例外

    Returns:
        同一内容による拒否なら True
    """
    return "duplicate" in str(error).lower()


def can_send_notification(state_manager: StateManager) -> bool:
    """通知を送信可能かチェック（月間制限）

    Args:
        state_manager: 状態管理オブジェクト

    Returns:
        送信可能ならTrue
    """
    count = state_manager.get_notification_count_this_month()

    if count >= MONTHLY_TWEET_LIMIT:
        # 1 件ごとに呼ばれるため、ここでは INFO に留め、呼び出し側でまとめて警告する
        logger.info(f"月間投稿制限に達しました: {count}/{MONTHLY_TWEET_LIMIT}")
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
    if count >= int(MONTHLY_TWEET_LIMIT * _RATE_LIMIT_CRITICAL_RATIO):
        return change_type == "new"

    # 90%以上使用: 新規障害のみ（ステータス変更はスキップ）
    if count >= int(MONTHLY_TWEET_LIMIT * _RATE_LIMIT_REDUCED_RATIO):
        return change_type == "new"

    return True
