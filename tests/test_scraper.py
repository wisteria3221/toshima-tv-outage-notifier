"""スクレイパーのテスト"""

from pathlib import Path

import pytest
import requests
import responses as responses_lib

from src.config import BACKOFF_FACTOR, MAX_RETRIES, TOSHIMA_TROUBLE_URL
from src.scraper import (
    OutageInfo,
    ToshimaScraper,
    UpstreamRejectedError,
    UpstreamUnavailableError,
)


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    """リトライのバックオフ待機（合計 40 秒）をテストでは省略する"""
    return mocker.patch("src.scraper.time.sleep")


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


class TestParseListPageDeduplication:
    """同じ障害へのリンクが複数ある場合のテスト"""

    def test_duplicate_links_to_same_outage_yield_one_entry(self, scraper):
        """1 エントリに同じ詳細 URL へのリンクが複数あっても 1 件にまとめること

        サムネイル画像リンクや「詳しくはこちら」リンクが追加されると同じ障害が
        2 件になり、新規障害として 2 回投稿されてしまう。
        """
        html = """
        <ul><li>
          <a href="/trouble/detail/91"><img alt="thumb"></a>
          <a href="/trouble/detail/91">2025.12.09（終了）緊急メンテナンス（池袋本町1丁目付近）</a>
          <a href="/trouble/detail/91/">詳しくはこちら</a>
        </li></ul>
        """
        outages = scraper._parse_list_page(html)

        assert [o.id for o in outages] == ["91"]
        # 最初にテキストを持つリンク（本文側）の内容が採用されること
        assert outages[0].status == "終了"
        assert outages[0].title == "緊急メンテナンス"

    def test_distinct_outages_are_all_kept(self, scraper, sample_list_html):
        """異なる障害は重複排除の影響を受けないこと"""
        outages = scraper._parse_list_page(sample_list_html)
        assert len({o.id for o in outages}) == len(outages) == 4


class TestUrlConstruction:
    """詳細 URL 構築のテスト"""

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("/trouble/detail/91", "https://www.toshima.co.jp/trouble/detail/91"),
            (
                "//www.toshima.co.jp/trouble/detail/91",
                "https://www.toshima.co.jp/trouble/detail/91",
            ),
            (
                "https://www.toshima.co.jp/trouble/detail/91/",
                "https://www.toshima.co.jp/trouble/detail/91/",
            ),
        ],
    )
    def test_href_variants_resolve_to_absolute_url(self, scraper, href, expected):
        """絶対パス・プロトコル相対・絶対 URL のいずれも正しい絶対 URL になること"""
        html = f'<a href="{href}">2025.12.09（終了）緊急メンテナンス</a>'
        outages = scraper._parse_list_page(html)
        assert outages[0].url == expected


class TestSessionLifecycle:
    """HTTP セッションの後始末のテスト"""

    def test_context_manager_closes_session(self, mocker):
        """with ブロックを抜けるとセッションが閉じられること"""
        with ToshimaScraper() as scraper:
            close = mocker.patch.object(scraper.session, "close")
        close.assert_called_once()


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

    def test_extract_status_ignores_bracket_inside_title(self, scraper):
        """タイトル途中の括弧（例: STB）をステータスとして誤認しない"""
        text = "2025.12.09 サービス停波（STB）について"
        assert scraper._extract_status(text) == ""

    def test_extract_status_without_date_prefix(self, scraper):
        """日付が無くても先頭の括弧はステータスとして扱う"""
        text = "（復旧）通信障害"
        assert scraper._extract_status(text) == "復旧"

    def test_extract_status_with_space_after_date(self, scraper):
        """日付と括弧の間に空白があっても抽出できる"""
        text = "2025.12.09 （終了）緊急メンテナンス"
        assert scraper._extract_status(text) == "終了"


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

    def test_bracket_inside_title_is_kept(self, scraper):
        """タイトル途中の括弧（地域でもステータスでもない）はタイトルに残ること"""
        text = "2025.12.09 サービス停波（STB）について"
        status = scraper._extract_status(text)
        title, area = scraper._extract_title_and_area(text, "2025.12.09", status)

        assert status == ""
        assert title == "サービス停波（STB）について"
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
    def test_returns_none_on_server_error_after_retries(self, scraper):
        """5xx がリトライ回数分続いた場合にNoneを返すこと"""
        for _ in range(MAX_RETRIES):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                status=503,
            )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is None
        assert len(responses_lib.calls) == MAX_RETRIES

    @responses_lib.activate
    @pytest.mark.parametrize("status", [403, 404, 410])
    def test_raises_immediately_on_client_error(self, scraper, no_sleep, status):
        """4xx はリトライせずに UpstreamRejectedError を送出すること

        URL 変更や UA ブロックはリトライしても変わらないため、40 秒のバックオフを
        消費せず即座に失敗させる。
        """
        responses_lib.add(responses_lib.GET, TOSHIMA_TROUBLE_URL, status=status)
        with pytest.raises(UpstreamRejectedError, match=str(status)):
            scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert len(responses_lib.calls) == 1
        no_sleep.assert_not_called()

    @responses_lib.activate
    @pytest.mark.parametrize("status", [408, 429])
    def test_transient_client_errors_are_retried(self, scraper, no_sleep, status):
        """408 / 429 は恒久的な拒否ではなく、5xx と同様にリトライすること

        一時的なレート制限やタイムアウトで終了コード 1（要対応の失敗）にすると、
        自然に解消する事象が /fail アラートになってしまう。
        """
        responses_lib.add(responses_lib.GET, TOSHIMA_TROUBLE_URL, status=status)
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body="<html><body>recovered</body></html>",
            status=200,
        )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is not None
        assert "recovered" in result
        assert len(responses_lib.calls) == 2
        no_sleep.assert_called_once()

    @responses_lib.activate
    def test_persistent_429_returns_none_not_rejected(self, scraper):
        """429 がリトライ回数分続いた場合は None（上流不通扱い）になること"""
        for _ in range(MAX_RETRIES):
            responses_lib.add(responses_lib.GET, TOSHIMA_TROUBLE_URL, status=429)
        assert scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL) is None
        assert len(responses_lib.calls) == MAX_RETRIES

    @responses_lib.activate
    def test_returns_none_after_all_connection_failures(self, scraper, no_sleep):
        """接続エラーがリトライ回数分続いた場合にNoneを返すこと"""
        for _ in range(MAX_RETRIES):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                body=requests.exceptions.ConnectionError("接続失敗"),
            )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is None
        assert len(responses_lib.calls) == MAX_RETRIES
        # 待機は試行間のみ（最終試行後は待たない）、指数バックオフ 1/3/9/27 秒
        waits = [c.args[0] for c in no_sleep.call_args_list]
        assert waits == [BACKOFF_FACTOR**i for i in range(MAX_RETRIES - 1)]

    @responses_lib.activate
    def test_recovers_when_later_attempt_succeeds(self, scraper):
        """途中の試行で成功すればHTMLを返すこと"""
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=requests.exceptions.ConnectionError("接続失敗"),
        )
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body="<html><body>recovered</body></html>",
            status=200,
        )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result is not None
        assert "recovered" in result


class TestResponseEncoding:
    """レスポンスの文字コード決定のテスト"""

    @responses_lib.activate
    def test_declared_charset_is_respected(self, scraper, mocker):
        """ヘッダで charset が宣言されていれば推定エンコーディングで上書きしないこと"""
        body = "2025.12.09（復旧）通信障害（目白3丁目付近）"
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=body.encode("utf-8"),
            status=200,
            content_type="text/html; charset=utf-8",
        )
        # 推定が誤って Shift_JIS を返しても無視されること
        apparent = mocker.patch(
            "requests.Response.apparent_encoding",
            new_callable=mocker.PropertyMock,
            return_value="shift_jis",
        )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result == body
        apparent.assert_not_called()

    @responses_lib.activate
    def test_falls_back_to_apparent_encoding_without_charset(self, scraper):
        """charset 未宣言のときは本文から推定したエンコーディングで読むこと

        requests は text/* で charset が無いと ISO-8859-1 を既定にするため、
        そのままでは日本語が文字化けする。
        """
        body = "2025.12.09（復旧）通信障害（目白3丁目付近）"
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=body.encode("utf-8"),
            status=200,
            content_type="text/html",
        )
        result = scraper._fetch_with_retry(TOSHIMA_TROUBLE_URL)
        assert result == body


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
    def test_raises_when_first_page_fetch_fails(self, scraper):
        """1ページ目のフェッチ失敗時に UpstreamUnavailableError を送出すること"""
        for _ in range(MAX_RETRIES):
            responses_lib.add(
                responses_lib.GET,
                TOSHIMA_TROUBLE_URL,
                status=500,
            )
        with pytest.raises(UpstreamUnavailableError):
            scraper.fetch_outage_list()

    @responses_lib.activate
    def test_raises_rejected_when_first_page_is_404(self, scraper):
        """1ページ目が 404 の場合は UpstreamUnavailableError ではなく
        UpstreamRejectedError を送出すること"""
        responses_lib.add(responses_lib.GET, TOSHIMA_TROUBLE_URL, status=404)
        with pytest.raises(UpstreamRejectedError):
            scraper.fetch_outage_list()

    @responses_lib.activate
    def test_returns_first_page_when_second_page_is_404(
        self, scraper, sample_list_html
    ):
        """2ページ目が 404 でも例外にせず、取得済みの結果を返すこと

        最終ページの次は存在しないので 404 は正常系。1 ページ目の 4xx とは区別する。
        """
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=sample_list_html,
            status=200,
            content_type="text/html; charset=utf-8",
        )
        responses_lib.add(
            responses_lib.GET,
            f"{TOSHIMA_TROUBLE_URL}page/2/",
            status=404,
        )
        outages = scraper.fetch_outage_list(max_pages=2)
        assert len(outages) == 4
        assert len(responses_lib.calls) == 2

    @responses_lib.activate
    def test_returns_first_page_when_second_page_fetch_fails(
        self, scraper, sample_list_html
    ):
        """2ページ目以降の失敗は例外にせず、取得済みの結果を返すこと"""
        responses_lib.add(
            responses_lib.GET,
            TOSHIMA_TROUBLE_URL,
            body=sample_list_html,
            status=200,
            content_type="text/html; charset=utf-8",
        )
        responses_lib.add(
            responses_lib.GET,
            f"{TOSHIMA_TROUBLE_URL}page/2/",
            status=500,
        )
        outages = scraper.fetch_outage_list(max_pages=2)
        assert len(outages) > 0
