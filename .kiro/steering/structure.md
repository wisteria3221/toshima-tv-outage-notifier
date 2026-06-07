# プロジェクト構造

## 組織化の方針

責務ごとのレイヤード構成。`src/` 内をパイプラインの各段階（取得・差分検知・通知）に対応するモジュールへ分割し、`main.py` がそれらを順に呼び出すオーケストレーターとなる。設定・定数・認証はすべて `config.py` に集約し、各モジュールはそこから読み取る（設定の単一供給源）。

## ディレクトリパターン

### アプリケーションコード
**場所**: `src/`
**目的**: パイプラインの各段階を1モジュール1責務で配置する
**例**:
- `scraper.py` — HTML取得とパース。`OutageInfo` を生成
- `state_manager.py` — 保存状態との差分検知・状態更新・通知履歴管理
- `notifier.py` — メッセージ整形とX投稿、レート制限判定
- `config.py` — URL・パス・上限値・環境変数・認証情報
- `main.py` — 全体のオーケストレーション（CLI エントリポイント `python -m src.main`）

### テストコード
**場所**: `tests/`
**目的**: `src/` の各モジュールに 1:1 対応する `test_<module>.py` を置く
**例**: `test_scraper.py`、`test_state_manager.py`。HTMLサンプルなどの固定データは `tests/fixtures/` に置く

### 永続データ
**場所**: `data/`
**目的**: 実行間で引き継ぐ状態を保持する唯一の信頼できる情報源
**例**: `state.json`（既知障害・通知済みステータス・月間通知カウンター）。GitHub Actions が更新分をコミットする

### 自動化・運用設定
**場所**: `.github/`
**目的**: スケジュール実行（`workflows/check-outage.yml`）・Lint用ワークフロー・依存更新（dependabot）

## 命名規約

- **ファイル／モジュール**: `snake_case`（例: `state_manager.py`）。テストは `test_` プレフィックス
- **クラス**: `PascalCase`（例: `ToshimaScraper`, `XNotifier`, `StateManager`）
- **データクラス**: ドメイン概念を表す名詞（`OutageInfo`, `StatusChange`, `ChangeResult`）
- **関数／変数**: `snake_case`。内部ヘルパーは `_` プレフィックス（例: `_load_state`, `_post_tweet`）
- **定数**: `UPPER_SNAKE_CASE`（例: `MONTHLY_TWEET_LIMIT`, `MAX_RETRIES`）

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
- `src` は `known-first-party` として isort に認識させる

## コード組織の原則

- **設定の一元化**: URL・上限・タイムアウト・認証はすべて `config.py` から取得し、各モジュールにハードコードしない
- **依存方向**: `main` → 各モジュール → `config` の一方向。`config` は他モジュールに依存しない
- **副作用の分離**: HTTP・X投稿・ファイルI/Oは専用モジュール（scraper / notifier / state_manager）に閉じ込め、テストでモック可能にする
- **状態は state_manager 経由でのみ操作**: `notified_statuses` の保持や月次リセットなどの整合性ルールを一箇所に集約する

---
_ファイルツリーではなくパターンを記述する。パターンに従う新規ファイルの追加では本書の更新を要しない_
