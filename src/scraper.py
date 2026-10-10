"""障害情報スクレイピングモジュール"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from .config import (
    BACKOFF_FACTOR,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    TOSHIMA_BASE_URL,
    TOSHIMA_TROUBLE_URL,
)

logger = logging.getLogger(__name__)

# スクレイピング用の静的正規表現定数群
# 同一パターンの重複定義を避けるため、module レベルで事前コンパイルして共有する。
# 入力依存の動的パターン（re.escape を用いる re.sub）は定数化しない。

# 地域キーワード（否定判定・抽出の両方で共有する単一の文字列定数）
_AREA_KEYWORDS = "丁目|付近|地区|町|番地"

# 詳細リンク判定とID抽出を兼ねる定数（キャプチャグループ付き）。
# find_all(href=...) は re.search 意味で照合するため、グループ追加は照合結果を変えない。
_RE_DETAIL_ID = re.compile(r"/trouble/detail/(\d+)")

# 日付抽出（YYYY.MM.DD形式）
_RE_DATE = re.compile(r"(\d{4}\.\d{2}\.\d{2})")

# ステータス抽出（テキスト先頭の日付直後に続く括弧内テキストのみ）
# 先頭に固定しないと、タイトル途中の括弧（例「サービス停波（STB）について」）を
# ステータスとして誤認する。実サイトではステータスは常に日付直後に置かれている。
_RE_STATUS = re.compile(r"^\s*(?:\d{4}\.\d{2}\.\d{2})?\s*[（(]([^）)]+)[）)]")

# ステータスが地域情報でないことを確認する否定判定用
_RE_AREA_KEYWORD = re.compile(_AREA_KEYWORDS)

# 地域抽出（括弧内で地域キーワードを含むもの）
_RE_AREA_IN_BRACKETS = re.compile(rf"[（(]([^）)]*(?:{_AREA_KEYWORDS})[^）)]*)[）)]")


class UpstreamUnavailableError(Exception):
    """障害情報ページ自体を取得できなかったことを表す例外

    リトライを使い切っても としまテレビ側に接続できない／ランナー側で名前解決できない
    といった一時的なネットワーク要因で発生する。プログラムの不具合や通知失敗とは
    区別して扱う（main では終了コード 2 を返す）。
    """


class UpstreamRejectedError(Exception):
    """障害情報ページが 4xx で拒否されたことを表す例外

    404（URL 変更）や 403（User-Agent のブロック）はリトライしても結果が変わらず、
    放置すると監視が止まったままになる。一時的な不通（UpstreamUnavailableError）
    とは区別し、リトライせずに即座に失敗させて人に気づかせる（main では終了コード 1）。
    """


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

        Raises:
            UpstreamUnavailableError: 1 ページ目をリトライ後も取得できなかった場合
            UpstreamRejectedError: 4xx で拒否された場合（1 ページ目・2 ページ目以降とも）
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
                if page == 1:
                    # 1 ページ目すら取れない = 上流サイトに到達できない状態。
                    # 空リスト（= パース結果が空）とは区別するため例外で通知する。
                    raise UpstreamUnavailableError(
                        f"障害情報ページを取得できません: {url}"
                    )
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

        接続エラー・タイムアウト・5xx は一時的な要因とみなして指数バックオフで
        再試行する。4xx はリトライしても変わらないため即座に例外にする。

        Args:
            url: 取得するURL

        Returns:
            HTMLコンテンツ、リトライを使い切った場合はNone

        Raises:
            UpstreamRejectedError: 4xx 応答を受けた場合（リトライしない）
        """
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if 400 <= response.status_code < 500:
                    raise UpstreamRejectedError(
                        f"障害情報ページが拒否されました: {url} - "
                        f"HTTP {response.status_code}"
                    )
                response.raise_for_status()
                self._apply_fallback_encoding(response)
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

    @staticmethod
    def _apply_fallback_encoding(response: requests.Response) -> None:
        """Content-Type に charset が無い場合だけ本文からの推定エンコーディングを使う

        ヘッダで charset が宣言されていればそれを信頼する。推定（apparent_encoding）は
        短い本文で誤判定することがあり、無条件に上書きすると全角括弧が文字化けして
        ステータス抽出が全滅する。

        Args:
            response: 取得したレスポンス（encoding をその場で書き換える）
        """
        content_type = response.headers.get("Content-Type", "")
        if "charset=" in content_type.lower():
            return
        response.encoding = response.apparent_encoding

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
        links = soup.find_all("a", href=_RE_DETAIL_ID)

        for link in links:
            try:
                outage = self._parse_outage_entry(link)
                if outage:
                    outages.append(outage)
            except (AttributeError, ValueError) as e:
                logger.warning(f"障害エントリーのパースに失敗: {e}")
                continue

        return outages

    def _parse_outage_entry(self, link_element: Tag) -> OutageInfo | None:
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
        id_match = _RE_DETAIL_ID.search(href)
        if not id_match:
            return None
        outage_id = id_match.group(1)

        # 日付を抽出（YYYY.MM.DD形式）
        date_match = _RE_DATE.search(text)
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
        # テキスト先頭の日付直後に続く括弧内のステータスを探す
        # 例: "2025.12.09（終了）緊急メンテナンス..."
        # タイトル途中の括弧はステータスではないので見ない
        status_match = _RE_STATUS.match(text)

        if status_match:
            status = status_match.group(1)
            # 地域情報（丁目、付近など）ではないことを確認
            if not _RE_AREA_KEYWORD.search(status):
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
        area_match = _RE_AREA_IN_BRACKETS.search(clean_text)
        area = area_match.group(1) if area_match else ""

        # タイトルを抽出（地域情報を除去）
        title = clean_text
        if area:
            title = re.sub(rf"[（(]{re.escape(area)}[）)]", "", title)

        title = title.strip()

        return title, area
