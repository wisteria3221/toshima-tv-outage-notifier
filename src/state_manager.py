"""状態管理モジュール"""

import copy
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import STATE_FILE_PATH
from .scraper import OutageInfo

logger = logging.getLogger(__name__)

# 状態ファイルに保存する OutageInfo のフィールド（変更判定・更新・新規作成で共有）
_TRACKED_FIELDS = ("date", "status", "title", "area", "url")


def _current_month() -> str:
    """月間カウンタのキー（UTC の YYYY-MM）を返す"""
    return datetime.now(UTC).strftime("%Y-%m")


def display_status(status: str) -> str:
    """ステータスの表示名を返す（空文字 = 括弧なし = 「進行中」）"""
    return status or "進行中"


class StateFileError(Exception):
    """既存の状態ファイルを読み込めなかったことを表す例外

    状態ファイルは通知判断の唯一の根拠なので、壊れている場合に空の初期状態で
    続行すると、掲載中の全障害を新規として再通知した上で履歴を上書きしてしまう。
    そのため読み込み失敗は処理を止めて人に気づかせる（main では終了コード 1）。
    ファイルが存在しない場合は正常な初回実行なので、この例外は使わない。
    """


@dataclass
class StatusChange:
    """ステータス変更情報"""

    outage: OutageInfo
    old_status: str
    new_status: str

    def describe(self) -> str:
        """ログ用の「旧 -> 新」表記を返す"""
        return f"{display_status(self.old_status)} -> {display_status(self.new_status)}"


@dataclass
class ChangeResult:
    """差分検出結果"""

    new_outages: list[OutageInfo]
    status_changes: list[StatusChange]

    def has_changes(self) -> bool:
        """変更があるかどうか"""
        return bool(self.new_outages or self.status_changes)


class StateManager:
    """状態管理クラス

    JSONファイルで障害情報の状態を永続化し、
    新規障害やステータス変更を検出する
    """

    SCHEMA_VERSION = "1.2"

    def __init__(self, state_file: Path | None = None):
        """初期化

        Args:
            state_file: 状態ファイルのパス（デフォルト: data/state.json）
        """
        self.state_file = state_file or STATE_FILE_PATH
        self.state = self._load_state()
        self._dirty = False

    def _load_state(self) -> dict:
        """状態ファイルを読み込む

        旧形式（schema_version 1.0/1.1、障害エントリに archives が無い）も変換せずに
        そのまま読み、魚拓の記録なしとして扱う。schema_version の値では分岐しない。

        Returns:
            状態辞書

        Raises:
            StateFileError: ファイルは存在するが読み込めない・JSON として不正な場合
        """
        if not self.state_file.exists():
            logger.info(f"状態ファイルが存在しません: {self.state_file}")
            return self._create_initial_state()

        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # 初期状態で続行すると全障害を再通知し履歴も上書きしてしまうため、止める
            raise StateFileError(
                f"状態ファイルを読み込めません: {self.state_file} - {e}"
            ) from e

        if not isinstance(state, dict) or not isinstance(state.get("outages"), dict):
            raise StateFileError(
                f"状態ファイルの形式が不正です（outages が辞書ではありません）: "
                f"{self.state_file}"
            )

        logger.info(
            f"状態ファイルを読み込みました: {len(state['outages'])} 件の障害情報"
        )
        return state

    def _create_initial_state(self) -> dict:
        """初期状態を作成

        Returns:
            初期状態辞書
        """
        return {
            "schema_version": self.SCHEMA_VERSION,
            "outages": {},
            "stats": {
                "total_notifications_this_month": 0,
                "month": _current_month(),
            },
        }

    def save_state(self, force: bool = False) -> bool:
        """状態をファイルに保存

        Args:
            force: Trueの場合、dirty状態に関わらず強制保存

        Returns:
            実際に保存した場合True、スキップした場合False
        """
        if not force and not self._dirty:
            logger.debug("状態に変更がないため保存をスキップします")
            return False

        # ディレクトリが存在しない場合は作成
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # 一時ファイルに書き切ってから差し替える（アトミック書き込み）。
        # 直接書くと途中で失敗したときに壊れたファイルが残り、終了コード 1 の
        # ワークフローがそれをコミットしてしまう。次回は StateFileError で停止し
        # 人手での修復が必要になるため、元のファイルは最後まで無傷に保つ。
        tmp_file = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.state_file)
            logger.info(f"状態ファイルを保存しました: {self.state_file}")
            self._dirty = False
            return True
        except OSError as e:
            logger.error(f"状態ファイルの保存に失敗: {e}")
            tmp_file.unlink(missing_ok=True)
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
        stored_outages = self.state["outages"]

        for outage in current_outages:
            stored = stored_outages.get(outage.id)
            if stored is None:
                new_outages.append(outage)
                logger.info(f"新規障害を検出: ID={outage.id}, タイトル={outage.title}")
                continue

            # 既存障害: ステータスが変わり、かつ未通知のステータスなら変更として扱う
            old_status = stored.get("status", "")
            if outage.status == old_status:
                continue
            if outage.status in stored.get("notified_statuses", []):
                continue

            change = StatusChange(
                outage=outage, old_status=old_status, new_status=outage.status
            )
            status_changes.append(change)
            logger.info(f"ステータス変更を検出: ID={outage.id}, {change.describe()}")

        return ChangeResult(new_outages=new_outages, status_changes=status_changes)

    def update_outages(self, outages: list[OutageInfo]) -> None:
        """障害情報を更新（通知状態と魚拓記録は保持）

        既存エントリは追跡フィールド（_TRACKED_FIELDS）だけを上書きするので、
        notified_statuses と archives はそのまま残る。

        Args:
            outages: 最新の障害情報リスト
        """
        # 月のロールオーバーチェック
        self._ensure_current_month()

        stored_outages = self.state["outages"]
        now = datetime.now(UTC).isoformat()

        for outage in outages:
            fields = {name: getattr(outage, name) for name in _TRACKED_FIELDS}
            existing = stored_outages.get(outage.id)

            if existing is None:
                # 新規エントリー: 常にdirty
                self._mark_dirty()
                stored_outages[outage.id] = {
                    "id": outage.id,
                    **fields,
                    "first_seen": now,
                    "last_updated": now,
                    "notified_statuses": [],
                    "archives": [],
                }
            elif any(existing.get(k) != v for k, v in fields.items()):
                # 既存エントリー: いずれかのフィールドが変わっていれば更新
                # （追跡フィールドのみ。notified_statuses と archives は保持する）
                self._mark_dirty()
                existing.update(fields, last_updated=now)

    def snapshot_outages(self) -> dict:
        """障害エントリの現在の状態を複製して返す（rollback_outages 用）

        Returns:
            outages 辞書のディープコピー
        """
        return copy.deepcopy(self.state["outages"])

    def rollback_outages(self, outage_ids: Iterable[str], snapshot: dict) -> None:
        """指定した障害エントリを snapshot 時点の内容に戻す

        通知に失敗した障害だけを update_outages() 前の状態に戻し、次回実行で
        再び「新規」または「ステータス変更」として検出されるようにする。
        snapshot に存在しなかった ID（新規障害）はエントリごと削除する。
        snapshot のエントリはそのまま state に移すので、呼び出し後に snapshot を
        使い回さないこと。

        Args:
            outage_ids: 戻す対象の障害ID
            snapshot: snapshot_outages() の戻り値
        """
        outages = self.state["outages"]
        for outage_id in outage_ids:
            if outage_id in snapshot:
                outages[outage_id] = snapshot[outage_id]
            else:
                outages.pop(outage_id, None)
            self._mark_dirty()
            logger.debug(f"障害エントリを巻き戻し: ID={outage_id}")

    def mark_notified(self, outage_id: str, status: str) -> None:
        """ステータスを通知済みとしてマーク

        Args:
            outage_id: 障害ID
            status: 通知したステータス
        """
        entry = self.state["outages"].get(outage_id)
        if entry is None:
            return
        notified = entry.setdefault("notified_statuses", [])
        if status not in notified:
            notified.append(status)
            self._mark_dirty()
            logger.debug(f"通知済みマーク: ID={outage_id}, ステータス={status}")

    def record_archive(self, outage_id: str, status: str, archived_url: str) -> None:
        """魚拓の記録を障害エントリの archives に追記する

        記録は {"status", "archived_at", "url"} の 3 キーで、archived_at は呼び出し
        時刻（UTC、ISO 8601。first_seen と同形式）。既存の記録は保持して末尾に追記し、
        同じステータスの記録が既にあっても追記する（巻き戻し後の再検出や push 失敗後の
        再実行で同じステータスを再取得した記録も残すため）。
        archives を持たない旧形式のエントリには初回追記時に配列を作る。
        mark_notified() と同じく、state に存在しない障害 ID は無視する。

        Args:
            outage_id: 障害ID
            status: 魚拓を取った時点の対象ステータス（空文字 = 進行中）
            archived_url: 魚拓の公開 URL
        """
        entry = self.state["outages"].get(outage_id)
        if entry is None:
            return
        entry.setdefault("archives", []).append(
            {
                "status": status,
                "archived_at": datetime.now(UTC).isoformat(),
                "url": archived_url,
            }
        )
        self._mark_dirty()
        logger.debug(
            f"魚拓を記録: ID={outage_id}, ステータス={display_status(status)}, "
            f"URL={archived_url}"
        )

    def increment_notification_count(self) -> None:
        """月間通知カウントをインクリメント"""
        self._ensure_current_month()
        stats = self.state.get("stats", {})
        stats["total_notifications_this_month"] = (
            stats.get("total_notifications_this_month", 0) + 1
        )
        self.state["stats"] = stats
        self._mark_dirty()

    def get_notification_count_this_month(self) -> int:
        """今月の通知数を取得

        Returns:
            今月の通知数
        """
        stats = self.state.get("stats", {})

        # 月が変わっていればカウントは0
        if stats.get("month") != _current_month():
            return 0

        return stats.get("total_notifications_this_month", 0)

    def is_dirty(self) -> bool:
        """状態に保存が必要な変更があるかチェック

        Returns:
            変更がある場合True
        """
        return self._dirty

    def _mark_dirty(self) -> None:
        """状態を変更済みとしてマーク（内部用）"""
        self._dirty = True
        logger.debug("状態が変更されました（保存が必要）")

    def _ensure_current_month(self) -> None:
        """月が変わっていた場合に統計カウンターをリセット（内部用）"""
        current_month = _current_month()
        stats = self.state.get("stats", {})
        if stats.get("month") != current_month:
            logger.info(f"月が変わりました: {stats.get('month')} -> {current_month}")
            stats["month"] = current_month
            stats["total_notifications_this_month"] = 0
            self.state["stats"] = stats
            self._mark_dirty()
