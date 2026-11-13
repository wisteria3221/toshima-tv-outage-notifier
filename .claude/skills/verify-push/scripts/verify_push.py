#!/usr/bin/env python3
"""push 後の CI（Lint and Test）と、push 後初回の障害チェック（Check Toshima TV Outage）を確認する。

gh CLI と標準ライブラリだけで動く。ランは日付ではなく「push 時刻以降に作られ、対象コミットを含む」
ことで特定する（ローカル日付と GitHub の UTC 日付がずれても取り違えないため）。

終了コード:
  0  すべて正常（障害チェックの終了コード 2 = 上流不通は警告扱いで 0）
  1  CI 失敗、障害チェックの失敗、state の push 失敗、Healthchecks 送信失敗のいずれか
  3  待ち時間内にランが見つからない・終わらない
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

LINT_WORKFLOW = "lint.yml"
OUTAGE_WORKFLOW = "check-outage.yml"
DEFAULT_BRANCH = "main"

_APP_LOG = re.compile(
    r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ - (\S+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)"
)
_STATE_COMMIT = re.compile(r"^\[\S+ ([0-9a-f]{7,})\] chore: Update outage state")
_ENV_LINE = re.compile(r"^\s+(EXIT_CODE|COMMIT_OUTCOME):\s*(.*)$")
_JST = timezone(timedelta(hours=9))
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


class WaitTimeoutError(Exception):
    pass


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} が失敗しました: {result.stderr.strip()}"
        )
    return result.stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_time(value: str) -> str:
    """UTC と JST を併記する（GitHub は UTC、ワークフローのログは JST のため）。"""
    t = parse_time(value)
    return f"{t:%Y-%m-%d %H:%M:%S} UTC（JST {t.astimezone(_JST):%m-%d %H:%M}）"


def log(msg: str) -> None:
    print(f"[verify-push] {msg}", file=sys.stderr, flush=True)


def compare_status(repo: str, base: str, head: str) -> str:
    """GitHub の compare API の status（identical / ahead / behind / diverged）を返す。"""
    return gh("api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".status").strip()


def contains_commit(repo: str, head: str, sha: str) -> bool:
    """head が sha を含む（sha と同じか、sha より先にある）か。"""
    if head == sha:
        return True
    return compare_status(repo, sha, head) in ("identical", "ahead")


def wait_for(fetch, done, timeout_s: float, poll_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while True:
        value = fetch()
        if done(value):
            return value
        if time.monotonic() >= deadline:
            raise WaitTimeoutError(
                f"{what}を {int(timeout_s)} 秒待ちましたが確認できませんでした"
            )
        time.sleep(poll_s)


# ---------------------------------------------------------------- Lint and Test


def check_lint(sha: str, args) -> tuple[list[dict], str | None]:
    """sha の Lint and Test ランを待って返す。push イベントのランの作成時刻も返す。"""
    fields = "databaseId,event,status,conclusion,createdAt,url,headBranch"

    def fetch():
        return gh_json(
            "run",
            "list",
            "-w",
            LINT_WORKFLOW,
            "--commit",
            sha,
            "-L",
            "20",
            "--json",
            fields,
        )

    log(f"Lint and Test のランを待っています（{sha[:7]}）")
    try:
        runs = wait_for(
            fetch, bool, args.appear_timeout, args.poll, "Lint and Test のラン"
        )
    except WaitTimeoutError:
        return [], None
    runs = wait_for(
        fetch,
        lambda rs: rs and all(r["status"] == "completed" for r in rs),
        args.timeout,
        args.poll,
        "Lint and Test の完了",
    )
    push_runs = [r for r in runs if r["event"] == "push"]
    pushed_at = min((r["createdAt"] for r in push_runs), default=None)
    return runs, pushed_at


# ---------------------------------------------------------- Check Toshima TV Outage


def find_outage_run(repo: str, sha: str, since: datetime, args) -> dict:
    fields = "databaseId,headSha,event,status,conclusion,createdAt,url"
    rejected: set[int] = set()

    def list_since() -> list[dict]:
        # 取得件数の窓が push 時刻まで届いていないと、窓の端のランを「初回」と取り違える。
        # 一番古いランが push 時刻より前になる（または全件取り切る）まで窓を広げる
        limit = 30
        while True:
            runs = gh_json(
                "run", "list", "-w", OUTAGE_WORKFLOW, "-L", str(limit), "--json", fields
            )
            covered = len(runs) < limit or any(
                parse_time(r["createdAt"]) < since for r in runs
            )
            if covered or limit >= 1000:
                return [r for r in runs if parse_time(r["createdAt"]) >= since]
            limit *= 3

    def fetch():
        runs = list_since()
        runs.sort(key=lambda r: r["createdAt"])
        for r in runs:
            if r["databaseId"] in rejected:
                continue
            if contains_commit(repo, r["headSha"], sha):
                return r
            rejected.add(r["databaseId"])
        return None

    log(
        f"{fmt_time(since.isoformat().replace('+00:00', 'Z'))} 以降の障害チェックを待っています"
    )
    run = wait_for(fetch, bool, args.timeout, args.poll, "push 後初回の障害チェック")
    run_id = str(run["databaseId"])
    log(f"ラン {run_id} を見つけました。完了を待っています")
    return wait_for(
        lambda: gh_json("run", "view", run_id, "--json", fields),
        lambda r: r["status"] == "completed",
        args.timeout,
        args.poll,
        f"ラン {run_id} の完了",
    )


def fetch_log(run_id: str) -> list[str]:
    # 完了直後はログがまだ用意されていないことがあるので数回待つ
    for _ in range(6):
        try:
            text = gh("run", "view", run_id, "--log")
            if text.strip():
                break
        except RuntimeError:
            pass
        time.sleep(10)
    else:
        return []
    lines = []
    for raw in text.splitlines():
        body = raw.split("\t", 2)[-1]
        body = _TIMESTAMP.sub("", body)
        # スクリプト本文のエコーは除く（gh は色コードを ESC ではなく文字列 "^[" で出すことがある）
        if "[36;1m" in body:
            continue
        lines.append(body)
    return lines


def analyze_outage(run: dict) -> dict:
    run_id = str(run["databaseId"])
    view = gh_json("run", "view", run_id, "--json", "jobs")
    steps = {s["name"]: s["conclusion"] for job in view["jobs"] for s in job["steps"]}
    lines = fetch_log(run_id)

    app_lines = []
    env: dict[str, str] = {}
    state_commit = None
    push_retry = push_failed = upstream_warning = no_change = False
    for line in lines:
        if m := _APP_LOG.search(line):
            app_lines.append((m.group(2), m.group(3)))
        elif m := _ENV_LINE.match(line):
            env.setdefault(m.group(1), m.group(2).strip())
        elif m := _STATE_COMMIT.match(line.strip()):
            state_commit = m.group(1)
        elif "状態ファイルに変更はありません" in line:
            no_change = True
        elif "push が拒否されたため" in line:
            push_retry = True
        elif "State push failed" in line and "##[error]" in line:
            push_failed = True
        elif "Upstream unavailable" in line and "##[warning]" in line:
            upstream_warning = True

    checker = steps.get("Run outage checker")
    exit_code = env.get("EXIT_CODE")
    if not exit_code:
        if checker == "success":
            exit_code = "2" if upstream_warning else "0"
        elif checker == "failure":
            exit_code = "1 以上（ログから特定できず）"
        else:
            exit_code = f"不明（ステップ: {checker}）"

    commit_step = steps.get("Commit state changes")
    if commit_step == "skipped":
        commit = "スキップ（終了コード 2 など、コミットしない終了）"
    elif push_failed or commit_step == "failure":
        commit = "**失敗**（state を push できず。次回二重投稿の恐れ）"
    elif state_commit:
        commit = f"コミット `{state_commit}`" + (
            "（push 拒否後にリベースして再試行で成功）" if push_retry else ""
        )
    elif no_change:
        commit = "変更なし（コミット不要）"
    else:
        commit = f"不明（ステップ: {commit_step}）"

    hc_step = steps.get("Report to Healthchecks.io")
    if hc_step == "skipped":
        hc = "スキップ（`HEALTHCHECKS_URL` 未設定）"
    elif hc_step == "success":
        outcome = env.get("COMMIT_OUTCOME", "")
        code = env.get("EXIT_CODE", "")
        if outcome == "failure":
            hc = "`/fail` を送信（state の push 失敗）"
        elif code == "0":
            hc = "通常 ping を送信（成功）"
        elif code == "2":
            hc = "`/log` を送信（上流不通の記録のみ）"
        else:
            hc = f"`/fail` を送信（終了コード {code or 'なし'}）"
    elif hc_step == "failure":
        hc = "**送信失敗**（curl がエラー）"
    else:
        hc = f"不明（ステップ: {hc_step}）"

    ok = (
        run["conclusion"] == "success"
        and exit_code in ("0", "2")
        and not push_failed
        and hc_step in ("success", "skipped")
    )
    return {
        "exit_code": exit_code,
        "commit": commit,
        "healthchecks": hc,
        "app_lines": app_lines,
        "ok": ok,
        "steps": steps,
    }


# ------------------------------------------------------------------------ main


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sha", help="確認するコミット（既定: ローカルの HEAD）")
    p.add_argument(
        "--since", help="push 時刻（ISO 8601, UTC）。Lint の push ランが無いときに使う"
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=1500,
        help="各段階の待ち時間の上限（秒、既定 1500）",
    )
    p.add_argument(
        "--appear-timeout",
        type=float,
        default=180,
        help="Lint ランが現れるまでの上限（秒、既定 180）",
    )
    p.add_argument(
        "--poll", type=float, default=20, help="問い合わせ間隔（秒、既定 20）"
    )
    p.add_argument(
        "--skip-outage", action="store_true", help="障害チェックの確認を省く"
    )
    args = p.parse_args()

    sha = subprocess.run(
        ["git", "rev-parse", args.sha or "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    repo = gh(
        "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
    ).strip()
    print(f"## push 後の確認（`{sha[:7]}`）\n")

    status = 0
    try:
        lint_runs, pushed_at = check_lint(sha, args)
    except WaitTimeoutError as e:
        print(f"- Lint and Test: **時間切れ** — {e}")
        return 3

    print("| 確認項目 | イベント | 結果 | 作成時刻 |\n|---|---|---|---|")
    if not lint_runs:
        print(
            f"| Lint and Test | - | ランなし（{int(args.appear_timeout)} 秒待っても現れず） | - |"
        )
    for r in sorted(lint_runs, key=lambda r: r["createdAt"]):
        ok = r["conclusion"] == "success"
        status = status if ok else 1
        result = "成功" if ok else f"**{r['conclusion']}**"
        print(
            f"| Lint and Test | {r['event']} | {result} | {fmt_time(r['createdAt'])} |"
        )

    if args.skip_outage:
        return status

    in_main = compare_status(repo, DEFAULT_BRANCH, sha) in ("identical", "behind")
    if not in_main:
        print(
            f"\n障害チェック: 対象外（`{sha[:7]}` は `{DEFAULT_BRANCH}` に入っていないため、障害チェックでは実行されない）"
        )
        return status

    since_str = pushed_at or args.since
    if not since_str:
        print(
            "\n障害チェック: **push 時刻が分からない**（`--since` で push 時刻を指定して再実行する）"
        )
        return 3
    since = parse_time(
        since_str if since_str.endswith(("Z", "+00:00")) else since_str + "Z"
    )

    try:
        run = find_outage_run(repo, sha, since, args)
    except WaitTimeoutError as e:
        print(f"| Check Toshima TV Outage | - | **時間切れ** — {e} | - |")
        return 3

    result = analyze_outage(run)
    status = status if result["ok"] else 1
    label = "成功" if run["conclusion"] == "success" else f"**{run['conclusion']}**"
    print(
        f"| Check Toshima TV Outage（push 後初回） | {run['event']} | {label}、終了コード {result['exit_code']} "
        f"| {fmt_time(run['createdAt'])} |"
    )
    print(
        f"\n### 障害チェックの詳細（ラン {run['databaseId']}、headSha `{run['headSha'][:7]}`）\n"
    )
    if run["headSha"] != sha:
        print(
            f"- headSha は対象コミットの後のコミット（state の自動コミットなど）で、`{sha[:7]}` を含む"
        )
    print(f"- 終了コード: {result['exit_code']}")
    print(f"- state のコミット: {result['commit']}")
    print(f"- Healthchecks: {result['healthchecks']}")
    print(f"- URL: {run['url']}")
    if result["app_lines"]:
        print("\nチェッカーのログ（WARNING 以上と結果行）:\n```")
        shown = [
            (lv, msg)
            for lv, msg in result["app_lines"]
            if lv != "INFO"
            or re.search(
                r"新しい障害|ステータス変更|通知|魚拓|投稿|スキップ|件の障害情報を取得",
                msg,
            )
        ]
        for lv, msg in shown[-40:]:
            print(f"{lv:<8} {msg}")
        print("```")
    else:
        print("\n（チェッカーのログを取得できなかった）")
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
