"""テスト共通フィクスチャ

複数のテストファイルで使うフィクスチャを集約する。
pytest は conftest.py を自動収集するため、各テストは引数名で fixture を受け取るだけでよい。
"""

import pytest

from src.notifier import XNotifier
from src.scraper import OutageInfo


@pytest.fixture(autouse=True)
def never_post_for_real(mocker):
    """テストから X への実投稿が呼ばれないことを保証するガード

    main() 系のテストは本物の XNotifier を生成し、投稿メソッドを個別にモックしている。
    ローカルに本物の認証情報を持つ .env があると、モックを忘れたテストを追加した
    時点で本番投稿が走るため、create_tweet を必ず失敗させておく。
    投稿処理を試験するテストは、このフィクスチャより後に自分で patch し直す。
    """
    return mocker.patch(
        "tweepy.Client.create_tweet",
        side_effect=AssertionError("テストから X への実投稿が呼ばれました"),
    )


@pytest.fixture
def temp_state_file(tmp_path):
    """一時的な状態ファイル"""
    return tmp_path / "state.json"


@pytest.fixture
def make_outage():
    """障害情報を生成するファクトリ

    既定値は sample_outage と同じ。id を変えると URL も追従する。
    フィールドを個別に上書きしたいときはキーワード引数で渡す。
    """

    def _make(id: str = "100", **overrides) -> OutageInfo:
        fields = {
            "id": id,
            "date": "2025.12.20",
            "status": "",
            "title": "テスト障害",
            "area": "池袋",
            "url": f"https://www.toshima.co.jp/trouble/detail/{id}",
        }
        fields.update(overrides)
        return OutageInfo(**fields)

    return _make


@pytest.fixture
def sample_outage(make_outage):
    """テスト用の障害情報"""
    return make_outage()


@pytest.fixture
def notifier():
    """投稿クライアントを持たない XNotifier（メッセージ整形のテスト用）

    __init__ は環境変数から tweepy クライアントを組み立てるため通さない。
    """
    instance = XNotifier.__new__(XNotifier)
    instance.client = None
    return instance
