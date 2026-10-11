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

# Wayback Machine（Save Page Now 2）設定
# 障害詳細ページの魚拓を取る保存要求の送り先。ステータス問い合わせも同じ URL 配下。
WAYBACK_SAVE_URL = "https://web.archive.org/save"
# ステータス問い合わせの間隔（秒）。保存は通常 5〜60 秒で終わるので、
# これより短くしても早く終わらず SPN2 側への問い合わせ回数だけ増える。
WAYBACK_POLL_INTERVAL = 5
# 保存要求から完了までの上限（秒）。SPN2 の 1 件あたりの最大所要時間（約 2 分）に
# 合わせる。超えたら諦めて None（魚拓なし）とし、通知処理を止めない。
WAYBACK_TIMEOUT = 120
# この秒数内に取られた既存キャプチャは流用せず新規に取る（秒）。
# SPN2 の既定（1 時間）のままだと、新規検出から 1 時間以内に復旧した場合に
# 検出時の古いキャプチャが返り、復旧後の内容が残らない。実行間隔（10 分）より
# 短くして、変更を検出するたびに新しいキャプチャを作る。
WAYBACK_IF_NOT_ARCHIVED_WITHIN = 60

# DRY RUNモード（True の場合、X への投稿をスキップ）
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ログレベル
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def get_x_credentials() -> dict[str, str]:
    """X API認証情報を環境変数から取得

    キー名は tweepy.Client のキーワード引数に合わせてあり、そのまま展開して渡せる。
    """
    return {
        "consumer_key": os.environ.get("X_API_KEY", ""),
        "consumer_secret": os.environ.get("X_API_SECRET", ""),
        "access_token": os.environ.get("X_ACCESS_TOKEN", ""),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    }


def get_wayback_credentials() -> tuple[str, str]:
    """Wayback Machine の認証情報を環境変数から取得

    (アクセスキー, シークレットキー) の順で返す。未設定は空文字。
    get_x_credentials() と同じく呼び出し時に読むので、テストで monkeypatch できる。
    """
    return (
        os.environ.get("WAYBACK_ACCESS_KEY", ""),
        os.environ.get("WAYBACK_SECRET_KEY", ""),
    )
