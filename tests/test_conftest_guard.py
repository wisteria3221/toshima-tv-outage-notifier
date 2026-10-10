"""テスト共通ガードのテスト"""

import pytest
import tweepy

from src.notifier import XNotifier


class TestNeverPostForReal:
    """実投稿ガード（conftest.never_post_for_real）のテスト"""

    def test_create_tweet_is_blocked_in_tests(self, mocker):
        """DRY_RUN を外して本物のクライアントで投稿しようとしても X に届かないこと

        ローカルの .env に本物の認証情報があっても、モックを忘れたテストから
        本番投稿が走らないことを保証する。
        """
        mocker.patch("src.notifier.DRY_RUN", False)
        notifier = XNotifier.__new__(XNotifier)
        notifier.client = tweepy.Client(
            consumer_key="k",
            consumer_secret="s",
            access_token="t",
            access_token_secret="ts",
        )
        with pytest.raises(AssertionError, match="実投稿"):
            notifier._post_tweet("hello")
