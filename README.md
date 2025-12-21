# toshima-tv-outage-notifier

としまテレビの障害情報を監視し、X（Twitter）に自動通知するシステム。

## 機能

- としまテレビの障害情報ページ（https://www.toshima.co.jp/trouble/）を定期的にチェック
- 新規障害の発生時にX（Twitter）へ通知
- 障害のステータス変更（復旧、終了など）時にX（Twitter）へ通知
- 重複通知の防止
- 月間投稿制限の管理（X API Free プラン対応）

## 必要要件

- Python 3.13以上
- X Developer アカウント（Free プラン以上）

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/your-username/toshima-tv-outage-notifier.git
cd toshima-tv-outage-notifier
```

### 2. uv のインストール

このプロジェクトはパッケージ管理に [uv](https://github.com/astral-sh/uv) を使用しています。

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# または pipx 経由
pipx install uv
```

### 3. 依存パッケージをインストール

```bash
uv sync --all-extras
```

### 4. X API 認証情報を取得

1. [X Developer Portal](https://developer.x.com/) でアカウントを作成
2. Free tier プランを選択
3. 新しいアプリを作成
4. User authentication settings で「Read and Write」権限を設定
5. Keys and tokens タブから以下の4つを取得:
   - API Key (Consumer Key)
   - API Secret (Consumer Secret)
   - Access Token
   - Access Token Secret

### 5. 環境変数を設定

#### ローカル実行の場合

`.env.example` をコピーして `.env` を作成し、認証情報を設定:

```bash
cp .env.example .env
```

```bash
X_API_KEY=your_api_key_here
X_API_SECRET=your_api_secret_here
X_ACCESS_TOKEN=your_access_token_here
X_ACCESS_TOKEN_SECRET=your_access_token_secret_here
```

#### GitHub Actions の場合

リポジトリの Settings > Secrets and variables > Actions で以下のシークレットを設定:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

## 使い方

### ローカルで実行

```bash
uv run python -m src.main
```

### DRY RUN モード（X への投稿をスキップ）

```bash
DRY_RUN=true uv run python -m src.main
```

### GitHub Actions での自動実行

リポジトリを GitHub にプッシュすると、30分ごとに自動実行されます。

手動実行する場合は、Actions タブから「Check Toshima TV Outage」ワークフローを選択し、「Run workflow」をクリック。

## ディレクトリ構成

```
toshima-tv-outage-notifier/
├── .github/workflows/
│   └── check-outage.yml       # GitHub Actions ワークフロー
├── src/
│   ├── __init__.py
│   ├── config.py              # 設定・定数
│   ├── scraper.py             # スクレイピング
│   ├── state_manager.py       # 状態管理
│   ├── notifier.py            # X API連携
│   └── main.py                # エントリーポイント
├── tests/                     # テスト
├── data/
│   └── state.json             # 状態保存ファイル
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── README.md
```

## テスト

```bash
# セットアップ（初回のみ）
uv sync --all-extras

# 全テスト実行
uv run pytest

# または、仮想環境を有効化してから実行
pytest
```

## 通知メッセージ例

### 新規障害

```
【としまテレビ 障害情報】
緊急メンテナンス
日時: 2025.12.09
地域: 池袋本町1丁目付近
詳細: https://www.toshima.co.jp/trouble/detail/91
```

### 復旧通知

```
【としまテレビ 復旧情報】
緊急メンテナンス が復旧しました
地域: 池袋本町1丁目付近
詳細: https://www.toshima.co.jp/trouble/detail/91
```

## 制限事項

- X API Free プランの制限（月500投稿）を超えないよう、月間450投稿で自動制限
- 制限に近づくと新規障害のみ通知するよう自動調整

## ライセンス

MIT
