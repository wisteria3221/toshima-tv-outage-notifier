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

# 4xx のうち一時的な要因とみなしてリトライする応答コード。
# 408 Request Timeout / 429 Too Many Requests はリトライで解消しうるため、
# 404 や 403 のような恒久的な拒否とは区別する。scraper と archiver で同じ区分を使う。
TRANSIENT_CLIENT_ERRORS = frozenset({408, 429})

# Wayback Machine（Save Page Now 2）設定
# 障害詳細ページの魚拓を取る保存要求の送り先。ステータス問い合わせも同じ URL 配下。
WAYBACK_SAVE_URL = "https://web.archive.org/save"
# ステータス問い合わせの間隔（秒）。保存は通常 5〜60 秒で終わるので、
# これより短くしても早く終わらず SPN2 側への問い合わせ回数だけ増える。
WAYBACK_POLL_INTERVAL = 5
# 1 件の魚拓に費やす時間の上限（秒）。保存要求（再試行を含む）から完了確認までを
# 通して数え、個々の HTTP タイムアウトも残り時間に切り詰める（requests の timeout は
# 接続と読み取りに別々に効き、本文の受信全体には効かないため、通常は最大 1 要求ぶん
# 超過しうる。応答を少しずつ送り続ける異常なサーバーでは超過に上限は無い。許容する）。
# SPN2 の 1 件あたりの最大所要時間（約 2 分）に合わせる。超えたら諦めて None
# （魚拓なし）とし、通知処理を止めない。
WAYBACK_TIMEOUT = 120
# この秒数内に取られた既存キャプチャは流用せず新規に取る（秒）。
# SPN2 の既定（45 分）のままだと、新規検出から 45 分以内に復旧した場合に
# 検出時の古いキャプチャが返り、復旧後の内容が残らない。実行間隔（10 分）より
# 短くして、変更を検出するたびに新しいキャプチャを作る。
WAYBACK_IF_NOT_ARCHIVED_WITHIN = 60
# 1 回の実行で魚拓に費やす時間の上限（秒）。処理中の件もこの期限で打ち切り、
# 残りの件は諦める。1 件あたり最大 WAYBACK_TIMEOUT かかりうるため、対象が多い実行
# （状態ファイルの作り直し直後など）で実行時間が数十分に延び、Healthchecks の
# 猶予（30 分）を超えて誤報になるのを防ぐ。通常の実行（1〜2 件）には影響しない。
WAYBACK_RUN_BUDGET = 300
# 保存要求が一時的なエラー（5xx・408/429・通信エラー・非 JSON）になったとき、
# 1 回だけ再試行するまでの待機秒数。SPN2 は過負荷時に 503 や HTML を返すことがある。
WAYBACK_SUBMIT_RETRY_WAIT = 5

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


def is_transient_status(status_code: int) -> bool:
    """応答コードが一時的な要因（5xx・408/429）で、リトライしてよいかどうか"""
    return status_code >= 500 or status_code in TRANSIENT_CLIENT_ERRORS


def get_wayback_credentials() -> tuple[str, str]:
    """Wayback Machine の認証情報を環境変数から取得

    (アクセスキー, シークレットキー) の順で返す。未設定は空文字。
    前後の空白・改行は取り除く（`echo $KEY | gh secret set` や CRLF の .env で末尾に
    改行が付くことがあり、そのまま HTTP ヘッダにすると拒否される上、その例外メッセージに
    キーの値が含まれてログに漏れる）。
    get_x_credentials() と同じく呼び出し時に読むので、テストで monkeypatch できる。
    """
    return (
        os.environ.get("WAYBACK_ACCESS_KEY", "").strip(),
        os.environ.get("WAYBACK_SECRET_KEY", "").strip(),
    )
