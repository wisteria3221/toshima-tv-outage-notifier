# よくある作業のレシピ

`HC="python3 <スキルのディレクトリ>/scripts/hc.py"` と置いた前提で書く。書き込み系は最初に `--dry-run` を付けて確認する。

## 1. GitHub Actions のワークフローを dead-man's switch で監視する

GitHub の `schedule` は間引かれるので、「起動しなかったこと」をワークフロー自身は検知できない。チェックを作って `ping_url` をシークレットに入れ、ワークフロー末尾から ping する。

```bash
# 既存チェックの確認（重複を作らない）
$HC checks list --table

# upsert で作成。30 分ごとのジョブなら Period は余裕を見て 1 時間、Grace 30 分
$HC checks create \
  --name "my-repo check-outage" --slug my-repo-check-outage \
  --tags "github-actions my-repo" \
  --timeout 3600 --grace 1800 --channels '*' --unique slug \
  --desc "GitHub Actions check-outage.yml の未起動検知"

# ping_url だけ取り出してリポジトリのシークレットへ
PING_URL=$($HC checks list --slug my-repo-check-outage | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"][0]["ping_url"])')
gh secret set HEALTHCHECKS_URL --repo <owner>/<repo> --body "$PING_URL"
```

ワークフロー側は終了コードに応じて `"$HEALTHCHECKS_URL"`（成功）、`"$HEALTHCHECKS_URL/log"`（記録のみ）、`"$HEALTHCHECKS_URL/fail"`（即時アラート）へ `curl -fsS -m 10 --retry 3` する。

## 2. UUID 方式から Ping Key + slug 方式へ移す

Ping Key を使うと、チェックごとの UUID をシークレットに持たなくてよい。`--create` を付ければ初回 ping でチェックが自動作成されるが、timeout/grace は既定値（1 日 / 1 時間）になるので、後から `checks update` で調整するか、先に Management API で作っておく。

```bash
export HEALTHCHECKS_PING_KEY=...   # Settings → API Access で発行
$HC ping my-repo-check-outage --create          # 成功 ping（無ければ作成）
$HC ping my-repo-check-outage --kind fail        # 失敗
$HC ping my-repo-check-outage --kind log --body "upstream unreachable"
```

ワークフローからは `https://hc-ping.com/${PING_KEY}/<slug>` を使う。slug は重複すると 409 になるので、プロジェクト内で一意にする。

## 3. メンテナンス中の誤報を止める（一括 pause / resume）

```bash
# タグで対象を列挙
$HC checks list --tag prod | python3 -c 'import json,sys; [print(c["uuid"], c["name"]) for c in json.load(sys.stdin)["checks"]]'

# 1 件ずつ pause（resume も同じ形）
for u in <uuid1> <uuid2>; do $HC checks pause "$u"; done
```

pause 中は ping を受けても状態は変わらない。再開は `checks resume <uuid>`（状態は `new` に戻り、次の ping で `up` になる）。

## 4. 落ちているチェックと最近の状態変化を調べる

```bash
# status が down / grace のものを抽出
$HC checks list | python3 -c '
import json,sys
for c in json.load(sys.stdin)["checks"]:
    if c["status"] in ("down","grace"): print(c["status"], c["name"], c["last_ping"])'

# 直近 24 時間の up/down 変化
$HC flips <uuid> --seconds 86400

# 最後の ping の本文（ジョブが POST したログ）
$HC pings list <uuid> | python3 -c 'import json,sys; print(json.load(sys.stdin)["pings"][0]["n"])'
$HC pings body <uuid> <n>
```

## 5. 通知先を紐付ける・外す

integration 自体はダッシュボードで作る。API からは名前か UUID で紐付けを変えられる。

```bash
$HC channels --table
$HC checks update <uuid> --channels "Ops Slack,alerts@example.com"   # 名前で指定
$HC checks update <uuid> --channels '*'                               # 全部
$HC checks update <uuid> --channels ''                                # 解除
```

## 6. cron ジョブを cron 式で登録する

実行時刻が決まっているジョブは `--schedule` の方が正確に期限を計算できる。

```bash
$HC checks create --name "nightly backup" --slug nightly-backup \
  --schedule "0 3 * * *" --tz Asia/Tokyo --grace 1800 --unique slug --channels '*'
```

## 7. セルフホスト版に対して使う

```bash
export HEALTHCHECKS_BASE_URL=https://hc.example.internal
$HC status            # 認証不要の稼働確認
$HC checks list --table
```

ping 先は自動的に `https://hc.example.internal/ping` になる。別ドメインなら `HEALTHCHECKS_PING_URL` で上書きする。

## 8. 読み取り専用キーでレポートを作る

読み取り専用キーでは `uuid` が返らず `unique_key` が付く。`checks get <unique_key>` と `flips <unique_key>` はそのまま使えるが、`ping_url` は組み立てられないので、URL が必要なら読み書きキーが要る。
