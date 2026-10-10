"""テスト共通フィクスチャ

複数のテストファイルで重複定義されていた共通フィクスチャを集約する。
pytest は conftest.py を自動収集するため、各テストは引数名で fixture を受け取るだけでよい。
"""

import pytest

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
def sample_outage():
    """テスト用の障害情報"""
    return OutageInfo(
        id="100",
        date="2025.12.20",
        status="",
        title="テスト障害",
        area="池袋",
        url="https://www.toshima.co.jp/trouble/detail/100",
    )
