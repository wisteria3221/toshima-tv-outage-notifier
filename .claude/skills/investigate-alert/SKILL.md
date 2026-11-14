---
name: investigate-alert
description: このリポジトリの障害チェック（Check Toshima TV Outage）が止まった・失敗したと疑われるときに使う。Healthchecks からのアラート（down・期限切れ）、Actions の失敗通知、startup_failure、ユーザーが「障害チェックが動いていない」「Healthchecks が落ちた」「アラートが来た」「X に通知が来ない」「最近ちゃんと動いてる？」と言ったとき、cron-job.org の失敗を見たときに使う。
allowed-tools: Bash, Read
---

# investigate-alert

障害チェックの稼働状況を 3 か所から集めて、原因を切り分ける。

- **Actions**: 起動したか、成功したか
- **Healthchecks**: ping が届いたか、どの終了コードで届いたか
- **cron-job.org**: dispatch を送れたか

判断基準の正本は [.kiro/steering/operations.md](../../../.kiro/steering/operations.md)（「実行結果の読み方と対処」「終了コード 1 の原因分類」）。このスキルは材料を集めて、その表に当てはめる手順を示す。

## 1. 材料を集める

```bash
python3 .claude/skills/investigate-alert/scripts/snapshot.py [--hours 6]
python3 .claude/skills/investigate-alert/scripts/snapshot.py --from 2026-11-12T04:30 --to 2026-11-12T07:00   # 過去のアラート
```

- 直近の状況は `--hours`、過去のアラートは `--from` / `--to` で、アラートの時刻を前後 1 時間ほど含む期間を指定する。時差を書かない時刻は JST とみなされる
- Actions と Healthchecks の flips（up と down の切り替わり）は、期間を指定すればさかのぼって取れる。Healthchecks の ping（直近 100 件、約 17 時間分）と cron-job.org の履歴（直近 50 件、約 8 時間分）はそれより前を取れない。「注意: 取得件数の上限のため…」と出たら、その時刻より前は見えていない。その場合は Actions と flips から判断する
- Healthchecks と cron-job.org のキーは、それぞれのスキル（`healthchecks-api` / `cron-job-org-api`）と同じく `.env` から読む。キーが無い取得元は「取得できず」と出るので、残りの材料で判断する
- cron-job.org の API は 1 日 100 リクエストまでで、このスクリプトは 1 回に 2 回使う。何度も実行し直すときは `--skip-cronjob` を付ける

## 2. 切り分ける

上から順に当てはめ、最初に当てはまった行を原因とする。

| 材料に出たこと | 原因 | 次に見るもの |
|---|---|---|
| cron-job.org に **失敗**（HTTP 401/403/404 など）がある | dispatch 用の GitHub PAT の期限切れ・権限不足 | `cron-job-org-api` の `history get` で応答本文を見る。PAT の再発行と差し替えはユーザーが行う |
| cron-job.org のジョブが **無効** | 一時停止したまま戻していない | 停止した理由（`state.json` の手修正中など）をユーザーに確認してから再開する |
| cron-job.org は成功しているのに Actions に **起動の空白** がある | GitHub 側で dispatch が受け付けられていない | [GitHub Status](https://www.githubstatus.com/) の Actions を確認する |
| 失敗したランが「ジョブが起動していない（ログ無し）」 | `startup_failure` などで、ジョブが 1 つも動いていない。ping も送られないので Healthchecks が down になる | 同じコミットで成功したランがあれば GitHub 側の一時障害なので、回復を待つ。無ければそのコミットでのワークフローの変更（`git show <headSha> -- .github/workflows`）を疑う |
| 失敗したランにエラー行がある（終了コード 1） | 通知失敗・ページ解析不能・状態ファイル破損など | エラー行を operations.md の「終了コード 1 の原因分類」に当てはめる |
| Healthchecks の ping がすべて `log`（本文が `exit_code=2`） | としまテレビ側に届かない状態が長引いている | ブラウザでサイトを確認する。プログラムの不具合ではない |
| ping の本文が `commit=failure` | `state.json` を push できていない。次回二重投稿の恐れがある | 該当ランの Commit state changes ステップのログを見る。最優先で報告する |
| Lint and Test（main の最新）が失敗 | テストが壊れたまま、障害チェックが動き続けている | `gh run view <id> --log-failed` で失敗を見て直す |
| すべて正常で Healthchecks も up | 一時的な遅延がすでに回復した、または問題が無い | flips の down→up の時刻と、その間の Actions を突き合わせて原因を一言で報告する |
| すべて正常なのに「X に通知が来ない」 | 障害が出ていないか、レート制限でスキップされている | `data/state.json` の月間カウンタと、各障害の `notified_statuses` を見る（operations.md「レート制限時の挙動」） |

## 3. 報告する

- まず結論（何が起きていて、いまも続いているか）を書く。続けて根拠（時刻つきの材料）、ユーザーがすべきこと（あれば）の順に書く
- 時刻は UTC と JST を併記する。スクリプトの出力をそのまま使えばよい

## 調査中にしないこと

調査は読み取りだけで行う。外部の状態を変える操作は、原因を報告した後にユーザーの了承を得てから行う。

- cron-job.org のジョブの再開・停止、トークンの差し替え
- Healthchecks のチェックの一時停止
- `data/state.json` の修正
- ワークフローの再実行

シークレット（PAT・Ping URL・API キー）の値は出力や報告に含めない。
