"""テスト共通ガードのテスト"""

import os

import pytest
import tweepy

from src.config import get_wayback_credentials


class TestNeverPostForReal:
    """実投稿ガード（conftest.never_post_for_real）のテスト"""

    def test_create_tweet_is_blocked_in_tests(self, mocker, notifier):
        """DRY_RUN を外して本物のクライアントで投稿しようとしても X に届かないこと

        ローカルの .env に本物の認証情報があっても、モックを忘れたテストから
        本番投稿が走らないことを保証する。
        """
        mocker.patch("src.notifier.DRY_RUN", False)
        notifier.client = tweepy.Client(
            consumer_key="k",
            consumer_secret="s",
            access_token="t",
            access_token_secret="ts",
        )
        with pytest.raises(AssertionError, match="実投稿"):
            notifier._post_tweet("hello")


class TestNoWaybackCredentials:
    """魚拓要求ガード（conftest.no_wayback_credentials）のテスト"""

    def test_wayback_credentials_are_empty_in_tests(self):
        """プロセスの環境変数に Wayback のキーがあってもテスト内では空文字になること

        ローカルの .env に本物のキーがあっても、モックを忘れたテストから
        Wayback Machine への保存要求が出ないことを保証する。
        例: `WAYBACK_ACCESS_KEY=x WAYBACK_SECRET_KEY=y uv run pytest` でも通る。
        """
        assert "WAYBACK_ACCESS_KEY" not in os.environ
        assert "WAYBACK_SECRET_KEY" not in os.environ
        assert get_wayback_credentials() == ("", "")
