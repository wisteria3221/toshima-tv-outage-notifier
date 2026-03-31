"""スクレイパーのテスト"""

from pathlib import Path

import pytest
import requests
import responses as responses_lib

from src.config import TOSHIMA_TROUBLE_URL
from src.scraper import OutageInfo, ToshimaScraper


@pytest.fixture
def sample_list_html():
    """テスト用HTMLフィクスチャ"""
    fixture_path = Path(__file__).parent / "fixtures" / "trouble_list.html"
    return fixture_path.read_text(encoding="utf-8")


@pytest.fixture
def scraper():
    """スクレイパーインスタンス"""
    return ToshimaScraper()


class TestToshimaScraper:
    """ToshimaScraperのテスト"""

    def test_parse_list_page_returns_outages(self, scraper, sample_list_html):
        """一覧ページのパースで障害情報が取得できること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert len(outages) == 4
        assert all(isinstance(o, OutageInfo) for o in outages)

    def test_parse_list_page_extracts_id(self, scraper, sample_list_html):
        """障害IDが正しく抽出されること"""
        outages = scraper._parse_list_page(sample_list_html)

        ids = [o.id for o in outages]
        assert ids == ["91", "90", "89", "88"]

    def test_parse_list_page_extracts_date(self, scraper, sample_list_html):
        """日付が正しく抽出されること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert outages[0].date == "2025.12.09"
        assert outages[1].date == "2025.12.05"
        assert outages[2].date == "2025.12.01"
        assert outages[3].date == "2025.11.28"

    def test_parse_list_page_extracts_status(self, scraper, sample_list_html):
        """ステータスが正しく抽出されること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert outages[0].status == "終了"
        assert outages[1].status == "復旧"
        assert outages[2].status == ""  # 進行中（ステータスなし）
        assert outages[3].status == "完了"

    def test_parse_list_page_extracts_title(self, scraper, sample_list_html):
        """タイトルが正しく抽出されること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert "緊急メンテナンス" in outages[0].title
        assert "インターネット接続障害" in outages[1].title
        assert "インターネットサービス不通" in outages[2].title
        assert "定期メンテナンス" in outages[3].title

    def test_parse_list_page_extracts_area(self, scraper, sample_list_html):
        """地域情報が正しく抽出されること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert "池袋本町1丁目" in outages[0].area
        assert "南池袋2丁目" in outages[1].area
        assert "目白3丁目" in outages[2].area
        assert outages[3].area == ""  # 地域情報なし

    def test_parse_list_page_builds_full_url(self, scraper, sample_list_html):
        """完全なURLが構築されること"""
        outages = scraper._parse_list_page(sample_list_html)

        assert outages[0].url == "https://www.toshima.co.jp/trouble/detail/91"
        assert outages[1].url == "https://www.toshima.co.jp/trouble/detail/90"


class TestExtractStatus:
    """ステータス抽出のテスト"""

    def test_extract_status_with_date(self, scraper):
        """日付付きテキストからステータスを抽出"""
        text = "2025.12.09（終了）緊急メンテナンス"
        assert scraper._extract_status(text) == "終了"

    def test_extract_status_restoration(self, scraper):
        """復旧ステータスを抽出"""
        text = "2025.12.05（復旧）インターネット接続障害"
        assert scraper._extract_status(text) == "復旧"

    def test_extract_status_no_status(self, scraper):
        """ステータスがない場合は空文字"""
        text = "2025.12.01インターネットサービス不通"
        assert scraper._extract_status(text) == ""

    def test_extract_status_ignores_area(self, scraper):
        """地域情報をステータスとして誤認しない"""
        text = "2025.12.01（池袋1丁目付近）障害発生"
        assert scraper._extract_status(text) == ""


class TestExtractTitleAndArea:
    """タイトルと地域抽出のテスト"""

    def test_extract_title_and_area_with_both(self, scraper):
        """タイトルと地域の両方がある場合"""
        text = "緊急メンテナンス（池袋本町1丁目付近）"
        title, area = scraper._extract_title_and_area(text, "", "")

        assert "緊急メンテナンス" in title
        assert "池袋本町1丁目" in area

    def test_extract_title_and_area_no_area(self, scraper):
        """地域情報がない場合"""
        text = "定期メンテナンス"
        title, area = scraper._extract_title_and_area(text, "", "")

        assert "定期メンテナンス" in title
        assert area == ""


class TestFetchWithRetry:
    """_fetch_with_retry のテスト"""

    @responses_lib.activate
    def test_successful_fetch_returns_html(self, scraper):
        """正常レスポンスでHTMLを返すこと"""
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body="<html><body>test</body></html>",
            status=200,
        )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is not None
        assert "test" in result

    @responses_lib.activate
    def test_returns_none_on_http_error(self, scraper):
        """HTTPエラー（404）の場合にNoneを返すこと（リトライ後）"""
        for _ in range(3):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                status=404,
            )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is None

    @responses_lib.activate
    def test_returns_none_after_all_connection_failures(self, scraper):
        """接続エラーがリトライ回数分続いた場合にNoneを返すこと"""
        for _ in range(3):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                body=requests.exceptions.ConnectionError("接続失敗"),
            )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is None


class TestFetchOutageList:
    """fetch_outage_list のテスト"""

    @responses_lib.activate
    def test_returns_outages_from_page(self, scraper, sample_list_html):
        """正常なHTMLから障害情報リストを返すこと"""
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=sample_list_html,
            status=200,
            content_type="text/html; charset=utf-8",
        )
        outages = scraper.fetch_outage_list()
        assert len(outages) > 0
        assert all(isinstance(o, OutageInfo) for o in outages)

    @responses_lib.activate
    def test_returns_empty_list_when_fetch_fails(self, scraper):
        """フェッチ失敗時に空リストを返すこと"""
        for _ in range(3):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                status=500,
            )
        outages = scraper.fetch_outage_list()
        assert outages == []
