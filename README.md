# toshima-tv-outage-notifier

としまテレビの障害情報を監視し、X（Twitter）に自動通知するシステム。

## 機能

- としまテレビの障害情報ページ（https://www.toshima.co.jp/trouble/）を定期的にチェック
- 新規障害の発生時にX（Twitter）へ通知
- 障害のステータス変更（復旧、終了など）時にX（Twitter）へ通知
- 重複通知の防止
- 月間投稿制限の管理（X API Free プラン対応）

## 必要要件

- Python 3.14以上
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

任意（未起動検知用、後述）:

- `HEALTHCHECKS_URL`

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

#### 実行結果の見方

| 終了コード | 意味 | Actions 上の表示 |
|---|---|---|
| 0 | 成功 | 緑 |
| 2 | としまテレビ側に到達できない（タイムアウト/DNS 失敗など一時的な要因） | 緑＋警告アノテーション。状態は保存されず次回実行で再試行 |
| 1 | 通知失敗・ページ解析不能・予期しない例外 | 赤。状態は保存されず次回実行で再試行 |

#### 未起動の検知（Healthchecks.io、任意）

GitHub Actions の `schedule` はベストエフォートで、高負荷時には遅延・間引きされます（実績では 1 日 48 回想定に対し 4〜7 回しか起動しない期間がありました）。
起動しなかった実行は履歴に残らないため、Actions タブを見ても気づけません。
[Healthchecks.io](https://healthchecks.io/)（無料枠あり）に成功時だけ ping を送り、一定時間 ping が途絶えたらアラートを受け取る仕組みを用意しています。

1. Healthchecks.io でチェックを作成し、Period を `30 minutes`、Grace Time を `1 hour` 程度に設定する
2. 表示された Ping URL（`https://hc-ping.com/<uuid>` 形式）をリポジトリのシークレット `HEALTHCHECKS_URL` に登録する
3. 通知先（メール、Slack、Discord など）を Healthchecks 側で設定する

シークレット未設定の場合、このステップはスキップされます。
終了コード 2（上流不通）のときは `/log` に記録のみ送り、期限はリセットしません。上流不通が長引いた場合も未起動と同様にアラートされます。
終了コード 1 や Lint/テスト失敗のときは `/fail` に送るので即時アラートになります。

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
├── tests/                     # テスト（conftest.py に共通フィクスチャ）
├── data/
│   └── state.json             # 状態保存ファイル
├── pyproject.toml             # 依存定義・ツール設定（ruff など）
├── uv.lock                    # 依存ロックファイル
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

## Linting & フォーマット

コード品質管理には [Ruff](https://github.com/astral-sh/ruff) を使用しています。

```bash
# チェック
uv run ruff check .

# 自動修正 + フォーマットをまとめて実行
uv run ruff check --fix . && uv run ruff format .
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
