"""状態管理モジュール"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import STATE_FILE_PATH
from .scraper import OutageInfo

logger = logging.getLogger(__name__)


@dataclass
class StatusChange:
    """ステータス変更情報"""

    outage: OutageInfo
    old_status: str
    new_status: str


@dataclass
class ChangeResult:
    """差分検出結果"""

    new_outages: list[OutageInfo]
    status_changes: list[StatusChange]

    def has_changes(self) -> bool:
        """変更があるかどうか"""
        return len(self.new_outages) > 0 or len(self.status_changes) > 0

    def total_changes(self) -> int:
        """変更の総数"""
        return len(self.new_outages) + len(self.status_changes)


class StateManager:
    """状態管理クラス

    JSONファイルで障害情報の状態を永続化し、
    新規障害やステータス変更を検出する
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, state_file: Path | None = None):
        """初期化

        Args:
            state_file: 状態ファイルのパス（デフォルト: data/state.json）
        """
        self.state_file = state_file or STATE_FILE_PATH
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """状態ファイルを読み込む

        Returns:
            状態辞書
        """
        if not self.state_file.exists():
            logger.info(f"状態ファイルが存在しません: {self.state_file}")
            return self._create_initial_state()

        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
                logger.info(
                    f"状態ファイルを読み込みました: {len(state.get('outages', {}))} 件の障害情報"
                )
                return state
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"状態ファイルの読み込みに失敗: {e}")
            return self._create_initial_state()

    def _create_initial_state(self) -> dict:
        """初期状態を作成

        Returns:
            初期状態辞書
        """
        now = datetime.now(UTC).isoformat()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "last_check": now,
            "outages": {},
            "stats": {
                "total_notifications_this_month": 0,
                "month": datetime.now(UTC).strftime("%Y-%m"),
            },
        }

    def save_state(self) -> None:
        """状態をファイルに保存"""
        # ディレクトリが存在しない場合は作成
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        self.state["last_check"] = datetime.now(UTC).isoformat()

        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            logger.info(f"状態ファイルを保存しました: {self.state_file}")
        except OSError as e:
            logger.error(f"状態ファイルの保存に失敗: {e}")
            raise

    def get_changes(self, current_outages: list[OutageInfo]) -> ChangeResult:
        """新規障害とステータス変更を検出

        Args:
            current_outages: 現在の障害情報リスト

        Returns:
            ChangeResult オブジェクト
        """
        new_outages = []
        status_changes = []
        stored_outages = self.state.get("outages", {})

        for outage in current_outages:
            if outage.id not in stored_outages:
                # 新規障害
                new_outages.append(outage)
                logger.info(f"新規障害を検出: ID={outage.id}, タイトル={outage.title}")

            else:
                # 既存障害のステータス変更チェック
                stored = stored_outages[outage.id]
                old_status = stored.get("status", "")
                new_status = outage.status

                # ステータスが変更された場合
                if old_status != new_status:
                    # 既に通知済みのステータスかチェック
                    notified_statuses = stored.get("notified_statuses", [])
                    if new_status not in notified_statuses:
                        status_changes.append(
                            StatusChange(
                                outage=outage,
                                old_status=old_status,
                                new_status=new_status,
                            )
                        )
                        logger.info(
                            f"ステータス変更を検出: ID={outage.id}, "
                            f"{old_status or '(進行中)'} -> {new_status or '(進行中)'}"
                        )

        return ChangeResult(new_outages=new_outages, status_changes=status_changes)

    def update_outages(self, outages: list[OutageInfo]) -> None:
        """障害情報を更新（通知状態は保持）

        Args:
            outages: 最新の障害情報リスト
        """
        stored_outages = self.state.get("outages", {})
        now = datetime.now(UTC).isoformat()

        for outage in outages:
            if outage.id in stored_outages:
                # 既存エントリーを更新（通知状態は保持）
                existing = stored_outages[outage.id]
                existing.update(
                    {
                        "date": outage.date,
                        "status": outage.status,
                        "title": outage.title,
                        "area": outage.area,
                        "url": outage.url,
                        "last_updated": now,
                    }
                )
            else:
                # 新規エントリー
                stored_outages[outage.id] = {
                    "id": outage.id,
                    "date": outage.date,
                    "status": outage.status,
                    "title": outage.title,
                    "area": outage.area,
                    "url": outage.url,
                    "first_seen": now,
                    "last_updated": now,
                    "notified_statuses": [],
                }

        self.state["outages"] = stored_outages

    def mark_notified(self, outage_id: str, status: str) -> None:
        """ステータスを通知済みとしてマーク

        Args:
            outage_id: 障害ID
            status: 通知したステータス
        """
        outages = self.state.get("outages", {})
        if outage_id in outages:
            notified = outages[outage_id].get("notified_statuses", [])
            if status not in notified:
                notified.append(status)
                outages[outage_id]["notified_statuses"] = notified
                logger.debug(f"通知済みマーク: ID={outage_id}, ステータス={status}")

    def increment_notification_count(self) -> None:
        """月間通知カウントをインクリメント"""
        current_month = datetime.now(UTC).strftime("%Y-%m")
        stats = self.state.get("stats", {})

        # 月が変わった場合はカウントをリセット
        if stats.get("month") != current_month:
            stats["month"] = current_month
            stats["total_notifications_this_month"] = 0

        stats["total_notifications_this_month"] = (
            stats.get("total_notifications_this_month", 0) + 1
        )
        self.state["stats"] = stats

    def get_notification_count_this_month(self) -> int:
        """今月の通知数を取得

        Returns:
            今月の通知数
        """
        current_month = datetime.now(UTC).strftime("%Y-%m")
        stats = self.state.get("stats", {})

        # 月が変わっていればカウントは0
        if stats.get("month") != current_month:
            return 0

        return stats.get("total_notifications_this_month", 0)
