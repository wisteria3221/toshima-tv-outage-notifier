---
name: update-dependencies
description: このリポジトリで依存パッケージを更新するときに使う。Dependabot の PR（uv・github-actions・devcontainers）が来たとき、複数の Dependabot PR が uv.lock で衝突しているとき、ユーザーが「依存を更新して」「ライブラリを最新にして」「Dependabot の PR を片付けて」「推移的依存を上げて」と言ったとき、ruff や pytest の更新で lint・テストが落ちたときに使う。
allowed-tools: Bash, Read, Edit
---

# update-dependencies

依存の更新を 1 本の PR にまとめ、CI と同じ検査を通してから `main` に入れる。CI は `uv sync --frozen` で動くので、`pyproject.toml` と `uv.lock` が食い違ったまま入れると 10 分ごとの障害チェックまで止まる。

## 1. 現状を確認する

```bash
gh pr list --author app/dependabot --json number,title,headRefName
uv tree --outdated --depth 1 --all-groups   # 直接依存。古いものに (latest: vX) が付く
uv lock --upgrade --dry-run                 # 推移的依存も含めた更新の有無
```

どれも更新が無ければ、その旨を報告して終える。

## 2. 進め方を決める

| 状況 | 進め方 |
|---|---|
| Dependabot の PR が 1 本だけ（エコシステムは問わない）で CI が成功 | その PR を `gh pr merge <n> --merge --delete-branch` でマージする。手元での作業は不要 |
| uv の Dependabot PR が 2 本以上 | `uv.lock` が互いに衝突するので、個別にマージせず 3 の手順で 1 本にまとめる |
| Dependabot の PR は無いが、直接依存か推移的依存に更新がある | 3 の手順で更新する |

github-actions の PR はコミット SHA と版のコメント（`@<sha> # v7.1.0`）を一緒に更新するので、そのままマージしてよい。手で直すときもタグではなく SHA で書く。

## 3. まとめて更新する

1. `main` を最新にして `deps/<内容>` ブランチを切る
2. 直接依存は `uv add '<pkg>>=<新版>'` で更新する（dev 依存は `uv add --optional dev ...`）。`pyproject.toml` の下限と `uv.lock` が同時に更新される。下限は更新後の版に揃えるのがこのリポジトリの慣例
3. 推移的依存は `uv lock --upgrade` で更新する。上がらなかったものは `uv tree --invert --package <pkg>` で制約の出どころを調べ、コミット本文に書く（例: oauthlib は tweepy が `<4` に制限）
4. CI と同じ検査を通す:

```bash
uv sync --all-extras
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

ruff の更新で新しいルールや整形の対象が増えて落ちたら、`src/` と `tests/` は修正する。`.kiro/` の仕様書は当時の設計のスナップショットなので書き換えない。既に `pyproject.toml` の `[tool.ruff.format] exclude = [".kiro/**"]` で整形の対象から外してある。それでも `.kiro/` で落ちる場合は、新しい ruff で exclude の効き方が変わった可能性がある。changelog で確認し、設定を直す（例: `[tool.ruff] extend-exclude` に移す）。どう対処したかはコミット本文に書く。

## 4. コミットからマージまで

- コミットメッセージは `chore(deps): <要約>`。本文には次を書く
  - `pkg 旧 → 新` の一覧
  - まとめた Dependabot PR の番号
  - 据え置いたパッケージとその理由
  - 設定を変えた場合はその理由
- PR を作り、CI（Lint and Test）が成功したら `gh pr merge <n> --merge --delete-branch` でマージする
- マージ後は **verify-push スキル**で、CI と push 後初回の障害チェックを確認する。実行時の依存（requests・beautifulsoup4・tweepy・python-dotenv）を上げたときは、本番で動くのはこの障害チェックが最初になる
- まとめた Dependabot の PR は、自動でクローズされていなければ `gh pr close <n> --comment "#<まとめた PR> でまとめて更新したためクローズします。" --delete-branch` で閉じる

## 対象外

- Python 本体の版の更新（正本は `.python-version`。CI と Dev Container が参照する）は Dependabot の対象外で、別の作業として扱う
- uv 本体は CI で版を固定していない（毎回最新）。更新作業は不要
