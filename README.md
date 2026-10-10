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

> **Dev Container を使う場合**: VS Code の Dev Containers 拡張でこのリポジトリを開くと、uv・Node.js・gh CLI・Claude Code 入りのコンテナが起動し、依存パッケージのインストール（`uv sync --all-extras`）まで自動で行われます。その場合は手順 2〜3 を省略できます。

### 1. リポジトリをクローン

```bash
git clone https://github.com/wisteria3221/toshima-tv-outage-notifier.git
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

任意の環境変数:

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DRY_RUN` | `false` | `true` にすると X への投稿をスキップする（動作確認用） |
| `LOG_LEVEL` | `INFO` | ログレベル（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |

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

GitHub Actions の `schedule`（毎時 22 分・52 分）と、外部 cron（cron-job.org）からの `workflow_dispatch` 起動（毎時 7 分・37 分）の 2 系統で実行されます。
`schedule` は GitHub 側の都合で遅延・間引きされることがあるため、外部 cron を主系、`schedule` をフォールバックとしています。
両者が重なっても `concurrency` 設定により直列に実行され、状態ファイルのコミットが衝突することはありません。

手動実行する場合は、Actions タブから「Check Toshima TV Outage」ワークフローを選択し、「Run workflow」をクリック。

#### 外部 cron からの起動（cron-job.org）

主系となる外部 cron は、GitHub REST API の `workflow_dispatch` エンドポイントを定期的に叩くことでワークフローを起動します。

1. GitHub で [Fine-grained personal access token](https://github.com/settings/personal-access-tokens) を作成する
   - Repository access: このリポジトリのみ
   - Permissions: `Actions` を `Read and write`
2. [cron-job.org](https://cron-job.org/) でジョブを作成し、以下を設定する
   - URL: `https://api.github.com/repos/wisteria3221/toshima-tv-outage-notifier/actions/workflows/check-outage.yml/dispatches`
   - Method: `POST`
   - Headers:
     - `Authorization: Bearer <作成したトークン>`
     - `Accept: application/vnd.github+json`
   - Body: `{"ref":"main"}`
   - Schedule: 毎時 7 分・37 分
3. トークンには有効期限があるため、期限切れ前に更新する。期限切れになると `schedule` のみのフォールバック運用になり、起動頻度が大きく落ちる（後述の Healthchecks.io で検知できる）

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
├── .claude/                   # Claude Code 用スキル（cc-sdd で生成。settings.local.json は Git 管理外）
├── .devcontainer/             # VS Code Dev Container 定義（uv, Node.js, gh CLI, Claude Code 入り）
├── .github/
│   ├── workflows/
│   │   ├── check-outage.yml   # 障害チェック（schedule / workflow_dispatch）
│   │   └── lint.yml           # push / PR 時の Ruff チェック
│   └── dependabot.yml         # Dev Container の週次更新
├── .kiro/                     # SDD（仕様駆動開発）の steering / specs
├── src/
│   ├── __init__.py
│   ├── config.py              # 設定・定数
│   ├── scraper.py             # スクレイピング
│   ├── state_manager.py       # 状態管理
│   ├── notifier.py            # X API連携
│   └── main.py                # エントリーポイント
├── tests/
│   ├── conftest.py            # 共通フィクスチャ
│   ├── fixtures/              # テスト用 HTML
│   └── test_*.py
├── data/
│   └── state.json             # 状態保存ファイル（Actions が自動コミット）
├── .python-version            # Python バージョンの正本（CI / Dev Container が参照）
├── pyproject.toml             # 依存定義・ツール設定（ruff など）
├── uv.lock                    # 依存ロックファイル
├── .env.example
├── CLAUDE.md                  # Claude Code 向けの開発ガイド
├── LICENSE
└── README.md
```

### 状態ファイル（data/state.json）

既知の障害とその通知履歴（`notified_statuses`）、月間通知カウンターを保持するファイルで、Git 管理対象です。
GitHub Actions は実行が成功（終了コード 0）したときだけ変更を `[skip ci]` 付きで自動コミットします。

- このファイルが通知の唯一の判断基準です。手動で編集・削除すると、既知の障害を再通知したり、通知済みの状態を見失ったりします
- ローカルで `DRY_RUN=true` で実行した場合も、投稿は成功扱いになり「通知済み」のマークと月間カウンターの加算が記録されます。そのままコミットすると本番で通知が欠落するため、`git checkout data/state.json` で戻してください
- 意図的にリセットしたい場合（初期化など）は、`outages` を空にしてコミットしてください。次回実行時に掲載中の障害がすべて新規として通知されます

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

### 復旧通知（ステータスが「復旧」「終了」「完了」に変わったとき）

```
【としまテレビ 復旧情報】
緊急メンテナンス が復旧しました
地域: 池袋本町1丁目付近
詳細: https://www.toshima.co.jp/trouble/detail/91
```

### その他のステータス変更（「仮復旧」「調査中」など）

```
【としまテレビ 障害情報更新】
緊急メンテナンス（仮復旧）
地域: 池袋本町1丁目付近
詳細: https://www.toshima.co.jp/trouble/detail/91
```

いずれも X の文字数規則（日本語は 1 文字を 2 文字、URL は 23 文字として数える）で 280 文字を超える場合、タイトルの末尾が `...` に切り詰められます。ヘッダーと「詳細: URL」の行は常に残ります。

## 制限事項

- X API Free プランの制限（月 500 投稿）を超えないよう、月間 450 投稿で自動制限（毎月 1 日にリセット）
- 月間 405 投稿（90%）以上では新規障害のみ通知し、ステータス変更の通知はスキップ
- 450 投稿に達すると以後は一切通知しない。その間に検出した変更は通知済み扱いにはならないが状態には保存されるため、翌月に遡って通知されることはない

## ライセンス

MIT
