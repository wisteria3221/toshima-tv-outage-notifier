"""障害情報スクレイピングモジュール"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup

from .config import (
    BACKOFF_FACTOR,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    TOSHIMA_BASE_URL,
    TOSHIMA_TROUBLE_URL,
)

logger = logging.getLogger(__name__)


@dataclass
class OutageInfo:
    """障害情報データクラス"""

    id: str  # 障害ID（詳細URLから抽出）
    date: str  # 日付（YYYY.MM.DD形式）
    status: str  # ステータス（終了/復旧/調査中/空文字=進行中）
    title: str  # 障害タイトル
    area: str  # 影響地域
    url: str  # 詳細ページURL
    last_updated: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class ToshimaScraper:
    """としまテレビ障害情報スクレイパー"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "ToshimaTVOutageNotifier/1.0 (GitHub Actions Bot)"}
        )

    def fetch_outage_list(self, max_pages: int = 1) -> list[OutageInfo]:
        """障害情報一覧を取得

        Args:
            max_pages: 取得する最大ページ数（デフォルト: 1）

        Returns:
            障害情報のリスト
        """
        all_outages = []

        for page in range(1, max_pages + 1):
            url = (
                TOSHIMA_TROUBLE_URL
                if page == 1
                else f"{TOSHIMA_TROUBLE_URL}page/{page}/"
            )

            html = self._fetch_with_retry(url)
            if html is None:
                logger.warning(f"ページ {page} の取得に失敗しました")
                break

            outages = self._parse_list_page(html)
            if not outages:
                logger.info(f"ページ {page} に障害情報がありませんでした")
                break

            all_outages.extend(outages)
            logger.info(f"ページ {page} から {len(outages)} 件の障害情報を取得")

        return all_outages

    def _fetch_with_retry(self, url: str) -> str | None:
        """リトライ付きでページを取得

        Args:
            url: 取得するURL

        Returns:
            HTMLコンテンツ、失敗時はNone
        """
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                response.encoding = response.apparent_encoding
                return response.text

            except requests.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"URL取得失敗 (最終試行): {url} - {e}")
                    return None

                wait_time = BACKOFF_FACTOR**attempt
                logger.warning(
                    f"リトライ {attempt + 1}/{MAX_RETRIES}: {url} - {wait_time}秒後に再試行"
                )
                time.sleep(wait_time)

        return None

    def _parse_list_page(self, html: str) -> list[OutageInfo]:
        """一覧ページのHTMLをパース

        Args:
            html: HTMLコンテンツ

        Returns:
            障害情報のリスト
        """
        soup = BeautifulSoup(html, "html.parser")
        outages = []

        # 障害詳細へのリンクを含む要素を探す
        # パターン: /trouble/detail/{ID} または /trouble/detail/{ID}/
        links = soup.find_all("a", href=re.compile(r"/trouble/detail/\d+"))

        for link in links:
            try:
                outage = self._parse_outage_entry(link)
                if outage:
                    outages.append(outage)
            except Exception as e:
                logger.warning(f"障害エントリーのパースに失敗: {e}")
                continue

        return outages

    def _parse_outage_entry(self, link_element) -> OutageInfo | None:
        """個別の障害エントリーをパース

        Args:
            link_element: BeautifulSoupのリンク要素

        Returns:
            OutageInfo、パース失敗時はNone
        """
        href = link_element.get("href", "")
        text = link_element.get_text(strip=True)

        if not href or not text:
            return None

        # IDを抽出
        id_match = re.search(r"/trouble/detail/(\d+)", href)
        if not id_match:
            return None
        outage_id = id_match.group(1)

        # 日付を抽出（YYYY.MM.DD形式）
        date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
        date = date_match.group(1) if date_match else ""

        # ステータスを抽出（最初の括弧内テキスト）
        status = self._extract_status(text)

        # タイトルと地域を抽出
        title, area = self._extract_title_and_area(text, date, status)

        # 完全なURLを構築
        full_url = f"{TOSHIMA_BASE_URL}{href}" if href.startswith("/") else href

        return OutageInfo(
            id=outage_id,
            date=date,
            status=status,
            title=title,
            area=area,
            url=full_url,
        )

    def _extract_status(self, text: str) -> str:
        """テキストからステータスを抽出

        Args:
            text: エントリーのテキスト

        Returns:
            ステータス文字列（終了/復旧/完了等）、なければ空文字
        """
        # 日付の後に続く括弧内のステータスを探す
        # 例: "2025.12.09（終了）緊急メンテナンス..."
        status_match = re.search(
            r"(?:\d{4}\.\d{2}\.\d{2})?\s*[（(]([^）)]+)[）)]", text
        )

        if status_match:
            status = status_match.group(1)
            # 地域情報（丁目、付近など）ではないことを確認
            if not re.search(r"丁目|付近|地区|町|番地", status):
                return status

        return ""

    def _extract_title_and_area(
        self, text: str, date: str, status: str
    ) -> tuple[str, str]:
        """テキストからタイトルと地域を抽出

        Args:
            text: エントリーのテキスト
            date: 日付文字列
            status: ステータス文字列

        Returns:
            (タイトル, 地域) のタプル
        """
        # 日付とステータスを除去
        clean_text = text

        if date:
            clean_text = clean_text.replace(date, "")

        if status:
            # ステータス部分（括弧込み）を除去
            clean_text = re.sub(rf"[（(]{re.escape(status)}[）)]", "", clean_text)

        clean_text = clean_text.strip()

        # 地域情報を抽出（括弧内で「丁目」「付近」などを含むもの）
        area_match = re.search(
            r"[（(]([^）)]*(?:丁目|付近|地区|町|番地)[^）)]*)[）)]", clean_text
        )
        area = area_match.group(1) if area_match else ""

        # タイトルを抽出（地域情報を除去）
        title = clean_text
        if area:
            title = re.sub(rf"[（(]{re.escape(area)}[）)]", "", title)

        title = title.strip()

        return title, area
