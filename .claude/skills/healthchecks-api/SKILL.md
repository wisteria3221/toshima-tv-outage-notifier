---
name: healthchecks-api
description: Healthchecks.io（および同ソフトのセルフホスト版）を Management API / Ping API で操作する。チェック（cron 監視・dead-man's switch）の作成・一覧・更新・一時停止・再開・削除、ping 履歴や up/down 履歴の取得、通知先（integration）・バッジ URL の取得、Ping Key と slug による ping 送信と自動作成を、同梱の Python CLI（標準ライブラリのみ）で行う。ユーザーが「Healthchecks」「hc-ping」「healthchecks.io」「cron ジョブの死活監視」「ping URL を作りたい・取りたい」「Period / Grace を変えたい」「チェックを一時停止したい」「Healthchecks の設定を自動化・スクリプト化・CI から行いたい」「HEALTHCHECKS_URL を発行したい」などと言ったら、ダッシュボードの手順を案内する代わりにこのスキルを使う。API キーの発行方法の質問にも使う。
allowed-tools: Bash, Read
---

# healthchecks-api

Healthchecks.io の Management API（`/api/v3/`）と Ping API（`hc-ping.com`）を、同梱の `scripts/hc.py` 経由で操作するスキル。
curl を毎回組み立てるより、CLI に任せた方が JSON の整形・エラーの説明・キーのマスクが安定する。

## 前提と認証情報

- **API キーはプロジェクト単位**。アカウント全体のキーは存在しない。Healthchecks.io のダッシュボード → 対象プロジェクト → **Settings → API Access** で発行する。「読み書き」と「読み取り専用」の 2 種類があり、読み取り専用は一覧・取得・flips・badges だけ使え、応答から `uuid` や `ping_url` などが省かれる（代わりに `unique_key` が付く）
- **Ping Key** も同じ画面で発行する。slug 指定での ping（`hc-ping.com/<ping-key>/<slug>`）と `?create=1` による自動作成に使う
- 通知先（メール・Slack 等の integration）の **作成** は API ではできない。ダッシュボードで追加してもらい、API からは `channels` に名前か UUID を指定して紐付ける
- キーは環境変数で渡す。環境変数が未設定なら、`hc.py` はカレントディレクトリから親へ辿って見つけた `.env` の `HEALTHCHECKS_*` 行で補う（既存の環境変数は上書きしない）。継続的に使うなら `.env`（git 管理外）に `HEALTHCHECKS_API_KEY=...` を書いてもらうと再設定が要らない。ユーザーに値を聞くときはチャットに貼らせず、本人に `.env` へ書くか `export` してもらう。キーをログや出力に含めないこと（`--dry-run` の表示はマスク済み）

| 環境変数 | 用途 |
|---|---|
| `HEALTHCHECKS_API_KEY` | Management API キー（必須。`status` と `ping` 以外） |
| `HEALTHCHECKS_PING_KEY` | slug で ping する場合 |
| `HEALTHCHECKS_BASE_URL` | セルフホスト時の URL（既定 `https://healthchecks.io`） |
| `HEALTHCHECKS_PING_URL` | ping 先（既定 `https://hc-ping.com`。セルフホストで未指定なら `<BASE_URL>/ping`） |

## 進め方

1. **現状を確認してから変える**。`checks list --table` で既存チェックを見て、同じ用途のものが無いか確かめる。Free プランはプロジェクトあたり 20 件までなので、無駄な重複は作らない
2. **書き込み系はまず `--dry-run`** で送る内容をユーザーに見せ、意図と一致していれば同じコマンドから `--dry-run` を外して実行する。作成・更新・削除・ping はすべて外部状態を変える
3. **作成は `--unique slug` を付けて upsert にする**。再実行しても重複せず、HTTP 201（新規）/ 200（既存を更新）で結果が分かる。セットアップスクリプトや CI に組み込むときに特に重要
4. **削除は取り消せない**。`checks delete` は `--yes` が無いと終了コード 2 で止まるが、これは **人間に最終確認してもらうための停止** であり、Claude が自分で `--yes` を付け直して通すためのものではない。ユーザーが最初から「消して」と言っていても、`checks get <uuid>` の結果（状態・最終 ping・schedule・通知先）を見せてから、同意を得て `--yes` を付ける。特に状態が `down` / `grace` のチェックは「ping すべきジョブが止まっていて、今まさにそれを検知している」状態かもしれず、消すとその障害が見えなくなる。「いらなくなった」の根拠（ジョブ自体を廃止したか）を一言確認し、通知を止めたいだけなら `pause` を提案する
5. 結果の `ping_url` など、ユーザーが次に使う値は本文に明示する。GitHub Actions のシークレットに入れる場合は `gh secret set NAME --body "<url>"` まで案内する（`references/recipes.md`）

## コマンド早見表

スクリプトは `python3 <このスキルのディレクトリ>/scripts/hc.py ...` で呼ぶ。`--help` と各サブコマンドの `--help` が最新の仕様。`--dry-run` はサブコマンドの前後どちらに置いてもよいが、`--api-key` と `--base-url` は `hc.py` の直後に置く（環境変数で渡せば位置を気にしなくてよい）。

| やりたいこと | コマンド |
|---|---|
| チェック一覧（表形式） | `checks list --table [--tag T ...] [--slug S]` |
| チェック 1 件 | `checks get <uuid または unique_key>` |
| 作成（upsert） | `checks create --name N --slug S --timeout 秒 --grace 秒 --channels '*' --unique slug` |
| cron 式で作成 | `checks create --name N --slug S --schedule '*/30 * * * *' --tz Asia/Tokyo --unique slug` |
| 更新（指定項目のみ） | `checks update <uuid> --grace 1800` |
| 一時停止 / 再開 | `checks pause <uuid>` / `checks resume <uuid>` |
| 削除 | `checks delete <uuid> --yes` |
| ping 履歴 / 本文 | `pings list <uuid>` / `pings body <uuid> <n>` |
| up/down 履歴 | `flips <uuid> [--seconds 86400]` |
| 通知先一覧 | `channels --table` |
| バッジ URL | `badges` |
| ping を送る | `ping <uuid>` / `ping <slug> --ping-key K [--kind fail\|start\|log\|<終了コード>] [--create] [--body TEXT]` |
| サービス稼働確認 | `status`（認証不要） |

高度なフィールド（`start_kw` / `success_kw` / `failure_kw`、`filter_*`、`manual_resume`、`methods`）は専用フラグか `--json '{...}'` で渡せる。

## 値の決め方

- **`timeout`（Period）** はジョブの実行間隔、**`grace`** は遅延をどこまで許すか。GitHub Actions の `schedule` のように起動が間引かれる環境では、間隔ぴったりの `timeout` だと誤報が出る。実際の間隔の 2 倍程度を `timeout` にするか、`grace` を厚めに取る
- cron 式で動くジョブは `--schedule` + `--tz` を使うと Healthchecks 側が次回期限を正確に計算できる。`schedule` を指定すると `timeout` は無視される
- `channels '*'` でプロジェクトの全通知先に紐付く。既定（未指定）は通知先なしなので、作っただけでは何も届かない点をユーザーに伝える

## エラーの読み方

CLI はステータスコードごとのヒントを stderr に出す。要点だけ:

- **401**: キーが無い・無効。環境変数とプロジェクトの対応を確認
- **403**: 読み取り専用キーで書き込んだ、または作成時はチェック数上限（Free 20 件）
- **404**: UUID 違い、または別プロジェクトのキー。ping では Ping Key と slug の不一致（`--create` で自動作成可）
- **409**: `resume` を一時停止中でないチェックに呼んだ、または slug が複数一致して曖昧
- **429**: Management API は約 100 req/分、ping はチェックごと 5 回/分

終了コードは `0` 成功、`1` API/接続エラー、`2` 引数や認証情報の不足。

## 参照

- `references/api.md` — 全エンドポイント・フィールド・応答コードの一覧（curl で直接叩きたいとき、CLI にない項目を使うとき）
- `references/recipes.md` — GitHub Actions 用のセットアップ、UUID から slug 方式への移行、監視の一括停止・再開、down 中のチェック抽出など
