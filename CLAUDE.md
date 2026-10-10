# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

としまテレビの障害情報ページ (https://www.toshima.co.jp/trouble/) をスクレイピングし、障害の発生やステータス変更時にX（Twitter）へ通知するPythonベースの監視システム。GitHub Actions 上で動作し、外部 cron（cron-job.org）から約30分ごとに起動される。

## 開発コマンド

### セットアップ
```bash
# uv のインストール（未インストールの場合）
# macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 依存パッケージのインストール（dev依存関係含む）
uv sync --all-extras

# 環境変数テンプレートのコピー
cp .env.example .env
# .envを編集してX APIの認証情報を設定
```

### 実行
```bash
# 通常実行（Xへ投稿する）
uv run python -m src.main

# DRY RUN（Xへの投稿をスキップ、テスト用）
DRY_RUN=true uv run python -m src.main
```

### テスト
```bash
# セットアップ（初回のみ）
uv sync --all-extras

# 全テスト実行
uv run pytest

# または、仮想環境を有効化してから実行
pytest

# 特定のテストファイルを実行
uv run pytest tests/test_scraper.py
uv run pytest tests/test_state_manager.py

# 詳細出力
uv run pytest -v

# ログ出力付き
uv run pytest -s
```

### 依存関係の変更
CI は `uv sync --frozen` で実行するため、依存を追加・更新したら必ず `uv.lock` を更新してコミットする（更新し忘れると CI が失敗する）。
```bash
# パッケージを追加（pyproject.toml と uv.lock を同時に更新）
uv add <package>
uv add --optional dev <package>   # dev 依存の場合

# pyproject.toml を直接編集した場合はロックファイルのみ更新
uv lock
```

### Linting & Formatting
```bash
# Ruff でコードをチェック
uv run ruff check .

# 自動修正可能な問題を修正
uv run ruff check --fix .

# コードをフォーマット
uv run ruff format .

# チェックとフォーマットを一度に実行
uv run ruff check --fix . && uv run ruff format .
```

### cc-sdd（SDD スキル）の更新
DevContainer には Node.js が含まれており、`npx` で cc-sdd を実行できる。`.claude/skills/` と `.kiro/` を再生成する。
```bash
# まず --dry-run で差分をプレビュー（既存ファイルを上書きしないか確認）
npx cc-sdd@latest --claude-skills --lang ja --dry-run

# 問題なければ実行
npx cc-sdd@latest --claude-skills --lang ja
```
注意: 実行すると `CLAUDE.md` や `.kiro/steering` も更新されうる。`git diff` でレビューしてからコミットすること。

## アーキテクチャ

### コアデータフロー

1. **スクレイピング** ([src/scraper.py](src/scraper.py)) → としまテレビのWebサイトから障害情報一覧を取得
2. **状態管理** ([src/state_manager.py](src/state_manager.py)) → 現在の障害情報と保存済み状態を比較して変更を検出
3. **通知** ([src/notifier.py](src/notifier.py)) → 変更をX（Twitter）へ投稿
4. **永続化** → 状態を [data/state.json](data/state.json) に保存し、GitHub Actions経由でリポジトリへコミット

### 主要コンポーネント

**`OutageInfo` データクラス** ([src/scraper.py](src/scraper.py))
- 1件の障害を表すコアデータ構造
- フィールド: `id`, `date`, `status`, `title`, `area`, `url`, `last_updated`
- `status` の値: 日付直後の括弧内テキストがそのまま入る（`""` = 括弧なし = 進行中）。典型値は `"終了"`, `"復旧"`, `"完了"`, `"仮復旧"`, `"調査中"` だが固定の列挙ではなく、メンテナンス告知では `"2026年3月12日"` のような予定日が入った実例もある。固定値を前提にした分岐を書かないこと

**状態ファイルフォーマット** ([data/state.json](data/state.json))
- 既知の全障害を通知履歴とともに追跡
- 各障害には `notified_statuses` 配列があり、重複通知を防ぐ
- 月間通知カウンターを含み、レート制限に使用
- 月が変わると自動的にカウンターをリセット（月判定は UTC。ワークフローの `TZ=Asia/Tokyo` は影響しない）

**通知レート制限** ([src/notifier.py](src/notifier.py) の `can_send_notification()` / `should_notify_change()`)
- X API Freeプランは月500ツイートまで
- システムは安全マージンとして月450ツイートに制限（`MONTHLY_TWEET_LIMIT`、Free枠500の90%）
- しきい値は定数化（`_RATE_LIMIT_CRITICAL_RATIO=0.96`, `_RATE_LIMIT_REDUCED_RATIO=0.90`）
- 90%以上使用時: 新規障害のみ通知し、ステータス変更は**破棄**する（`"skipped"`。`update_outages()` で新ステータスが保存されるため、翌月カウンタがリセットされても再検出されない。README の「翌月に遡って通知されることはない」はこの仕様）。96% のしきい値も現状は同じ挙動で、将来の段階的制限用に分けてある
- 送信可否は `can_send_notification()`、変更種別ごとの絞り込みは `should_notify_change()` を参照

### 変更検出ロジック

**新規障害の検出** ([src/state_manager.py](src/state_manager.py) の `get_changes()`)
- 障害IDが保存済み状態に存在しない → 新規障害

**ステータス変更の検出** ([src/state_manager.py](src/state_manager.py) の `get_changes()`)
- 現在のステータスと保存済みステータスを比較
- 新しいステータスが `notified_statuses` 配列に含まれていない場合のみ通知
- ステータスが変更されていない場合の重複通知を防ぐ

**状態更新フロー** ([src/state_manager.py](src/state_manager.py) の `update_outages()` / `mark_notified()`)
- `notified_statuses` を保持しながら既存障害を更新
- 新規障害は空の `notified_statuses` 配列を持つ
- `mark_notified()` は通知成功後にステータスを配列に追加

## 環境変数

X API用（ローカルでは `.env` に、GitHub Actionsではシークレットに設定）。`.env` は [src/config.py](src/config.py) の先頭で `load_dotenv()` により読み込む（`DRY_RUN` や `LOG_LEVEL` は import 時に評価されるため、`main.py` 側で読むと間に合わない）:
- `X_API_KEY` - Consumer Key
- `X_API_SECRET` - Consumer Secret
- `X_ACCESS_TOKEN` - Access Token
- `X_ACCESS_TOKEN_SECRET` - Access Token Secret

オプション:
- `DRY_RUN=true` - Xへの投稿をスキップ（テスト用）
- `LOG_LEVEL=INFO` - ログレベル設定（DEBUG, INFO, WARNING, ERROR）

GitHub Actions のみ（シークレット）:
- `HEALTHCHECKS_URL` - Healthchecks.io の Ping URL（任意。未設定なら死活報告ステップをスキップ）

## GitHub Actions

**ワークフロー**: [.github/workflows/check-outage.yml](.github/workflows/check-outage.yml)
- 外部 cron（cron-job.org）から毎時 `:07` `:37` に `workflow_dispatch` で起動するのが主系。`schedule`（毎時 `:22` `:52`）は GitHub 側で間引かれるためフォールバック
- `concurrency: group: check-outage` で重複起動を直列化（state.json の push 衝突防止）
- Actionsタブから手動実行可能
- `data/state.json` の変更を `[skip ci]` フラグ付きで自動コミット
- 本処理の前に Lint とテストを実行するため、どちらかが失敗すると障害チェック自体が走らない
- リポジトリ設定でシークレットの設定が必要

**Lint ワークフロー**: [.github/workflows/lint.yml](.github/workflows/lint.yml)
- push / PR 時に `ruff check` と `ruff format --check` を実行する
- フォーマット差分だけでも失敗するので、コミット前に `uv run ruff format .` を通しておくこと

## 実装上の重要なポイント

**コーディング規約・環境**
- Python 3.14 以上。バージョンの正本は `.python-version`（CI と Dev Container が参照）
- docstring・コメント・ログメッセージは日本語で書く。ドメインデータは `@dataclass`、テストは対象ごとの `Test...` クラスでグルーピングする。詳細は [.kiro/steering/tech.md](.kiro/steering/tech.md) を参照
- スクレイパーのテストは [tests/fixtures/trouble_list.html](tests/fixtures/trouble_list.html) の保存 HTML に依存する。サイト構造の変更に追従する際はフィクスチャも更新する

**スクレイピング戦略**
- 取得対象は障害情報一覧の 1 ページ目のみ（`fetch_outage_list(max_pages=1)`）
- 日本語テキストから日付、ステータス、タイトル、地域を抽出するために正規表現を使用
- ステータス抽出: テキスト先頭（日付直後）の括弧内テキストのみ（`_RE_STATUS` は `match` で先頭固定）。地理的用語は除外し、タイトル途中の括弧（例「（STB）」）はステータスにしない
- 地域抽出: 括弧内の地理的用語（丁目、付近、地区など）を識別
- リトライロジック: 接続エラー・タイムアウト・5xx は 5回まで指数バックオフ（1/3/9/27 秒）で再試行。1ページ目を取得できなければ `UpstreamUnavailableError`
- 4xx（404 の URL 変更、403 の UA ブロックなど）はリトライしても変わらないので即座に `UpstreamRejectedError`。`main()` は 1 を返す（2 にすると恒久的な破損が警告止まりで埋もれる）

**状態の保持**
- 既存障害の更新時は必ず `notified_statuses` を保持する
- ツイートが実際に成功した場合にのみステータスを通知済みとしてマーク
- 状態ファイルが唯一の信頼できる情報源 - 状態が破損すると重複/欠落通知が発生
- 既存の状態ファイルが読めない・形式が不正な場合は `StateFileError` で停止し `main()` は 1 を返す（初期状態で続行すると全障害を再通知し履歴を上書きするため）。ファイルが存在しない場合だけ初期状態で開始する
- `mark_notified()` は state に存在する障害しかマークしないため、通知ループの**前**に `update_outages()` を呼んで新規障害を state に登録しておく（順序を逆にすると新規障害の `notified_statuses` が常に空になる）

**通知処理の共通化** ([src/main.py](src/main.py))
- 新規障害・ステータス変更の通知は `_process_notification()` ヘルパーに集約
- 戻り値は `"sent"` / `"skipped"`（レート制限等の意図的スキップ）/ `"failed"`（投稿失敗）の3値
- 「通知可否判定 → 投稿 → 通知済みマーク → カウンタ加算」の順序を保証
- 投稿関数は `functools.partial` で束縛し、ループ内ラムダの遅延束縛を回避

**通知失敗をサイレントにしない**
- 投稿が1件でも失敗（`"failed"`）したら `main()` は **1 を返す**。ただし状態は保存する
- 失敗した障害だけ `rollback_outages()` で `update_outages()` 前の内容に戻してから保存する（新規なら削除、ステータス変更なら旧ステータスに戻す）。これで未通知の障害は次回実行で再検出・リトライされ、同じ実行で成功した通知のマークとカウンタは失われない（失われると次回二重投稿になる）
- `update_outages()` の直前に `snapshot_outages()` で控えを取る。この順序を崩すと巻き戻しが効かない
- ワークフローは終了コード `0` と `1` のときに `data/state.json` をコミットし、`2` やステップ異常終了ではコミットしない。ジョブは `1` で失敗扱いになり取りこぼしに気づける
- 過去にX APIの `402 Payment Required`（従量課金のクレジット切れ）で約6ヶ月間サイレントに通知が欠落した事例があり、その再発防止策

**終了コードの使い分け** ([src/main.py](src/main.py) の `EXIT_*` 定数)
- `0`: 成功 / `1`: 通知失敗・ページ解析不能・予期しない例外 / `2`: 上流サイトに到達できない
- `2` は [src/scraper.py](src/scraper.py) の `UpstreamUnavailableError`（1ページ目をリトライ後も取得できない）に対応。としまテレビ側の接続タイムアウトやランナー側の DNS 失敗が原因で、プログラムの不具合ではないためワークフローでは警告のみでジョブは成功扱いにする
- 「ページは取れたが 0 件」は構造変更の疑いがあるので `1`（失敗）のまま。4xx 応答（`UpstreamRejectedError`）も同じ理由で `1`
- 過去 2 ヶ月の Actions 失敗 31 件のうち 30 件がこの上流要因だった（2026-10 調査）

**未起動検知（dead-man's switch）**
- GitHub の `schedule` はベストエフォートで、実績では 1 日 48 回想定に対し 4〜7 回しか起動しない期間があった。起動しなかった実行は履歴に残らず気づけない
- ワークフロー末尾で Healthchecks.io に ping する（シークレット `HEALTHCHECKS_URL`、未設定ならスキップ）。成功時のみ通常 ping、終了コード 2 は `/log`、それ以外は `/fail`

**メッセージフォーマット** ([src/notifier.py](src/notifier.py) の `_format_new_outage_message()` / `_format_status_change_message()`)
- ヘッダーとタイトル行の決定は `_header_and_title_format()` に共通化。復旧/終了/完了（`_RESOLUTION_STATUSES`）なら "【としまテレビ {status}情報】" + "{title} が{status}しました"、それ以外のステータスがあれば既定ヘッダー + "{title}（{status}）"、ステータスなしなら既定ヘッダー + "{title}"
- 既定ヘッダーは新規障害が "【としまテレビ 障害情報】"、ステータス変更が "【としまテレビ 障害情報更新】"（変更後が空文字なら "進行中" と表記）
- 新規障害でも検出時点のステータスを反映する。初検出時に既に「復旧」の障害を進行中として通知すると、復旧が通知済みになり続報が出ないため
- 文字数判定は `weighted_length()`（X の規則: CJK・全角は 2、URL は 23 として数える）で行い、`len()` は使わない。280 を超える場合は `_compose_message()` がタイトルだけを `...` 付きで切り詰め、ヘッダーと「詳細: URL」行は必ず残す

**月のロールオーバー**
- 月が変わると通知カウンターが自動的にリセット
- `increment_notification_count()` と `get_notification_count_this_month()` の両方でチェック


# Agentic SDLC and Spec-Driven Development

Kiro-style Spec-Driven Development on an agentic SDLC

## Project Context

### Paths
- Steering: `.kiro/steering/`
- Specs: `.kiro/specs/`

### Steering vs Specification

**Steering** (`.kiro/steering/`) - Guide AI with project-wide rules and context
**Specs** (`.kiro/specs/`) - Formalize development process for individual features

### Active Specifications
- Check `.kiro/specs/` for active specifications
- Use `/kiro-spec-status [feature-name]` to check progress

## Development Guidelines
- Think in English, generate responses in Japanese. All Markdown content written to project files (e.g., requirements.md, design.md, tasks.md, research.md, validation reports) MUST be written in the target language configured for this specification (see spec.json.language).

## Minimal Workflow
- Phase 0 (optional): `/kiro-steering`, `/kiro-steering-custom`
- Discovery: `/kiro-discovery "idea"` — determines action path, writes brief.md + roadmap.md for multi-spec projects
- Phase 1 (Specification):
  - Single spec: `/kiro-spec-quick {feature} [--auto]` or step by step:
    - `/kiro-spec-init "description"`
    - `/kiro-spec-requirements {feature}`
    - `/kiro-validate-gap {feature}` (optional: for existing codebase)
    - `/kiro-spec-design {feature} [-y]`
    - `/kiro-validate-design {feature}` (optional: design review)
    - `/kiro-spec-tasks {feature} [-y]`
  - Multi-spec: `/kiro-spec-batch` — creates all specs from roadmap.md in parallel by dependency wave
- Phase 2 (Implementation): `/kiro-impl {feature} [tasks] [--review required|inline|off]`
  - Without task numbers: autonomous mode (subagent per task + independent review + final validation)
  - With task numbers: manual mode (selected tasks in main context, still reviewer-gated before completion)
  - `--review off` skips task-local review; use it intentionally and keep `/kiro-validate-impl {feature}` as the final quality gate
  - `/kiro-validate-impl {feature}` (standalone re-validation)
- Progress check: `/kiro-spec-status {feature}` (use anytime)

## Skills Structure
Skills are located in `.claude/skills/kiro-*/SKILL.md`
- Each skill is a directory with a `SKILL.md` file
- Skills run inline with access to conversation context
- Skills may delegate parallel research to subagents for efficiency
- Additional files (templates, examples) can be added to skill directories
- `kiro-review` — task-local adversarial review protocol used by reviewer subagents
- `kiro-debug` — root-cause-first debug protocol used by debugger subagents
- `kiro-verify-completion` — fresh-evidence gate before success or completion claims
- Use skills explicitly requested by the user and skills relevant to the task's domain, including design, accessibility, and UX.
- Select skills from their descriptions or metadata first, then read only the selected skills and the references needed for the task.
- Follow explicit host and project rules and retain required workflow checks. Do not skip relevant skills just because the task is small.

## Development Rules
- 3-phase approval workflow: Requirements → Design → Tasks → Implementation
- Human review required each phase; use `-y` only for intentional fast-track
- Keep steering current and verify alignment with `/kiro-spec-status`
- Follow the user's instructions precisely, and within that scope act autonomously: gather the necessary context and complete the requested work end-to-end in this run, asking questions only when essential information is missing or the instructions are critically ambiguous.

## Steering Configuration
- For spec and implementation work, load the core steering files below from `.kiro/steering/`. Reuse current context rather than rereading unchanged files.
- Load additional steering only when required by project rules or relevant to the task.
- Default files: `product.md`, `tech.md`, `structure.md`
- Custom files are supported (managed via `/kiro-steering-custom`)
