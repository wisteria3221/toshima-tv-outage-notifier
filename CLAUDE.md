# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

としまテレビの障害情報ページ (https://www.toshima.co.jp/trouble/) をスクレイピングし、障害の発生やステータス変更時にX（Twitter）へ通知するPythonベースの監視システム。GitHub Actionsで30分ごとに自動実行される。

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

## アーキテクチャ

### コアデータフロー

1. **スクレイピング** ([src/scraper.py](src/scraper.py)) → としまテレビのWebサイトから障害情報一覧を取得
2. **状態管理** ([src/state_manager.py](src/state_manager.py)) → 現在の障害情報と保存済み状態を比較して変更を検出
3. **通知** ([src/notifier.py](src/notifier.py)) → 変更をX（Twitter）へ投稿
4. **永続化** → 状態を [data/state.json](data/state.json) に保存し、GitHub Actions経由でリポジトリへコミット

### 主要コンポーネント

**`OutageInfo` データクラス** ([src/scraper.py:24-33](src/scraper.py#L24-L33))
- 1件の障害を表すコアデータ構造
- フィールド: `id`, `date`, `status`, `title`, `area`, `url`, `last_updated`
- `status` の値: `"終了"`, `"復旧"`, `"完了"`, `"仮復旧"`, `"調査中"`, または `""` (空文字 = 進行中)

**状態ファイルフォーマット** ([data/state.json](data/state.json))
- 既知の全障害を通知履歴とともに追跡
- 各障害には `notified_statuses` 配列があり、重複通知を防ぐ
- 月間通知カウンターを含み、レート制限に使用
- 月が変わると自動的にカウンターをリセット

**通知レート制限** ([src/notifier.py:200-225](src/notifier.py#L200-L225))
- X API Freeプランは月500ツイートまで
- システムは安全マージンとして月450ツイートに制限（`MONTHLY_TWEET_LIMIT`）
- 96%使用時: 新規障害のみ通知
- 90%使用時: 新規障害のみ通知（ステータス変更は通知しない）
- ロジックは `should_notify_change()` 関数を参照

### 変更検出ロジック

**新規障害の検出** ([src/state_manager.py:122-125](src/state_manager.py#L122-L125))
- 障害IDが保存済み状態に存在しない → 新規障害

**ステータス変更の検出** ([src/state_manager.py:128-146](src/state_manager.py#L128-L146))
- 現在のステータスと保存済みステータスを比較
- 新しいステータスが `notified_statuses` 配列に含まれていない場合のみ通知
- ステータスが変更されていない場合の重複通知を防ぐ

**状態更新フロー** ([src/state_manager.py:150-185](src/state_manager.py#L150-L185))
- `notified_statuses` を保持しながら既存障害を更新
- 新規障害は空の `notified_statuses` 配列を持つ
- `mark_notified()` は通知成功後にステータスを配列に追加

## 環境変数

X API用（ローカルでは `.env` に、GitHub Actionsではシークレットに設定）:
- `X_API_KEY` - Consumer Key
- `X_API_SECRET` - Consumer Secret
- `X_ACCESS_TOKEN` - Access Token
- `X_ACCESS_TOKEN_SECRET` - Access Token Secret

オプション:
- `DRY_RUN=true` - Xへの投稿をスキップ（テスト用）
- `LOG_LEVEL=INFO` - ログレベル設定（DEBUG, INFO, WARNING, ERROR）

## GitHub Actions

**ワークフロー**: [.github/workflows/check-outage.yml](.github/workflows/check-outage.yml)
- 30分ごとに実行（UTC時刻の毎時 `:00` と `:30`）
- Actionsタブから手動実行可能
- `data/state.json` の変更を `[skip ci]` フラグ付きで自動コミット
- リポジトリ設定でシークレットの設定が必要

## 実装上の重要なポイント

**スクレイピング戦略**
- 日本語テキストから日付、ステータス、タイトル、地域を抽出するために正規表現を使用
- ステータス抽出: 日付の後の括弧内テキストを探す。地理的用語は除外
- 地域抽出: 括弧内の地理的用語（丁目、付近、地区など）を識別
- リトライロジック: 3回まで指数バックオフで再試行

**状態の保持**
- 既存障害の更新時は必ず `notified_statuses` を保持する
- ツイートが実際に成功した場合にのみステータスを通知済みとしてマーク
- 状態ファイルが唯一の信頼できる情報源 - 状態が破損すると重複/欠落通知が発生

**メッセージフォーマット** ([src/notifier.py:77-133](src/notifier.py#L77-L133))
- 新規障害: "【としまテレビ 障害情報】"
- 復旧/終了/完了へのステータス変更: "【としまテレビ {status}情報】"
- その他のステータス変更: "【としまテレビ 障害情報更新】"
- 必要に応じて280文字に切り詰め

**月のロールオーバー**
- 月が変わると通知カウンターが自動的にリセット
- `increment_notification_count()` と `get_notification_count_this_month()` の両方でチェック
