# cron-job.org REST API リファレンス

出典: https://docs.cron-job.org/rest-api.html

## 共通

- ベース URL: `https://api.cron-job.org/`（HTTPS のみ）
- 認証: `Authorization: Bearer <API キー>`。キーは Console → Settings で発行し、送信元 IP を制限できる
- 本文は JSON。`Content-Type: application/json` が無いと本文は無視される
- レート制限:
  - 1 日 100 リクエスト（Sustaining member は 5,000）
  - 多くの参照・更新・削除は 5 回/秒
  - `PUT /jobs`（作成）は 1 回/秒かつ 5 回/分
  - `PUT /folders` は 1 回/秒かつ 10 回/分。フォルダの更新・削除は 1 回/秒
- 応答コード: 200 OK / 400 入力不正 / 401 キー無効 / 403 IP 制限 / 404 なし / 409 重複（フォルダ名） / 429 レート制限 / 500 サーバーエラー

## エンドポイント

| メソッドとパス | リクエスト本文 | 応答 |
|---|---|---|
| `GET /jobs` | - | `{"jobs": [Job], "someFailed": bool}` |
| `GET /jobs/<jobId>` | - | `{"jobDetails": DetailedJob}` |
| `PUT /jobs` | `{"job": DetailedJob}`（`url` のみ必須） | `{"jobId": int}` |
| `PATCH /jobs/<jobId>` | `{"job": DetailedJob}`（変更するフィールドのみ） | `{}` |
| `DELETE /jobs/<jobId>` | - | `{}` |
| `GET /jobs/<jobId>/history` | - | `{"history": [HistoryItem], "predictions": [int]}`（`predictions` は次回実行予定の UNIX 秒、最大 3 件） |
| `GET /jobs/<jobId>/history/<identifier>` | - | `{"jobHistoryDetails": HistoryItem}` |
| `GET /folders` | - | `{"folders": [Folder]}` |
| `GET /folders/<folderId>` | - | `{"folderDetails": Folder}` |
| `PUT /folders` | `{"folder": {"title": ...}}` | `{"folderId": int}` |
| `PATCH /folders/<folderId>` | `{"folder": {...}}` | `{}` |
| `DELETE /folders/<folderId>` | - | `{}`（中のジョブはルート `folderId=0` へ移動） |

読み取り専用フィールド（`jobId`、`lastStatus`、`lastDuration`、`lastExecution`、`nextExecution`、`sslCertExpiry`、`type`）は作成・更新時に無視される。

## Job

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `jobId` | int | - | 読み取り専用 |
| `enabled` | bool | `false` | 実行するか |
| `title` | string | `""` | |
| `saveResponses` | bool | `false` | 応答ヘッダー・本文を履歴に保存する |
| `url` | string | 必須 | |
| `lastStatus` | JobStatus | `0` | 読み取り専用 |
| `lastDuration` | int | - | ミリ秒。読み取り専用 |
| `lastExecution` | int | - | UNIX 秒。読み取り専用 |
| `nextExecution` | int / null | - | UNIX 秒。読み取り専用 |
| `sslCertExpiry` | int | - | HTTPS のみ。読み取り専用 |
| `type` | int | `0` | 0 = 通常、1 = ステータスモニター用。読み取り専用 |
| `requestTimeout` | int | `-1` | 秒。-1 = 既定値 |
| `redirectSuccess` | bool | `false` | 3xx を成功とみなす |
| `folderId` | int | `0` | |
| `schedule` | JobSchedule | `{}` | |
| `requestMethod` | int | `0` | 0 GET, 1 POST, 2 OPTIONS, 3 HEAD, 4 PUT, 5 DELETE, 6 TRACE, 7 CONNECT, 8 PATCH |

DetailedJob は Job に以下を加えたもの:

- `auth`（JobAuth）: `enable` bool、`user` string、`password` string（Basic 認証）
- `notification`（JobNotificationSettings）:
  - `onFailure` bool（既定 false）、`onFailureCount` int（既定 1、1 以上）
  - `onSuccess` bool（失敗後の成功で通知）、`onDisable` bool（自動無効化で通知）
  - `onSslCertExpiry` bool、`onSslCertExpirySeconds` int（既定 604800 秒）
  - `mode` int（0 = 通知しない、1 = 全チャネル（既定）、2 = `selectedChannels` のみ）、`selectedChannels` int[]
- `extendedData`（JobExtendedData）: `headers`（名前→値の辞書）、`body` string

### JobSchedule

| フィールド | 型 | 説明 |
|---|---|---|
| `timezone` | string | 既定 `UTC`（例: `Asia/Tokyo`） |
| `expiresAt` | int | `YYYYMMDDhhmmss`（ジョブのタイムゾーン）。`0` = 無期限 |
| `minutes` | int[] | 0–59 |
| `hours` | int[] | 0–23 |
| `mdays` | int[] | 1–31 |
| `months` | int[] | 1–12 |
| `wdays` | int[] | 0（日）–6（土） |

各配列の `[-1]` は「すべて」を表す（cron の `*`）。

## HistoryItem

| フィールド | 型 | 説明 |
|---|---|---|
| `jobId` | int | |
| `identifier` | string | 詳細取得に使う ID |
| `date` / `datePlanned` | int | 実際の実行時刻 / 予定時刻（UNIX 秒） |
| `jitter` | int | 予定からの遅れ（ミリ秒） |
| `url` | string | 実行時の URL |
| `duration` | int | ミリ秒 |
| `status` | JobStatus | |
| `statusText` | string | |
| `httpStatus` | int | 呼び出し先の HTTP ステータス |
| `headers` / `body` | string / null | 応答の生データ。一覧では常に null。詳細でも `saveResponses` が false なら null |
| `stats` | object | `nameLookup`, `connect`, `appConnect`, `preTransfer`, `startTransfer`, `total`（マイクロ秒） |

## JobStatus

| 値 | 意味 |
|---|---|
| 0 | 不明 / 未実行 |
| 1 | OK |
| 2 | 失敗（DNS エラー） |
| 3 | 失敗（ホストに接続できない） |
| 4 | 失敗（HTTP エラー） |
| 5 | 失敗（タイムアウト） |
| 6 | 失敗（応答データが大きすぎる） |
| 7 | 失敗（URL 不正） |
| 8 | 失敗（内部エラー） |
| 9 | 失敗（不明） |
| 10 | 失敗（チャレンジページ） |

## curl 例

```bash
curl -sS -H "Authorization: Bearer $CRONJOB_API_KEY" https://api.cron-job.org/jobs

curl -sS -X PATCH -H "Authorization: Bearer $CRONJOB_API_KEY" -H 'Content-Type: application/json' \
  -d '{"job":{"enabled":false}}' https://api.cron-job.org/jobs/1234567
```
