# 技術スタック

## アーキテクチャ

単一プロセスのバッチ型パイプライン。`main.py` がオーケストレーターとなり、「スクレイピング → 差分検知 → 通知 → 状態保存」の各段階を順に実行する。各段階は専用モジュールに分離され、`src/config.py` が設定値・環境変数・認証情報の単一の供給源となる。

永続化はローカルの JSON ファイル（`data/state.json`）のみで完結し、データベースやサーバーを持たない。実行スケジューリングと状態のコミットは GitHub Actions が担い、状態ファイルが唯一の信頼できる情報源（Source of Truth）となる。

プロセスとワークフローの間は**終了コードが契約**になっている（`main.py` の `EXIT_*` 定数）。`0` 成功、`1` 気づくべき失敗（通知失敗・解析不能・予期しない例外）、`2` 上流サイトに到達できない一時要因。ワークフローは `0` と `1` のとき状態をコミットし（`1` は成功した通知のマークを守るため）、`2` は警告のみで成功扱い、`1` はジョブ失敗にする。新しい失敗種別を増やすときはこの契約（`main.py` と `check-outage.yml` の両方）を更新する。

## コア技術

- **言語**: Python（`requires-python >= 3.14`。バージョンの正本は `.python-version` で、CI と Dev Container の両方がこれを参照する）
- **実行環境**: GitHub Actions（ubuntu-latest）上での無人バッチ実行。起動は外部 cron（cron-job.org）からの `workflow_dispatch` が主系、GitHub の `schedule` は間引かれるためフォールバック。`concurrency` グループで直列化し、状態ファイルの push 衝突を防ぐ。ローカルでも CLI 実行可能
- **パッケージ管理**: uv（`uv.lock` でロック。CI は `uv sync --frozen` なので、依存を変えたら必ず `uv lock` を通してロックファイルもコミットする）

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
- 少数の固定値を返す関数は `Literal` で戻り値を列挙する（例: `_process_notification` の `"sent" | "skipped" | "failed"`）

### コード品質
- **Ruff** によるリント・フォーマットを一元化（`pyproject.toml` で設定）
- 有効ルール: `E/W/F/I/N/UP/B/C4/SIM/PIE/RET/ARG`。フォーマットは line-length 88・ダブルクォート
- import の並びは isort 規約（標準ライブラリ → サードパーティ → first-party `src`）に従う
- docstring・コメント・ログメッセージは日本語で記述する
- CI では `ruff check` に加えて `ruff format --check` も走る（フォーマット差分だけで失敗する）。障害チェックのワークフローも本処理の前に Lint とテストを実行するため、コミット前に `uv run ruff check --fix . && uv run ruff format .` と `uv run pytest` を通す

### テスト
- `tests/` 配下に pytest で配置。`testpaths = ["tests"]`
- 外部依存（HTTP・X API・ファイル）はモック化する（`responses`・`pytest-mock`・`tmp_path`）
- テストは「対象クラス／関数ごとの `Test...` クラス」でグルーピングする
- 複数ファイルで使う fixture は `tests/conftest.py` に集約する。保存 HTML など固定データは `tests/fixtures/` に置き、サイト構造の変更に追従する際はフィクスチャも更新する
- リトライのスリープは autouse fixture でモックし、テストを待たせない
- `main()` のテストは `src.main.<名前>` のようにモジュール境界でパッチし、終了コードと状態ファイルの有無を検証する

## 開発環境

### 必須ツール
- uv（依存解決・実行）、Python 3.14+
- Dev Container（`.devcontainer/`）を使うと uv・Node.js・gh CLI・Claude Code 入りの環境が立ち上がり、依存インストールまで自動で行われる。Node.js は cc-sdd（`npx cc-sdd@latest`）の実行用

### 主要コマンド
```bash
# セットアップ: uv sync --all-extras
# 実行:        uv run python -m src.main
# DRY RUN:     DRY_RUN=true uv run python -m src.main
# テスト:      uv run pytest
# Lint/Format: uv run ruff check --fix . && uv run ruff format .
```

DRY RUN でもローカルの `data/state.json` には「通知済み」マークとカウンタ加算が書き込まれる。そのままコミットすると本番で通知が欠落するため、確認後は `git checkout data/state.json` で戻す。

## 重要な技術的判断

- **サーバーレス／DBレス運用**: 状態を Git 管理下の JSON に保存し、GitHub Actions がスケジュール・実行・コミットを担う。インフラコストゼロで運用する
- **環境変数による設定注入**: X API認証・`DRY_RUN`・`LOG_LEVEL` はすべて環境変数経由。認証情報はコードに含めず、CI ではシークレットを使う
- **冪等な通知設計**: 状態ファイルの `notified_statuses` により、再実行・スケジュール重複でも二重通知が起きない
- **防御的なスクレイピング**: 指数バックオフ付きリトライ（`MAX_RETRIES`・`BACKOFF_FACTOR`）とタイムアウト（`REQUEST_TIMEOUT`）で外部サイトの不安定さに備える
- **通知失敗をサイレントにしない**: 投稿が1件でも失敗したら `1` で終了してジョブ失敗として可視化する。失敗した障害だけ `update_outages()` 前の内容に巻き戻してから保存するので、次回実行で同じ変更が再検出・再試行され、同じ実行で成功した通知が二重投稿されることもない。過去に X API の `402 Payment Required` で約6ヶ月間気づかれずに通知が欠落した再発防止策
- **上流不通と自分の失敗を区別する**: 1ページ目をリトライ後も取得できない場合は `UpstreamUnavailableError` → 終了コード `2`。としまテレビ側のタイムアウトやランナーの DNS 失敗が原因でプログラムの不具合ではないため、警告のみでジョブは成功扱いにしてノイズを減らす。「ページは取れたが 0 件」と 4xx 応答（`UpstreamRejectedError`、リトライなし）は URL 変更や UA ブロックなど恒久的な破損の疑いなので `1` にする
- **未起動検知（dead-man's switch）**: GitHub の `schedule` はベストエフォートで、起動しなかった実行は履歴に残らない。ワークフロー末尾で Healthchecks.io に ping し（シークレット `HEALTHCHECKS_URL`、未設定ならスキップ）、成功のみ通常 ping、`2` は `/log`、それ以外は `/fail` に送る。これにより「起動していない」「上流不通が長引いている」「失敗している」を区別してアラートできる
- **月間カウンタの境界は UTC**: 月のロールオーバー判定は `datetime.now(UTC)` で行う。ワークフローの `TZ=Asia/Tokyo` はログ表示用で、判定には影響しない

---
_標準とパターンを記述し、すべての依存関係を列挙しない_
_updated_at: 2026-10-10（終了コード契約・失敗可視化・未起動検知・外部 cron 主系・Dev Container を追記）_
