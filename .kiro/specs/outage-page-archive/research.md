# Research & Design Decisions: outage-page-archive

## Summary
- **Feature**: `outage-page-archive`
- **Discovery Scope**: Extension（既存パイプラインの後段に外部サービス連携を 1 つ追加する）
- **Key Findings**:
  - Wayback Machine の Save Page Now は**匿名利用が実質不可**。本環境からの匿名 GET `/save/<url>` は HTTP 429（`x-rl: 0`）、匿名 POST は HTTP 401「You need to be logged in」を返した。GitHub Actions のランナー IP は共有されるため、匿名利用はさらに不安定になる。認証付き SPN2 API（S3 キー）を使う
  - SPN2 API は「保存要求 → `job_id` → ステータス問い合わせ」の非同期契約。完了まで数秒〜最大 2 分。ステータスは要求後 1 時間程度しか保持されない
  - 詳細ページ（`/trouble/detail/<ID>`）には発生時間・内容・原因・復旧の記述・追記がすべて載り、ステータス「（復旧）」もタイトルに付く。一覧ページは詳細の部分集合なので対象にしない
  - 既存の `update_outages()` は追跡フィールドだけを `dict.update` で上書きするため、エントリに追加キーを足しても保持される。スキーマ変更は追加キー 1 つで済み、移行は不要

## Research Log

### 魚拓サービスの選定
- **Context**: 無人の GitHub Actions から、認証や CAPTCHA なしに安定して保存要求を送れるサービスが必要
- **Sources Consulted**:
  - SPN2 Public API Docs（Internet Archive, 2026-07-22 更新）: https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA
  - 本環境からの実測（2026-10-11）: `curl https://web.archive.org/save/https://example.com/` → 429、`curl -X POST .../save -d url=...` → 401
- **Findings**:
  - Wayback Machine: 公式 API あり。S3 キー（https://archive.org/account/s3.php、無料アカウントで発行可）で `Authorization: LOW <accesskey>:<secret>` ヘッダ認証。匿名は本環境で拒否された
  - archive.today: 公式 API なし、CAPTCHA が出ることがあり無人運用に向かない
  - 自前保存（HTML をリポジトリにコミット）: 確実だが第三者性がなく、公開にも別の仕組みが要る
- **Implications**: Wayback Machine の認証付き SPN2 を採用。認証情報は既存の X API と同じく環境変数／シークレットで注入し、未設定なら機能を無効化する

### SPN2 API の契約
- **Context**: 保存要求と完了確認の正確な仕様、および時間・回数の制限
- **Sources Consulted**: 上記 SPN2 Public API Docs
- **Findings**:
  - 保存要求: `POST https://web.archive.org/save`、ヘッダ `Accept: application/json`・`Authorization: LOW key:secret`、フォーム `url=<対象>`。応答 `{"url": ..., "job_id": "..."}`。失敗時は `{"message": "..."}` のみのことがある
  - 任意パラメータ（値は `1` のみ有効）: `skip_first_archive=1`（初回判定を省いて高速化）、`js_behavior_timeout=0`（JS 動作を省略、静的ページ向け）、`if_not_archived_within=<秒>`（直近キャプチャがこの時間内なら新規作成せず既存を返す。既定 1 時間）、`capture_all=1`（4xx/5xx でも保存。既定は 200 のみ）
  - ステータス: `GET https://web.archive.org/save/status/<job_id>`（同じ認証ヘッダ）。`status` は `pending` / `success` / `error`。`success` 時に `timestamp`（`YYYYMMDDHHMMSS`）と `original_url`。`error` 時に `status_ext`（例 `error:not-found`, `error:too-many-requests`, `error:user-session-limit`）と `message`
  - 公開 URL: `https://web.archive.org/web/<timestamp>/<original_url>`
  - 制限: 認証ユーザーは 7 キャプチャ/分、同一 URL は 10 キャプチャ/日、1 キャプチャの最大所要時間 2 分、対象サーバーの接続タイムアウト 10 秒、ステータス保持は要求後約 1 時間
- **Implications**:
  - 完了確認は同期的なポーリングで行い、上限 120 秒（SPN2 側の最大所要時間と同じ）で打ち切る
  - `if_not_archived_within` は既定の 1 時間のままだと「新規検出から 1 時間以内の復旧」で古いキャプチャが返り、復旧後の内容が残らない。実行間隔（10 分）より短い 60 秒を指定して、変更検出のたびに新しいキャプチャを作る
  - `capture_all` は指定しない。詳細ページが削除され 404 になった場合は失敗として扱い、記録を残さない（エラーページを魚拓にしても意味がない）
  - 同一 URL 10 回/日の制限は、1 障害あたり多くても数回（新規 + ステータス変更数）なので問題にならない

### 既存コードとの統合点
- **Context**: どこで魚拓を取り、どこに記録し、既存の巻き戻し・DRY RUN・終了コードの契約をどう守るか
- **Sources Consulted**: `src/main.py`, `src/state_manager.py`, `src/notifier.py`, `src/config.py`, `.github/workflows/check-outage.yml`, `.kiro/steering/*.md`
- **Findings**:
  - 通知ループは `_pending_notifications()` が新規障害とステータス変更を同じ形（`_PendingNotification`）に揃え、`_process_notification()` が 1 件ずつ `"sent" | "skipped" | "failed"` を返す。失敗分は `rollback_outages()` で `update_outages()` 前に戻してから保存する
  - `update_outages()` は新規エントリを `id` + 追跡フィールド + `first_seen` + `last_updated` + `notified_statuses` で作り、既存エントリは `existing.update(fields, last_updated=now)` で追跡フィールドだけ上書きする。追加キーは保持される
  - `DRY_RUN` は `config.py` の import 時に評価され、`XNotifier._post_tweet()` が先頭で判定して成功扱いにする
  - 認証情報は `get_x_credentials()` のように関数で読み、呼び出し時に環境変数を参照する
  - `tests/conftest.py` の autouse fixture `never_post_for_real` が実投稿を防ぐ。`responses` ライブラリは登録されていない URL への要求を `ConnectionError` にするため、archiver のテストで実要求が漏れることはない
  - ワークフローは `git add data/state.json` のみをコミットし、push 衝突時に `python3 -m json.tool` で検証する
- **Implications**:
  - 記録は `data/state.json` の障害エントリ内（`archives` 配列）に置き、別ファイルを増やさない。ワークフローの変更はシークレットの env 追加だけで済む
  - 魚拓は通知ループの**後**に、結果が `"sent"` / `"skipped"` の件だけを対象に取る。`"failed"` は巻き戻されるので対象外。通知より後に回すことで速報性を優先し、魚拓の待ち時間（最大 2 分）がツイートを遅らせない
  - 状態への書き込みは `StateManager` のメソッド経由に限定する（steering「状態は state_manager 経由でのみ操作」）
  - main 側は archiver 呼び出しを `try/except Exception` で包み、archiver のバグでも終了コードが変わらないようにする

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 同期ポーリング（採用） | 保存要求 → `job_id` を 5 秒間隔で問い合わせ → `success` で URL を確定 | 実装が最小。状態に「保留中ジョブ」を持たない | 1 件あたり最大 120 秒実行時間が延びる | 1 回の実行での変更は通常 1〜2 件なので許容。`concurrency` で後続ランは待たされる |
| 投げっぱなし + 次回回収 | 要求だけ送って `job_id` を状態に保存し、次回実行で回収 | 実行時間が延びない | 保留中ジョブの管理が要る。ステータスは約 1 時間で消えるので、次回実行が遅れると回収不能 | 複雑さに見合わない |
| 一括投入 + 一括問い合わせ | 複数の要求を先に送り、`job_ids=` で同時に問い合わせ | 複数件でも待ち時間が 1 件分 | 実装がやや増える | 件数が増えたときの改善案として記録。今回は採らない |
| 自前保存 | 詳細ページの HTML を取得してリポジトリにコミット | 外部依存なし、確実 | 第三者性がなく公開にも仕組みが要る。リポジトリが肥大化 | 要件（魚拓）に合わない |

## Design Decisions

### Decision: 認証付き SPN2 を使い、認証情報が無ければ機能を無効化する
- **Context**: 匿名の Save Page Now は本環境で 429/401 を返し、Actions の共有 IP ではさらに不安定になる
- **Alternatives Considered**:
  1. 匿名 GET `/save/<url>` — 認証不要だが拒否される
  2. 認証付き SPN2（S3 キー） — 無料アカウントで発行でき、公式 API として契約が明文化されている
- **Selected Approach**: 2。`WAYBACK_ACCESS_KEY` / `WAYBACK_SECRET_KEY` を環境変数から読み、どちらかが空なら archiver を無効にして警告ログを出す
- **Rationale**: `HEALTHCHECKS_URL` と同じ「任意のシークレット、未設定なら機能をスキップ」の流儀に揃う。既存の通知が認証情報の有無で止まることはない
- **Trade-offs**: 運用者が archive.org のアカウントとキーを用意する手間が増える
- **Follow-up**: README に取得手順を書く。キーの値はログに出さない

### Decision: 魚拓は通知ループの後に、送信・スキップ分だけ取る
- **Context**: 通知の速報性と、失敗時の巻き戻しとの整合
- **Alternatives Considered**:
  1. 通知の前に取る — ツイートが最大 2 分遅れる。魚拓 URL をツイートに載せる場合は必要だが、今回は載せない
  2. `_process_notification()` の中で件ごとに取る — 2 件目以降のツイートが 1 件目の魚拓待ちで遅れる
  3. 通知ループ完了後にまとめて取る（採用）
- **Selected Approach**: 3。`"sent"` と `"skipped"` の件を集め、ループ後に順に取る。`"failed"` は巻き戻されるので取らない
- **Rationale**: ツイートは全件を先に送る。レート制限でスキップした変更も記録には残す（要件 1.3）
- **Trade-offs**: 魚拓取得中にプロセスが落ちると、その実行の通知済みマークが保存されず次回二重投稿になりうる。archiver の全例外を捕捉し、HTTP には必ずタイムアウトを付けて、この経路で落ちないようにする
- **Follow-up**: main の例外捕捉と上限時間をテストで固定する

### Decision: 記録は `data/state.json` の障害エントリ内 `archives` 配列に置く
- **Context**: 記録の置き場所と、既存の自動コミット・JSON 検証・巻き戻しとの関係
- **Alternatives Considered**:
  1. `data/archives.json` など別ファイル — ワークフローの `git add` と JSON 検証を 2 ファイルに広げる必要があり、「状態ファイルが唯一の情報源」の前提も崩れる
  2. 障害エントリ内に `archives` 配列（採用）
- **Selected Approach**: 2。各要素は `{"status", "archived_at", "url"}`。`SCHEMA_VERSION` を `1.2` に上げるが、`_load_state()` は旧形式をそのまま読む（キーが無ければ記録なし）
- **Rationale**: `update_outages()` は追加キーを保持する。ワークフローの変更が不要
- **Trade-offs**: 状態ファイルが少し大きくなる（1 記録 約 150 バイト）
- **Follow-up**: 旧スキーマ読み込みのテストを追加する

### Decision: 失敗は警告のみで再試行しない
- **Context**: 魚拓は記録であり通知ではない。再試行には「未取得の変更」を状態に持つ必要がある
- **Selected Approach**: 失敗は WARNING ログと件数サマリのみ。次回実行では再試行しない（通知済みの変更は再検出されない）
- **Rationale**: ユーザーと合意済みの方針。再試行の仕組みは複雑さに見合わない
- **Trade-offs**: SPN2 の一時的な過負荷で記録が抜けることがある。ログで気づけるが自動では埋まらない
- **Follow-up**: 抜けが目立つようなら「失敗した件を次回取り直す」拡張を別 spec で検討する

## Risks & Mitigations
- SPN2 の過負荷・レート制限（`error:too-many-requests`, `error:user-session-limit`, 7 件/分） — 1 件ずつ順に取り、失敗は警告のみ。1 実行での件数は通常 1〜2 件
- 実行時間の延伸（1 件最大 120 秒） — 上限を定数化し、通知の後に回す。実行全体は `WAYBACK_RUN_BUDGET`（300 秒）で打ち切り、期限は archiver に渡して処理中の件も超えない。`concurrency` で後続ランは直列化される
- 保存要求の再試行による二重投入 — 受理されたのに応答が届かず（読み取りタイムアウト）再試行すると同じページに 2 件目のジョブが入りうる。セッション枠を 1 つ余分に使うだけで、記録は 2 件目の結果になる。対象は通常 1〜2 件なので許容し、対策（応答待ちの延長や再試行の抑止）は取らない
- 詳細ページが削除され 404 — `capture_all` を付けないので失敗扱い（記録なし）。警告ログに `error:not-found` が残る
- 認証情報の漏えい — 値はログ・状態ファイル・通知に出さない。`Authorization` ヘッダはセッションに設定し、例外メッセージにも含めない
- 魚拓取得中のプロセス異常終了で通知済みマークが失われる — archiver の例外は全て捕捉し、HTTP に必ずタイムアウトを付ける
- `if_not_archived_within` の値が実行間隔より長いと復旧後の内容が残らない — 60 秒に固定し、理由をコメントに書く

## References
- [Save Page Now 2 Public API Docs](https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA) — 保存要求・ステータス・エラーコード・制限の一次情報
- [archive.org S3 API keys](https://archive.org/account/s3.php) — 認証キーの発行ページ
- [Wayback Availability JSON API](https://archive.org/help/wayback_api.php) — 既存キャプチャの有無確認（今回は未使用。本件の詳細ページには 2026-10-11 時点でキャプチャが無いことを確認）
