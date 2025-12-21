"""設定管理モジュール"""

import os
from pathlib import Path

# プロジェクトルート
PROJECT_ROOT = Path(__file__).parent.parent

# としまテレビ関連URL
TOSHIMA_BASE_URL = "https://www.toshima.co.jp"
TOSHIMA_TROUBLE_URL = f"{TOSHIMA_BASE_URL}/trouble/"

# ファイルパス
STATE_FILE_PATH = PROJECT_ROOT / "data" / "state.json"

# X API投稿制限
MONTHLY_TWEET_LIMIT = 450  # Free枠500の90%を安全マージンとして設定

# リトライ設定
MAX_RETRIES = 3
BACKOFF_FACTOR = 2  # 指数バックオフの係数

# タイムアウト設定（秒）
REQUEST_TIMEOUT = 30

# DRY RUNモード（True の場合、X への投稿をスキップ）
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ログレベル
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def get_x_credentials() -> dict:
    """X API認証情報を環境変数から取得"""
    return {
        "consumer_key": os.environ.get("X_API_KEY", ""),
        "consumer_secret": os.environ.get("X_API_SECRET", ""),
        "access_token": os.environ.get("X_ACCESS_TOKEN", ""),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    }


def validate_x_credentials() -> bool:
    """X API認証情報が設定されているか確認"""
    creds = get_x_credentials()
    return all(creds.values())
