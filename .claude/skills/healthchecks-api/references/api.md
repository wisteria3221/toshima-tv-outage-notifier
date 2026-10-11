# Healthchecks.io Management API v3 リファレンス

公式: https://healthchecks.io/docs/api/ （Management API）、https://healthchecks.io/docs/http_api/ （Ping API）
ベース URL: `https://healthchecks.io/api/v3/`（セルフホストは `<自分のURL>/api/v3/`）。v1 / v2 も残っているが、slug 関連は v3 のみ。

## 認証

- ヘッダー `X-Api-Key: <key>`。POST の JSON ボディに `"api_key": "<key>"` を入れる方法もある
- キーはプロジェクト単位（Settings → API Access）。アカウント全体のキーは無い
- **読み取り専用キー** が使えるのは: チェック一覧、チェック取得、flips 一覧、badges 一覧。応答から `uuid` `ping_url` `update_url` `pause_url` `resume_url` `channels` が省かれ、代わりに安定識別子 `unique_key` が付く（`GET /checks/<unique_key>`、`GET /checks/<unique_key>/flips/` に使える）

## エンドポイント一覧

| メソッド / パス | 用途 | 備考 |
|---|---|---|
| `GET /checks/` | チェック一覧 | `?tag=a&tag=b`（AND）、`?slug=xxx`（完全一致、0 件や複数件もあり得る） |
| `GET /checks/<uuid>` | 1 件取得 | |
| `GET /checks/<unique_key>` | 1 件取得（読み取り専用キー） | |
| `POST /checks/` | 作成 | `unique` 指定時は upsert。201 = 新規、200 = 既存を更新 |
| `POST /checks/<uuid>` | 更新 | 省略したフィールドは維持。`unique` は使えない |
| `POST /checks/<uuid>/pause` | 一時停止 | ボディ不要。状態が `paused` に |
| `POST /checks/<uuid>/resume` | 再開 | 状態が `new` に。`manual_resume` のチェックの復帰にも使う。一時停止中でないと 409 |
| `DELETE /checks/<uuid>` | 削除 | 取り消し不可。削除直前の表現を返す |
| `GET /checks/<uuid>/pings/` | ping 履歴 | 新しい順。Free 100 件、有料 1000 件 |
| `GET /checks/<uuid>/pings/<n>/body` | ping 本文 | `text/plain`。本文が無ければ 404、オブジェクトストレージ不通で 503 |
| `GET /checks/<uuid|unique_key>/flips/` | up/down 変化の履歴 | `?seconds=N`、`?start=<unix>`、`?end=<unix>` |
| `GET /channels/` | 通知先（integration）一覧 | 作成・更新・削除は API 非対応 |
| `GET /badges/` | タグごとのバッジ URL | `svg` `json` `shields` と 3 状態版 `svg3` `json3` `shields3`。`*` は全体 |
| `GET /status/` | サービス自体の稼働確認 | 認証不要。DB クエリ失敗で 500 |

## チェックのフィールド（作成・更新の入力）

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `name` | 文字列 ≤100 | `""` | 表示名 |
| `slug` | 文字列 ≤100 | 空 | `a-z 0-9 - _` のみ。Ping Key と組み合わせて ping に使う。一意性は保証されないので運用で一意にする |
| `tags` | 文字列 | `""` | スペース区切り |
| `desc` | 文字列 ≤10000 | `""` | 説明 |
| `timeout` | 整数 60〜31536000 | 86400 | 期待する ping 間隔（Period）秒 |
| `grace` | 整数 60〜31536000 | 3600 | 猶予（Grace Time）秒 |
| `schedule` | 文字列 | 無し | cron 式または systemd OnCalendar 式。指定すると `timeout` は無視 |
| `tz` | 文字列 | `UTC` | `schedule` 用タイムゾーン（例 `Asia/Tokyo`） |
| `manual_resume` | bool | false | down 後に手動 resume するまで復帰させない |
| `methods` | `""` / `"POST"` | `""` | 受け付ける ping メソッド。`""` = HEAD/GET/POST |
| `channels` | 文字列 | 無し | `"*"` で全 integration、カンマ区切りの UUID か名前。更新時 `""` で全解除 |
| `unique` | 配列 | 無し | 作成時のみ。`name` `slug` `tags` `timeout` `grace` から選ぶ。一致する既存があれば更新 |
| `start_kw` / `success_kw` / `failure_kw` | 文字列 | `""` | カンマ区切りのキーワード（大文字小文字区別） |
| `filter_subject` / `filter_body` / `filter_http_body` / `filter_default_fail` | bool | false | メール件名 / メール本文 / HTTP 本文でキーワード判定する、不一致を失敗扱いにする |

## チェックオブジェクト（応答）

`name` `slug` `tags` `desc` `grace` `n_pings` `status` `started` `last_ping` `next_ping` `manual_resume` `methods` `subject` `subject_fail` `start_kw` `success_kw` `failure_kw` `filter_subject` `filter_body` `filter_http_body` `filter_default_fail` `badge_url` `uuid` `ping_url` `update_url` `pause_url` `resume_url` `channels` `timeout`。cron チェックは `timeout` の代わりに `schedule` と `tz`。

- `status`: `new`（まだ ping なし）/ `up` / `grace`（期限切れ、猶予中）/ `down` / `paused`
- `ping` オブジェクト: `type`（success/start/fail/log/ign…）`date` `n` `scheme` `remote_addr` `method` `ua` `rid` `duration` `body_url`
- `flip` オブジェクト: `timestamp` `up`（1 / 0）
- `channel` オブジェクト: `id` `name` `kind`（email, slack, webhook, …）

## 応答コード

| コード | 意味 |
|---|---|
| 200 | 成功。作成では「`unique` 一致で既存を更新」 |
| 201 | 新規作成 |
| 400 | 不正なリクエスト（JSON 構文、フィールドの型・範囲） |
| 401 | API キー無し / 無効 |
| 403 | アクセス拒否（読み取り専用キーで書き込み、別プロジェクトのキー）。作成では **チェック数上限**（Free 20 件）も 403 |
| 404 | チェック / ping が存在しない |
| 409 | 一時停止中でないチェックへの resume |
| 429 | レート制限（目安 100 req/分） |
| 500 / 503 | サーバー側エラー / オブジェクトストレージ不通 |

## Ping API

ベース `https://hc-ping.com`（セルフホストは `<URL>/ping`）。認証ヘッダー不要。HEAD / GET / POST を受け付け、POST 本文（既定 100KB まで）はイベントに記録される。

| URL | 意味 |
|---|---|
| `/<uuid>` または `/<ping-key>/<slug>` | 成功。期限をリセット |
| `.../start` | 開始。実行時間計測と「grace 超過」の検知に使う |
| `.../fail` | 失敗。即時アラート |
| `.../log` | 記録のみ。状態も期限も変えない |
| `.../<0-255>` | 終了コード。0 = 成功、それ以外 = 失敗 |
| `?create=1` | slug 方式のみ。一致するチェックが無ければ自動作成し 201 を返す（timeout/grace は既定値） |
| `?rid=<uuid>` | 実行 ID。start と終了を対応付けて並行実行を区別する |

応答: `200 OK`、`201 Created`（自動作成）、`404 not found`、`409 ambiguous slug`（slug が複数一致）、`429 rate limit`（チェックごと 5 回/分）。
