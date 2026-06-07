# 技術スタック

## アーキテクチャ

単一プロセスのバッチ型パイプライン。`main.py` がオーケストレーターとなり、「スクレイピング → 差分検知 → 通知 → 状態保存」の各段階を順に実行する。各段階は専用モジュールに分離され、`src/config.py` が設定値・環境変数・認証情報の単一の供給源となる。

永続化はローカルの JSON ファイル（`data/state.json`）のみで完結し、データベースやサーバーを持たない。実行スケジューリングと状態のコミットは GitHub Actions が担い、状態ファイルが唯一の信頼できる情報源（Source of Truth）となる。

## コア技術

- **言語**: Python（`requires-python >= 3.13`）
- **実行環境**: GitHub Actions（ubuntu-latest）上での無人バッチ実行。ローカルでも CLI 実行可能
- **パッケージ管理**: uv（`uv.lock` でロック。`uv sync --frozen --all-extras`）

## 主要ライブラリ

開発パターンに影響するもののみ:

- **requests + beautifulsoup4**: HTTP取得とHTML解析。スクレイピングの基盤
- **tweepy**: X (Twitter) API クライアント
- **python-dotenv**: ローカルでの `.env` 読み込み（CI ではシークレットを使用）
- テスト用: **pytest** / **responses**（HTTPモック）/ **pytest-mock**

## 開発標準

### 型安全性
- 型ヒントを積極的に付与（`-> int`、`list[OutageInfo]`、`Path | None` など PEP 585/604 の組み込みジェネリクス記法）
- ドメインデータは `@dataclass` で表現する（例: `OutageInfo`, `StatusChange`, `ChangeResult`）

### コード品質
- **Ruff** によるリント・フォーマットを一元化（`pyproject.toml` で設定）
- 有効ルール: `E/W/F/I/N/UP/B/C4/SIM/PIE/RET/ARG`。フォーマットは line-length 88・ダブルクォート
- import の並びは isort 規約（標準ライブラリ → サードパーティ → first-party `src`）に従う
- docstring・コメント・ログメッセージは日本語で記述する

### テスト
- `tests/` 配下に pytest で配置。`testpaths = ["tests"]`
- 外部依存（HTTP・X API・ファイル）はモック化する（`responses`・`pytest-mock`・`tmp_path`）
- テストは「対象クラス／関数ごとの `Test...` クラス」でグルーピングする

## 開発環境

### 必須ツール
- uv（依存解決・実行）、Python 3.13+

### 主要コマンド
```bash
# セットアップ: uv sync --all-extras
# 実行:        uv run python -m src.main
# DRY RUN:     DRY_RUN=true uv run python -m src.main
# テスト:      uv run pytest
# Lint/Format: uv run ruff check --fix . && uv run ruff format .
```

## 重要な技術的判断

- **サーバーレス／DBレス運用**: 状態を Git 管理下の JSON に保存し、GitHub Actions がスケジュール・実行・コミットを担う。インフラコストゼロで運用する
- **環境変数による設定注入**: X API認証・`DRY_RUN`・`LOG_LEVEL` はすべて環境変数経由。認証情報はコードに含めず、CI ではシークレットを使う
- **冪等な通知設計**: 状態ファイルの `notified_statuses` により、再実行・スケジュール重複でも二重通知が起きない
- **防御的なスクレイピング**: 指数バックオフ付きリトライ（`MAX_RETRIES`・`BACKOFF_FACTOR`）とタイムアウト（`REQUEST_TIMEOUT`）で外部サイトの不安定さに備える

---
_標準とパターンを記述し、すべての依存関係を列挙しない_
