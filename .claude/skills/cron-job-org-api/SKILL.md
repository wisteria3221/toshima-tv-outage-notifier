---
name: cron-job-org-api
description: cron-job.org の REST API でジョブ（外部 cron）を確認・変更する。ジョブ一覧・詳細（URL・メソッド・ヘッダー・本文・スケジュール・通知設定）の取得、実行履歴と次回実行予定・応答コードの確認、ジョブの作成・更新・有効化・無効化・削除、ヘッダーに入れた GitHub トークンの差し替え、フォルダ操作を、同梱の Python CLI（標準ライブラリのみ）で行う。ユーザーが「cron-job.org」「外部 cron」「workflow_dispatch を叩いているジョブ」「cron-job のスケジュールを変えたい」「cron-job が動いているか・失敗していないか見たい」「PAT / トークンを更新したので cron-job 側も差し替えたい」「cron-job を一時停止したい」などと言ったら、Console の手順を案内する代わりにこのスキルを使う。API キーの発行方法の質問にも使う。
allowed-tools: Bash, Read
---

# cron-job-org-api

cron-job.org の REST API（`https://api.cron-job.org/`）を、同梱の `scripts/cj.py` 経由で操作するスキル。
curl を毎回組み立てるより、CLI に任せた方が JSON の整形・エラーの説明・秘密値のマスク・ヘッダーの安全な合成が安定する。

## 前提と認証情報

- **API キー** は cron-job.org の Console → **Settings → API** で発行する。キーごとに送信元 IP を制限できる（制限外の IP からは 403）。Dev Container や CI から使うなら IP 制限は付けないか、その送信元を許可しておく
- **1 日 100 リクエストまで**（Sustaining member は 5,000）。超えると 429 で翌日まで使えない。一覧を何度も取り直さず、jobId が分かったら `jobs get` / `history list` を直接使う。`jobs update` でヘッダーや本文を変えるときは、現在値を読むための GET が 1 回増える
- キーは環境変数 `CRONJOB_API_KEY` で渡す。未設定なら、`cj.py` はカレントディレクトリから親へ辿って見つけた `.env` の `CRONJOB_*` 行で補う（既存の環境変数は上書きしない）。ユーザーに値を聞くときはチャットに貼らせず、本人に `.env` へ書くか `export` してもらう

| 環境変数 | 用途 |
|---|---|
| `CRONJOB_API_KEY` | API キー（必須） |
| `CRONJOB_BASE_URL` | API のベース URL（既定 `https://api.cron-job.org`。テスト用サーバーに向けるとき） |

## 秘密値の扱い

ジョブの `extendedData.headers` には、呼び出し先の認証情報（このリポジトリでは GitHub の `Authorization: Bearer <PAT>`）が入っている。

- `jobs get` / `jobs list` / `--dry-run` の出力では、`Authorization`・`Cookie` や名前に token / secret / key / password を含むヘッダーの値と、Basic 認証のパスワードを **マスクする**。`--show-secrets` はユーザーが値そのものを必要としている場合だけ使い、出力を回答に貼らない
- 新しいトークンを設定するときは、値をコマンドに直書きせず `--header "Authorization: Bearer $NEW_TOKEN"` のように環境変数で渡してもらう（ユーザーに `export NEW_TOKEN=...` してもらう）

## 進め方

1. **現状を確認してから変える**。`jobs list --table` で対象の jobId・スケジュール・最終結果を見て、必要なら `jobs get <id>` で詳細を見る
2. **書き込み系はまず `--dry-run`** で送る内容をユーザーに見せ、意図と一致したら同じコマンドから `--dry-run` を外して実行する。作成・更新・有効化・無効化・削除はすべて外部状態を変える
3. **更新は PATCH なので、指定した項目だけが変わる**。ただし `--header` / `--remove-header` / `--body` を使うと、CLI が現在の `extendedData` を GET して合成し、ヘッダーと本文をそろえて送る（ヘッダーだけ送って `{"ref":"main"}` の本文が消える、といった事故を防ぐため）。`--json` で `extendedData` を直接渡す場合はこの合成が効かないので、ヘッダーと本文を両方含める
4. **止めたいだけなら `jobs disable`**。`jobs delete` は取り消せず、`--yes` が無いと終了コード 2 で止まる。これは **人間に最終確認してもらうための停止** であり、Claude が自分で `--yes` を付け直して通すためのものではない。`jobs get` の内容（URL・スケジュール・最終実行）を見せて同意を得てから `--yes` を付ける
5. 作成時は `--enable` を付けないと **無効のまま作られる**（API の既定が `enabled=false`）。意図して無効で作る場合以外は付ける
6. 変更後は `jobs list --table` や `history list <id> --table` の次回実行予定（predictions）で、意図どおりのスケジュールになったか確認して伝える

## コマンド早見表

スクリプトは `python3 <このスキルのディレクトリ>/scripts/cj.py ...` で呼ぶ。`--help` と各サブコマンドの `--help` が最新の仕様。`--dry-run` はサブコマンドの前後どちらに置いてもよいが、`--api-key` と `--base-url` は `cj.py` の直後に置く。

| やりたいこと | コマンド |
|---|---|
| ジョブ一覧（表形式） | `jobs list --table [--tz Asia/Tokyo]` |
| ジョブ詳細（秘密値はマスク） | `jobs get <jobId>` |
| 実行履歴と次回予定 | `history list <jobId> --table [--tz Asia/Tokyo] [--limit N]` |
| 履歴 1 件（応答ヘッダー・本文） | `history get <jobId> <identifier>` |
| スケジュール変更 | `jobs update <jobId> --cron '7,37 * * * *' --timezone Asia/Tokyo` |
| ヘッダー差し替え（トークン更新） | `jobs update <jobId> --header "Authorization: Bearer $NEW_TOKEN"` |
| ヘッダー削除 / 本文変更 | `jobs update <jobId> --remove-header X-Foo` / `--body '{"ref":"main"}'` |
| 失敗通知を設定 | `jobs update <jobId> --notify-failure --notify-failure-count 2 --notify-disable` |
| 有効化 / 無効化 | `jobs enable <jobId>` / `jobs disable <jobId>` |
| 作成 | `jobs create --title T --url U --method POST --header 'K: V' --body B --cron '...' --timezone Asia/Tokyo --enable` |
| 削除 | `jobs delete <jobId> --yes` |
| フォルダ | `folders list --table` / `folders create T` / `folders rename <id> T` / `folders delete <id> --yes` |

`--cron` は「分 時 日 月 曜日」の 5 フィールドで、`*`・カンマ区切り・範囲・ステップ（`*/15`, `9-17/2`）に対応し、cron-job.org の配列表現（`*` は `[-1]`）に変換する。その他のフィールド（`auth`、`notification.mode` / `selectedChannels`、`onSslCertExpiry` など）は `--json '{...}'` で渡す。

## 結果の読み方

- `lastStatus` / 履歴の `status`: `1` が成功。`4`（HTTP エラー）は呼び出し先が 4xx/5xx を返したもの。`httpStatus` を見る。`workflow_dispatch` は成功すると **204**、トークンの期限切れや権限不足は **401 / 403**、ワークフロー名や ref の誤りは **404 / 422**
- 失敗の詳細（GitHub のエラーメッセージ）は `history get <jobId> <identifier>` の `body` で読める。ただし本文が残るのはジョブの `saveResponses` が true の場合だけ。false なら `--save-responses` を有効にして次の実行を待つ
- `jitter` は予定時刻からの遅れ（ミリ秒）。cron-job.org は通常 1 秒前後で、GitHub の `schedule` のような大幅な間引きは起きない

## エラーの読み方

CLI はステータスコードごとのヒントを stderr に出す。要点だけ:

- **401**: キーが無い・無効
- **403**: API キーの IP 制限に送信元 IP が含まれていない
- **404**: jobId / identifier / folderId の誤り
- **409**: フォルダ名の重複
- **429**: 1 日の上限（既定 100）か、作成の秒間・分間制限

終了コードは `0` 成功、`1` API/接続エラー、`2` 引数や認証情報の不足・確認待ち（削除の `--yes` なし）。

## 参照

- `references/api.md` — 全エンドポイント・フィールド・列挙値の一覧（curl で直接叩きたいとき、CLI にない項目を使うとき）
- `references/recipes.md` — このリポジトリの `workflow_dispatch` ジョブの点検、GitHub トークンの差し替え、失敗通知の設定、メンテナンス中の一時停止など
