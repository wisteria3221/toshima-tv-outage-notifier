"""テスト共通フィクスチャ

複数のテストファイルで重複定義されていた共通フィクスチャを集約する。
pytest は conftest.py を自動収集するため、各テストは引数名で fixture を受け取るだけでよい。
"""

import pytest

from src.scraper import OutageInfo


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
