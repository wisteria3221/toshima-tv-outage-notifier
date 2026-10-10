# プロジェクト構造

## 組織化の方針

責務ごとのレイヤード構成。`src/` 内をパイプラインの各段階（取得・差分検知・通知）に対応するモジュールへ分割し、`main.py` がそれらを順に呼び出すオーケストレーターとなる。設定・定数・認証はすべて `config.py` に集約し、各モジュールはそこから読み取る（設定の単一供給源）。

## ディレクトリパターン

### アプリケーションコード
**場所**: `src/`
**目的**: パイプラインの各段階を1モジュール1責務で配置する
**例**:
- `scraper.py` — HTML取得とパース。`OutageInfo` を生成。取得不能は `UpstreamUnavailableError` で表す
- `state_manager.py` — 保存状態との差分検知・状態更新・通知履歴管理
- `notifier.py` — メッセージ整形とX投稿、レート制限判定
- `config.py` — URL・パス・上限値・環境変数・認証情報
- `main.py` — 全体のオーケストレーション（CLI エントリポイント `python -m src.main`）。終了コード `EXIT_*` を定義し、通知 1 件分の「判定 → 投稿 → マーク → カウンタ」を `_process_notification()` に集約する

### テストコード
**場所**: `tests/`
**目的**: `src/` の各モジュールに 1:1 対応する `test_<module>.py` を置く
**例**: `test_scraper.py`、`test_state_manager.py`、`test_main.py`。複数ファイルで共有する fixture は `conftest.py` に集約し、HTMLサンプルなどの固定データは `tests/fixtures/` に置く

### 永続データ
**場所**: `data/`
**目的**: 実行間で引き継ぐ状態を保持する唯一の信頼できる情報源
**例**: `state.json`（既知障害・通知済みステータス・月間通知カウンター）。GitHub Actions が終了コード `0` のときだけ更新分を `[skip ci]` 付きでコミットする

### 自動化・運用設定
**場所**: `.github/`
**目的**: 障害チェックの実行（`workflows/check-outage.yml`: `schedule` / `workflow_dispatch`、状態コミット、Healthchecks.io への死活報告）・push / PR 時の Lint（`workflows/lint.yml`）・依存更新（`dependabot.yml`、現状は Dev Container の Feature 更新のみ）

### 開発環境定義
**場所**: `.devcontainer/`
**目的**: VS Code Dev Container の定義。Feature のバージョンは `devcontainer-lock.json` で固定し、Dependabot が更新する。Python バージョンは `.python-version` と揃える

### 仕様・ステアリング
**場所**: `.kiro/steering/`（プロジェクト共通のルール）、`.kiro/specs/`（機能ごとの仕様）
**目的**: 仕様駆動開発の文書置き場。コードから読み取れない判断や運用知識はここに残す

## 命名規約

- **ファイル／モジュール**: `snake_case`（例: `state_manager.py`）。テストは `test_` プレフィックス
- **クラス**: `PascalCase`（例: `ToshimaScraper`, `XNotifier`, `StateManager`）
- **データクラス**: ドメイン概念を表す名詞（`OutageInfo`, `StatusChange`, `ChangeResult`）
- **例外**: `...Error` サフィックスで、呼び出し側の扱いが変わる状況ごとに定義する（例: `UpstreamUnavailableError`）
- **関数／変数**: `snake_case`。内部ヘルパーは `_` プレフィックス（例: `_load_state`, `_post_tweet`）
- **定数**: `UPPER_SNAKE_CASE`（例: `MONTHLY_TWEET_LIMIT`, `MAX_RETRIES`, `EXIT_FAILURE`）。モジュール内部でのみ使う閾値や事前コンパイル済み正規表現は `_` プレフィックス（例: `_RATE_LIMIT_REDUCED_RATIO`, `_RE_DATE`）
- **テストクラス**: 対象の関数／メソッド名を `Test` + `PascalCase` にしたもの（例: `TestGetChanges`, `TestFetchWithRetry`）

## import の構成

```python
# 標準ライブラリ → サードパーティ → first-party(src) の順（isort規約）
import logging
import re

import requests
from bs4 import BeautifulSoup

from .config import MAX_RETRIES, TOSHIMA_TROUBLE_URL
```

- `src` 内モジュール間は相対 import（`from .config import ...`）を用いる
- テストからは絶対 import（`from src.scraper import OutageInfo`）を用い、パッチ対象も `src.main.XNotifier` のように使用側モジュールのパスで指定する
- `src` は `known-first-party` として isort に認識させる

## コード組織の原則

- **設定の一元化**: URL・上限・タイムアウト・認証はすべて `config.py` から取得し、各モジュールにハードコードしない
- **依存方向**: `main` → 各モジュール → `config` の一方向。`config` は他モジュールに依存しない
- **副作用の分離**: HTTP・X投稿・ファイルI/Oは専用モジュール（scraper / notifier / state_manager）に閉じ込め、テストでモック可能にする
- **状態は state_manager 経由でのみ操作**: `notified_statuses` の保持や月次リセットなどの整合性ルールを一箇所に集約する
- **終了コードは main とワークフローだけが知る**: 各モジュールは例外や `bool` で結果を返し、終了コードへの変換は `main.py` が行う。ワークフロー（`check-outage.yml`）はその値だけを見て後続ステップを分岐する
- **順序依存はコメントで明示する**: `update_outages()` を通知ループの前に呼ぶ、`mark_notified()` は投稿成功後に呼ぶ、といった入れ替えると静かに壊れる順序は、理由をコメントに書く

---
_ファイルツリーではなくパターンを記述する。パターンに従う新規ファイルの追加では本書の更新を要しない_
_updated_at: 2026-10-10（`.devcontainer/`・`conftest.py`・例外/内部定数の命名・終了コードの責務分担を追記）_
