# ギャップ分析 (Gap Analysis): code-refactoring

調査日: 2026-06-07
対象: 既存コードベース (src/ 全5モジュール, tests/ 全4テストファイル)

## 1. 分析サマリ

- 本specは新規実装ではなく**純粋なリファクタリング**であり、ギャップは「機能の欠如」ではなく「現状コードと目標構造の差分」として現れる。すべて既存パターン内で完結し、外部依存・アーキテクチャ変更は不要。
- 最優先3項目（正規表現定数化・通知ループ共通化・フィクスチャ共通化）はいずれも**低リスク・小工数**で、既存の責務分離・命名規約・テスト構成にそのまま収まる。
- 中優先2項目のうち、**状態スキーマのデータクラス化は後方互換のリスク**（`data/state.json` のフォーマット維持）があり要注意。メッセージ整形共通化は低リスク。
- **重要な発見**: 要件記載の「62件のテスト」は実態と不一致。`pytest --collect-only` で確認した実際のテスト件数は **71件**（main:4 / state_manager:23 / notifier:26 / scraper:18）。成功条件の数値を71件へ修正すべき。
- 振る舞い不変性の保証は、既存の71件テストが回帰スイートとして十分に機能するため、各リファクタリング後にテスト全通過を確認する運用で担保できる。

## 2. Requirement-to-Asset Map（要件→資産対応とギャップ）

| 要件 | 現状資産 | ギャップ種別 | 詳細 |
|------|---------|------------|------|
| R2 正規表現定数化 | `scraper.py` に module 定数なし。正規表現は全8箇所インライン（行122,151,157,189-190,196,222,227-228,235） | Constraint（既存パターン内で解消可） | 地域キーワード `丁目\|付近\|地区\|町\|番地` が行196と行227-228で重複。全角/半角括弧 `[（(]...[）)]` が行190,222,228,235で重複 |
| R3 通知ループ共通化 | `main.py` 行76-82（新規）と行85-94（変更）がほぼ同一構造 | Constraint | 相違は3点のみ: ①notify関数名 `notify_new_outage` vs `notify_status_change` ②`should_notify_change` 引数 `"new"` vs `"status_change"` ③`mark_notified` 第2引数 `outage.status` vs `change.new_status`。`notification_sent` フラグ（行73,81,90,106）の扱いに注意 |
| R4 フィクスチャ共通化 | `tests/conftest.py` は**不在**。`sample_outage` が3ファイルで重複定義（test_state_manager.py:15-25 / test_notifier.py:16-26 / test_main.py:10-20）、内容は完全同一。`temp_state_file` が2ファイルで重複（test_state_manager.py:9-12 / test_notifier.py:10-13）、内容同一 | Missing（conftest.py新規作成） | fixtures/ ディレクトリは既存（trouble_list.html）。conftest.py 追加で pytest が自動認識 |
| R5 状態データクラス化 | `state_manager.py` は dict を直接操作。`_create_initial_state()` 行80-93、エントリ構造 行204-214。state キーのハードコード参照が22箇所（行73〜280）。`asdict` 不使用で OutageInfo→dict は手動変換。`schema_version="1.1"` | Constraint + Risk | dataclass化しても JSON I/O（行70-71 load / 行112-113 dump, `ensure_ascii=False, indent=2`）の入出力フォーマットを完全維持する必要。`notified_statuses` は **list**（set化すると JSON 順序・型が変わり後方互換を破る恐れ） |
| R6 メッセージ整形共通化 | `notifier.py` `_format_new_outage_message()` 行85-108 と `_format_status_change_message()` 行110-141 | Constraint | 共通: 地域付与・URL付与・`_truncate_message()`（行143-160, 280字）。相違: ヘッダー（固定 vs 条件分岐 行123-128）、日時行（新規のみ 行100）、タイトル/ステータス文言 |
| R1 振る舞い不変性 | 既存71件テスト + `tests/fixtures/trouble_list.html` | 充足（回帰スイートとして機能） | DRY_RUN 経路・responses モックで HTTP/X API を遮断済み |
| R7 品質ゲート | Ruff 設定（pyproject.toml）, pytest 設定既存 | 充足 | `ruff check` / `ruff format` / `pytest` を完了判定に組込む |

## 3. 実装アプローチの選択肢

### R2（正規表現定数化）
- **Option A（推奨）: scraper.py 内に module 定数を定義**
  - module 冒頭に `_RE_*`（`re.compile` 済み）と地域キーワード定数を集約。各関数は定数を参照。
  - ✅ 既存の「設定一元化」方針と整合 ✅ 工数最小 ❌ なし
  - 補足: 地域キーワードは `_AREA_KEYWORDS = "丁目|付近|地区|町|番地"` を単一定義し、両正規表現で共有。`re.escape(status)`/`re.escape(area)` を使う動的パターン（行222,235）は定数化対象外（入力依存のため）。

### R3（通知ループ共通化）
- **Option A（推奨）: main.py 内にローカルヘルパー関数を抽出**
  - `_process_notification(state_manager, notifier_fn, change_type, outage_id, status) -> bool` 相当を main.py に追加し、両ループから呼ぶ。
  - ✅ オーケストレーション責務は main.py に閉じる ✅ structure.md の依存方向を維持 ❌ なし
- Option B: notifier 側へ移動 → ❌ notifier に状態管理(mark_notified/increment)を持ち込み責務が混ざるため非推奨。

### R4（フィクスチャ共通化）
- **Option A（推奨）: tests/conftest.py を新規作成**
  - 共通の `sample_outage`・`temp_state_file` を移動し、各ファイルのローカル定義を削除。ファイル固有フィクスチャ（`sample_outage_with_status` 等）は各ファイルに残す。
  - ✅ pytest 標準機構 ✅ 重複解消 ❌ なし

### R5（状態データクラス化）— 中優先・条件付き
- **Option A: dataclass + シリアライズ層を導入**（型安全性向上）
  - `OutageEntry`/`StateSchema` を定義し、load 時 dict→dataclass、save 時 dataclass→dict。
  - ✅ 型安全・可読性向上 ❌ JSON 入出力の完全一致検証が必須・工数中・後方互換リスク
- **Option B（推奨・保守的）: 今回はスキーマ化を見送り、キー名を定数化するに留める**
  - state キー文字列を module 定数（`_KEY_OUTAGES` 等）に集約してハードコード散在のみ解消。
  - ✅ 後方互換リスクほぼゼロ・工数小 ✅ R5 AC-3（互換を損なうなら現行維持）に合致 ❌ 型安全性の改善幅は限定的
  - **判断材料**: R5 は「後方互換を厳守できる場合のみ」の条件付き要件。リスクと工数を踏まえ design フェーズで A/B を決定。

### R6（メッセージ整形共通化）
- **Option A（推奨）: 共通の末尾組み立てヘルパーを抽出**
  - 「地域行＋URL行を追加して結合し `_truncate_message()` する」共通部分を `_finalize_message(lines, area, url)` 相当に抽出。ヘッダー/日時/タイトルの差分は各関数に残す。
  - ✅ 文面を変えずに重複のみ削減 ❌ なし（出力一致のスナップショット的テストで担保）

## 4. 工数とリスク

| 要件 | 工数 | リスク | 根拠 |
|------|------|--------|------|
| R2 正規表現定数化 | S | Low | 既存パターン内・テスト18件が抽出結果を保証 |
| R3 通知ループ共通化 | S | Low | 純粋な抽出・main テスト4件 + 手動DRY_RUNで確認可 |
| R4 フィクスチャ共通化 | S | Low | pytest 標準機構・内容同一の移動 |
| R5 データクラス化 | M | **Medium** | JSON 後方互換の検証が必要・キー参照22箇所・list/set の型維持に注意 |
| R6 メッセージ整形共通化 | S | Low | 出力文面の一致をテストで担保 |
| R7 品質ゲート | S | Low | 既存ツール（Ruff/pytest）をそのまま使用 |

全体: 最優先3項目のみなら **S（1〜3日）/ Low**。R5 を含めると **M（3〜7日）/ Medium**。

## 5. design フェーズへの申し送り

### 推奨アプローチ
- 全要件で **Option A（既存モジュールを拡張・既存パターン内で抽出）** を基本とする。新規ファイルは `tests/conftest.py` のみ。
- 実装順序（依存方向 main→各モジュール→config を壊さない下位優先）: R2(scraper) → R5/R6(state_manager/notifier) → R3(main) → R4(tests) → R7(品質ゲート) を各ステップ後に実行。
- 各リファクタリングは「1論点1コミット + 直後に `pytest`/`ruff` 全通過確認」の粒度で進め、振る舞い不変性（R1）を逐次保証する。

### 設計時の重要判断（Research/Decision Needed）
1. **R5 の採否**: データクラス化（Option A）か、キー名定数化に留める（Option B）か。後方互換の検証コストとのトレードオフを design で確定する。`notified_statuses` の list 維持は必須前提。
2. **R5/R6 を本spec スコープに含めるか**: 最優先3項目に絞る案も有効。tasks フェーズ前にユーザー判断を仰ぐ。
3. **振る舞い一致の検証手段**: R2/R6 は「リファクタリング前後で出力同一」を保証するため、必要なら現行出力を固定値化したテスト（characterization test）の追加可否を検討。

### 要件側の修正提案
- **テスト件数の不一致**: requirements.md の「62件」は実測 **71件** と相違。R4 AC-3 / R7 AC-1 の数値を 71 に修正することを推奨（design 着手前に確定）。

---

# 設計シンセシス結果 (Design Synthesis)

設計フェーズ（light discovery）で適用した3レンズの結論。

## 1. Generalization（一般化）
- R3 の2つの通知ループは「通知可否判定→投稿→マーク→加算」という同一問題の変種。単一ヘルパー `_process_notification(state_manager, change_type, notify, outage_id, status) -> bool` に一般化。投稿関数の差異は `functools.partial` で `Callable[[], bool]` に束ねて吸収する。
- R2 の地域キーワード `丁目|付近|地区|町|番地` は否定判定（196行）と抽出（227-228行）の2用途で重複。単一の `_AREA_KEYWORDS` 文字列定数を両正規表現で共有して一般化。
- 詳細リンク判定（`\d+`）とID抽出（`(\d+)`）は同一URLパターンの変種。キャプチャグループ付き単一定数 `_RE_DETAIL_ID` に統合（`find_all(href=...)` は search 意味のためグループ追加が照合に非影響）。

## 2. Build vs. Adopt（自作 vs 採用）
- R3 のラムダ遅延束縛問題: 自作のクロージャ回避策ではなく、標準ライブラリの `functools.partial` を採用。型安全（`Callable[[], bool]`）かつ遅延束縛バグを構造的に排除。
- R4 のフィクスチャ共有: 独自の共有モジュールを作らず、pytest 標準の `conftest.py` 自動収集機構を採用。

## 3. Simplification（単純化）
- R2 の動的パターン（`re.escape(status)`/`re.escape(area)` を使う `re.sub`）は入力依存のため定数化しない。投機的な抽象化を避け、静的パターンのみ定数化。
- R3 ヘルパーは `main.py` 内のローカル関数に留め、新規モジュール・新規クラスを作らない（最小の変更）。
- 新規ファイルは `tests/conftest.py` の1つのみ。state_manager / notifier / config は無変更。

## 設計上の確定事項
- 依存方向 `main → 各モジュール → config` を不変に維持。
- 振る舞い不変性（R1）は既存71件テストを回帰スイートとして担保。新規 characterization test は原則不要だが、R3 の投稿失敗パス（3.4）が既存テスト未カバーの場合のみ最小限追加する。
- 実装順序の推奨: R2(scraper) → R3(main) → R4(tests/conftest) → 各ステップ後に pytest/ruff 実行（5.x）。
