"""設定管理モジュール"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 環境変数を評価する前に .env を反映する（ローカル実行用。CI ではシークレットが環境変数に入る）
# main.py で呼ぶと、このモジュールの import 時に DRY_RUN や LOG_LEVEL が先に評価されてしまい
# .env の値が無視される（DRY_RUN=true のつもりで本番投稿される）ため、ここで読む。
load_dotenv()

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
# としまテレビ側の接続タイムアウトやランナー側の一時的な DNS 失敗が数分続くことがあるため、
# 5 回試行・待機 1/3/9/27 秒（計 40 秒）で同一実行内に吸収する。
# 最悪ケースの所要時間は REQUEST_TIMEOUT × MAX_RETRIES + 40 秒 ≒ 3 分強。
MAX_RETRIES = 5
BACKOFF_FACTOR = 3  # 指数バックオフの係数（待機秒数 = BACKOFF_FACTOR ** 試行回数）

# タイムアウト設定（秒）
REQUEST_TIMEOUT = 30

# DRY RUNモード（True の場合、X への投稿をスキップ）
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ログレベル
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def get_x_credentials() -> dict[str, str]:
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
