# よくある作業のレシピ

`CJ="python3 <スキルのディレクトリ>/scripts/cj.py"` と置いた前提で書く。書き込み系は最初に `--dry-run` を付けて確認する。1 日 100 リクエストの上限があるので、ループで何度も叩かない。

## 1. workflow_dispatch を叩くジョブが正しく動いているか点検する

このリポジトリでは、cron-job.org が毎時 7 分・37 分に GitHub の `workflow_dispatch` API を POST して `check-outage.yml` を起動している。

```bash
$CJ jobs list --table --tz Asia/Tokyo          # jobId・スケジュール・最終結果
$CJ history list <jobId> --table --tz Asia/Tokyo --limit 20
```

見るポイント:

- **HTTP が 204** なら起動成功。GitHub Actions 側に実行が無い場合は、GitHub 側の問題（ワークフローの無効化など）を疑う
- **401**: GitHub トークンの期限切れや失効 → レシピ 2
- **403**: トークンの権限不足（`Actions: Read and write` が無い）、またはリポジトリの対象外
- **404 / 422**: URL のワークフローファイル名、または本文の `ref` の誤り
- 予定時刻（`PLANNED`）が `:07` / `:17` / … / `:57` で、`JITTER` が数秒以内なら外部 cron 自体は正常

詳細なエラーメッセージは `history get <jobId> <identifier>` の `body` で読める。ただし本文が残るのはジョブの `saveResponses` が true の場合だけ。無ければ `jobs update <jobId> --save-responses` を有効にして次の実行を待つ。

## 2. GitHub トークン（PAT）を差し替える

Fine-grained PAT には有効期限がある。新しいトークンを発行したら、ジョブの `Authorization` ヘッダーだけを差し替える。`Accept` ヘッダーと本文 `{"ref":"main"}` は CLI が現在値を読んで残す。

```bash
# 新しいトークンはユーザー自身に環境変数へ入れてもらう（チャットに貼らせない）
export NEW_GITHUB_TOKEN=...   # ユーザーが実行

$CJ jobs update <jobId> --header "Authorization: Bearer $NEW_GITHUB_TOKEN" --dry-run   # 値はマスク表示
$CJ jobs update <jobId> --header "Authorization: Bearer $NEW_GITHUB_TOKEN"
$CJ jobs get <jobId>      # Authorization が "Bearer gith…****" になっているか、Accept/本文が残っているか
```

次の実行（10 分ごと）の後に `history list` で 204 になったことを確認する。

## 3. 失敗したらメールで知らせる

トークン切れで 401 が続いても、`schedule` のフォールバックで細々と動くため気づきにくい。cron-job.org 側の失敗通知を有効にしておく。

```bash
$CJ jobs update <jobId> --notify-failure --notify-failure-count 2 --notify-success --notify-disable --dry-run
```

通知先（メールなど）は Console のアカウント設定で管理し、API からは作れない。`notification.mode` を 2 にして特定チャネルだけに送る場合は `--json '{"notification":{"mode":2,"selectedChannels":[...]}}'`。

## 4. スケジュールを変える

```bash
# 10 分ごと、毎時 7, 17, …, 57 分（日本時間）
$CJ jobs update <jobId> --cron '7,17,27,37,47,57 * * * *' --timezone Asia/Tokyo --dry-run
# 変更後、次回実行予定で確認
$CJ history list <jobId> --table --tz Asia/Tokyo --limit 1
```

GitHub Actions の `schedule`（毎時 27 分のフォールバック）は主系と同じ分に揃えている（ずらすと 10 分未満の間隔で取得してしまう）。変えるときは `schedule` 側の分も合わせ、 README と CLAUDE.md の記述、Healthchecks の Period / Grace も合わせて更新する。

## 5. メンテナンス中に一時停止する

```bash
$CJ jobs disable <jobId>
# ... 作業 ...
$CJ jobs enable <jobId>
```

止めている間は `schedule` のフォールバックだけになり、起動頻度が大きく落ちる。Healthchecks.io の未起動検知が鳴る可能性があるので、必要なら healthchecks-api スキルでチェックも一時停止する。

## 6. ジョブを新しく作る（別リポジトリや作り直し）

```bash
$CJ jobs create \
  --title "toshima-tv-outage-notifier dispatch" \
  --url https://api.github.com/repos/<owner>/<repo>/actions/workflows/check-outage.yml/dispatches \
  --method POST \
  --header "Authorization: Bearer $GITHUB_TOKEN" \
  --header "Accept: application/vnd.github+json" \
  --body '{"ref":"main"}' \
  --cron '7,17,27,37,47,57 * * * *' --timezone Asia/Tokyo \
  --notify-failure --notify-failure-count 2 \
  --enable --dry-run
```

`--enable` を付けないと無効のまま作られる。作成 API はレート制限が厳しい（1 回/秒、5 回/分）ので、連続で作り直さない。
