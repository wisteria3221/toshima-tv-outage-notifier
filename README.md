# toshima-tv-outage-notifier

としまテレビの障害情報を監視し、X（Twitter）に自動通知するシステム。

## 機能

- としまテレビの障害情報ページ（https://www.toshima.co.jp/trouble/）を定期的にチェック
- 新規障害の発生時にX（Twitter）へ通知
- 障害のステータス変更（復旧、終了など）時にX（Twitter）へ通知
- 重複通知の防止
- 月間投稿制限の管理（X API Free プラン対応）
- 検出した障害（通知がスキップされた件を含む）の詳細ページを Wayback Machine に保存（魚拓、任意）

## 必要要件

- Python 3.14以上
- X Developer アカウント（Free プラン以上）
- archive.org アカウント（任意。障害詳細ページの魚拓を取る場合）

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
| `WAYBACK_ACCESS_KEY` | （なし） | 任意。`WAYBACK_SECRET_KEY` と両方設定すると、障害詳細ページの魚拓を Wayback Machine に取る（後述）。未設定なら魚拓だけスキップし、通知は通常どおり行う |
| `WAYBACK_SECRET_KEY` | （なし） | 任意。archive.org の S3 シークレットキー。`WAYBACK_ACCESS_KEY` とセットで設定する |

#### GitHub Actions の場合

リポジトリの Settings > Secrets and variables > Actions で以下のシークレットを設定:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

任意（未起動検知用、後述）:

- `HEALTHCHECKS_URL`

任意（魚拓用、後述）:

- `WAYBACK_ACCESS_KEY`
- `WAYBACK_SECRET_KEY`

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

GitHub Actions の `schedule`（毎時 27 分）と、外部 cron（cron-job.org）からの `workflow_dispatch` 起動（10 分ごと、毎時 7 分・17 分・…・57 分）の 2 系統で実行されます。
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
   - Schedule: 10 分ごと（毎時 7, 17, 27, 37, 47, 57 分）
3. トークンには有効期限があるため、期限切れ前に更新する。期限切れになると `schedule` のみのフォールバック運用になり、起動頻度が大きく落ちる（後述の Healthchecks.io で検知できる）

cron-job.org の設定確認・変更（実行履歴の確認、トークンの差し替え、スケジュール変更、一時停止など）は、Claude Code 用スキル [.claude/skills/cron-job-org-api/](.claude/skills/cron-job-org-api/) の同梱 CLI でも行えます（API キーは cron-job.org Console の Settings で発行し、環境変数 `CRONJOB_API_KEY` か `.env` に設定。API は 1 日 100 リクエストまで）。

```bash
CJ="python3 .claude/skills/cron-job-org-api/scripts/cj.py"
$CJ jobs list --table --tz Asia/Tokyo                       # jobId・スケジュール・最終結果
$CJ history list <jobId> --table --tz Asia/Tokyo            # 直近の実行（成功なら HTTP 204）と次回予定
$CJ jobs update <jobId> --header "Authorization: Bearer $NEW_GITHUB_TOKEN"   # トークン差し替え（他のヘッダーと本文は保持）
```

#### 実行結果の見方

| 終了コード | 意味 | Actions 上の表示 |
|---|---|---|
| 0 | 成功 | 緑 |
| 2 | としまテレビ側に到達できない（タイムアウト/DNS 失敗/5xx/408/429 など一時的な要因） | 緑＋警告アノテーション。状態は保存されず次回実行で再試行 |
| 1 | 通知失敗・ページ解析不能・ページが 4xx（408/429 以外）で拒否された・状態ファイル破損・予期しない例外 | 赤。通知に成功した分の状態は保存・コミットされ、失敗した障害だけ次回実行で再試行 |

#### 未起動の検知（Healthchecks.io、任意）

GitHub Actions の `schedule` はベストエフォートで、高負荷時には遅延・間引きされます（実績では 1 日 48 回想定に対し 4〜7 回しか起動しない期間がありました）。
起動しなかった実行は履歴に残らないため、Actions タブを見ても気づけません。
[Healthchecks.io](https://healthchecks.io/)（無料枠あり）に成功時だけ ping を送り、一定時間 ping が途絶えたらアラートを受け取る仕組みを用意しています。

1. Healthchecks.io でチェックを作成し、Period を `10 minutes`（外部 cron の間隔）、Grace Time を `30 minutes` 程度に設定する
2. 表示された Ping URL（`https://hc-ping.com/<uuid>` 形式）をリポジトリのシークレット `HEALTHCHECKS_URL` に登録する
3. 通知先（メール、Slack、Discord など）を Healthchecks 側で設定する

ダッシュボードを使わず Management API で行う場合は、Claude Code 用スキル [.claude/skills/healthchecks-api/](.claude/skills/healthchecks-api/) の同梱 CLI が使えます（API キーは Healthchecks のプロジェクト設定で発行し、環境変数 `HEALTHCHECKS_API_KEY` か `.env` に設定）。

```bash
HC="python3 .claude/skills/healthchecks-api/scripts/hc.py"
$HC checks create --name toshima-tv-outage-notifier --slug toshima-tv-outage-notifier \
  --timeout 3600 --grace 1800 --channels '*' --unique slug      # 再実行しても重複しない
PING_URL=$($HC checks list --slug toshima-tv-outage-notifier \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"][0]["ping_url"])')
gh secret set HEALTHCHECKS_URL --body "$PING_URL"
```

シークレット未設定の場合、このステップはスキップされます。
終了コード 2（上流不通）のときは `/log` に記録のみ送り、期限はリセットしません。上流不通が長引いた場合も未起動と同様にアラートされます。
終了コード 1 や依存インストール失敗などのときは `/fail` に送るので即時アラートになります。

### 障害詳細ページの魚拓（Wayback Machine、任意）

新規障害の検出時と各ステータス変更の検出時に、その障害の詳細ページ（`https://www.toshima.co.jp/trouble/detail/<id>`）を [Wayback Machine](https://web.archive.org/) に保存し、魚拓の URL を状態ファイルに記録します。公式ページが後から書き換えられたり削除されたりしても、通知時点の内容を第三者のアーカイブで参照できます。
魚拓はすべての通知が終わった後に取るため、ツイートが遅れることはありません。魚拓の URL はツイートには載せません（`data/state.json` から参照します）。

#### 認証情報の取得と設定

Wayback Machine の Save Page Now は匿名では利用できないため、archive.org の S3 キーで認証します。

1. [archive.org](https://archive.org/account/signup) で無料アカウントを作成する
2. ログインした状態で https://archive.org/account/s3.php を開き、S3 API キー（Access Key と Secret Key）を発行する
3. 取得したキーを設定する
   - ローカル実行: `.env` に追記する

     ```bash
     WAYBACK_ACCESS_KEY=your_access_key_here
     WAYBACK_SECRET_KEY=your_secret_key_here
     ```

   - GitHub Actions: リポジトリの Settings > Secrets and variables > Actions でシークレット `WAYBACK_ACCESS_KEY` と `WAYBACK_SECRET_KEY` を登録する（`gh secret set WAYBACK_ACCESS_KEY` などでも可）。ワークフローは Run outage checker ステップの `env` でこの 2 つを渡します

両方が設定されているときだけ魚拓を取ります。未設定（または片方だけ）の場合は、魚拓の対象があった実行で「魚拓の認証情報が未設定のため取得をスキップします」と警告を 1 回出し、魚拓だけをスキップします。通知には影響しません。

#### 記録の場所と形式

魚拓の記録は `data/state.json` の各障害エントリの `archives` 配列に追記され、他の状態と一緒に GitHub Actions がコミットします。要素は魚拓を取った時点のステータス（`status`、空文字は進行中）、取得日時（`archived_at`、UTC）、魚拓の公開 URL（`url`）の 3 つで、ステータスが変わるたびに 1 件ずつ増えます。

```json
"archives": [
  {
    "status": "",
    "archived_at": "2026-10-02T05:13:25.000000+00:00",
    "url": "https://web.archive.org/web/20261002051318/https://www.toshima.co.jp/trouble/detail/121"
  },
  {
    "status": "復旧",
    "archived_at": "2026-10-02T07:32:58.000000+00:00",
    "url": "https://web.archive.org/web/20261002073250/https://www.toshima.co.jp/trouble/detail/121"
  }
]
```

#### ベストエフォートであること

- 魚拓の失敗（Wayback Machine 側の過負荷・レート制限・時間切れなど）は WARNING ログに残すだけで、次回以降の実行で再試行はしません（1 件の処理の中では、保存要求の一時的なエラーを 1 回だけ再試行し、完了確認の一時的なエラーは上限時間内で問い合わせを続けます）。終了コードや Healthchecks への報告にも影響せず、ジョブは成功のまま終わります。失敗した魚拓は記録されず、後から自動で補われることもありません
- 詳細ページが既に削除されて 404 になっている場合は、エラーページを魚拓として記録しません
- 1 件あたりの待ち時間は最大 2 分（保存要求から完了確認までを通した上限。Wayback Machine 側の最大所要時間に合わせたもの）です。通常の実行で対象になるのは 1〜2 件です。1 回の実行で魚拓に費やす時間は 5 分までで、処理中の件もその時点で打ち切り、残りは諦めて「時間予算を超えたため、残り n 件の取得を諦めます」と警告します（通知や状態の保存には影響しません）
- 通知に失敗した障害は魚拓の対象になりません（次回実行で再検出されたときに取ります）
- `DRY_RUN=true` のときは保存要求を送らず、「[DRY RUN] 魚拓の取得を n 件スキップしました」とログに出します
- 実行結果はログで確認します。「魚拓を n 件取得、m 件失敗」の行が m > 0 のときは WARNING になり、各件の失敗理由はその前の行に出ています

## ディレクトリ構成

```
toshima-tv-outage-notifier/
├── .claude/                   # Claude Code 用スキル（cc-sdd で生成。settings.local.json は Git 管理外）
├── .devcontainer/             # VS Code Dev Container 定義（uv, Node.js, gh CLI, Claude Code 入り）
├── .github/
│   ├── workflows/
│   │   ├── check-outage.yml   # 障害チェック（schedule / workflow_dispatch）
│   │   └── lint.yml           # push / PR 時の Ruff チェックとテスト
│   └── dependabot.yml         # Dev Container / GitHub Actions / Python 依存の週次更新
├── .kiro/                     # SDD（仕様駆動開発）の steering / specs
├── src/
│   ├── __init__.py
│   ├── config.py              # 設定・定数
│   ├── scraper.py             # スクレイピング
│   ├── state_manager.py       # 状態管理
│   ├── notifier.py            # X API連携
│   ├── archiver.py            # Wayback Machine への魚拓取得
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

既知の障害とその通知履歴（`notified_statuses`）、魚拓の記録（`archives`、前述）、月間通知カウンターを保持するファイルで、Git 管理対象です。
GitHub Actions は終了コードが 0 または 1 のとき変更を `[skip ci]` 付きで自動コミットします（1 のときは通知に成功した分だけが保存され、失敗した障害は次回実行で再検出されます）。

- このファイルが通知の唯一の判断基準です。手動で編集・削除すると、既知の障害を再通知したり、通知済みの状態を見失ったりします
- 投稿後に状態のコミットが失われるなどで同じ本文を再投稿しようとした場合、X は 403（duplicate content）で拒否します。このときは「既に投稿済み」として通知済みマークだけを付け、失敗にはしません
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

初めて検出した時点で既にステータスが付いている障害は、そのステータスを反映して通知します（「復旧」などの解決系なら下の復旧通知と同じ形式、「仮復旧」などはタイトルに `（仮復旧）` を付記）。

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
