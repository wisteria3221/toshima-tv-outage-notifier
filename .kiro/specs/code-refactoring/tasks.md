# 実装計画: code-refactoring

- [x] 1. ベースライングリーンの確立
  - リファクタリング前の基準として全テストスイートを実行し、71件が全通過することを記録する
  - `uv run ruff check .` と `uv run ruff format --check .` を実行し、現状で違反・差分がないことを記録する
  - 観測可能な完了条件: pytest 71 passed・ruff 違反0・format 差分0 のベースラインが記録され、以降のリファクタリングの比較基準が確定する
  - _Requirements: 5.1, 5.2, 5.3_

- [ ] 2. コアリファクタリング
- [x] 2.1 (P) スクレイピング用正規表現の定数集約
  - scraper の日付・ステータス・地域判定・詳細リンク/ID抽出の静的正規表現を module レベルの re.compile 済み定数へ集約する
  - 地域キーワード（丁目/付近/地区/町/番地）を単一の文字列定数として定義し、否定判定用と抽出用の両正規表現で共有する
  - 詳細リンク判定とID抽出をキャプチャグループ付きの単一定数に統合する
  - 入力依存の動的パターン（re.escape を使う置換）は定数化しない
  - 観測可能な完了条件: scraper のテスト18件が全通過し、trouble_list.html からの抽出結果（id/date/status/title/area）がリファクタリング前と一致する
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 1.1_
  - _Boundary: scraper 正規表現定数群_

- [x] 2.2 (P) 通知処理の共通ヘルパー抽出
  - 新規障害とステータス変更の通知手順（通知可否判定→投稿→通知済みマーク→カウンタ加算）を単一の共通ヘルパーに抽出し、両ループから呼ぶ
  - ヘルパーは成否の bool のみを返す。投稿関数の差異は引数束縛で吸収し、型は Callable[[], bool] とする
  - notification_sent フラグの更新と成功時ログ出力（新規=1行、ステータス変更=old→new を含む複数行で文面が異なる）は呼び出し側ループに残す
  - 通知成功時のみマーク・カウンタ加算を行い、should_notify_change が False または投稿失敗時はスキップする挙動を保つ
  - 投稿失敗パス（投稿関数が False を返す場合にマーク・加算が呼ばれないこと）が既存テストで未カバーの場合は最小限のテストを追加する
  - 観測可能な完了条件: main のテスト4件が全通過し、新規・ステータス変更の通知フローと成功時ログ文面がリファクタリング前と一致する
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 1.2_
  - _Boundary: _process_notification_

- [x] 2.3 (P) テストフィクスチャの conftest 集約
  - 複数テストで重複定義されている sample_outage と temp_state_file を tests/conftest.py に集約する
  - 移動元の各テストファイル（test_state_manager / test_notifier / test_main）からローカル定義を削除し、ファイル固有フィクスチャは各ファイルに残す
  - 集約するフィクスチャの値は現行と完全一致させる
  - 観測可能な完了条件: conftest.py に共通フィクスチャが単一定義され、重複定義が消え、全71件のテストが引き続き通過する
  - _Requirements: 4.1, 4.2, 4.3_
  - _Boundary: tests/conftest.py_

- [ ] 3. 統合検証・品質ゲート
  - 全リファクタリング適用後に全テストスイートを実行し72件（既存71 + タスク2.2で追加した3.4用テスト1件）が全通過することを確認する
  - `uv run ruff check .`（違反0）と `uv run ruff format --check .`（差分0）を確認する
  - DRY_RUN=true でアプリを実行し、公開エントリポイント・環境変数の挙動が不変であることを確認する
  - 変更コードが型ヒント・日本語コメント・isort 規約の import 順・命名規約を維持していることを確認する
  - 観測可能な完了条件: pytest 72 passed・ruff 違反0・format 差分0・DRY_RUN 実行が正常終了し、スクレイピング結果/通知内容/state.json フォーマット/レート制限の振る舞いがベースラインと一致する
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.1, 5.2, 5.3, 5.4, 4.3_
  - _Depends: 2.1, 2.2, 2.3_

## Implementation Notes
- 環境セットアップ: 初回ベースライン取得時に dev 依存（pytest-mock / responses / ruff）が未インストールで6件のエラーが発生。`uv sync --all-extras` で解消し 71 passed を確認。以降のタスクでも同コマンドで環境を揃えること。
- pre-existing な format 差分: `tests/test_main.py` が `ruff format --check` で1件の差分を持つ（リファクタリング前から存在）。タスク2.3で同ファイル編集後に `ruff format` で解消する想定。
