#!/usr/bin/env python3
"""障害チェックの稼働状況を、Actions・Healthchecks・cron-job.org の 3 か所から集めて並べる。

Healthchecks と cron-job.org は同じリポジトリのスキル（healthchecks-api / cron-job-org-api）の
CLI を呼ぶ。キーはそれらと同じく環境変数か .env から読まれる。キーが無い取得元は
「取得できず」と表示して残りを続ける。

cron-job.org の API は既定で 1 日 100 リクエストまで。このスクリプトは 1 回の実行で 2 回使う。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
HC = SKILLS / "healthchecks-api" / "scripts" / "hc.py"
CJ = SKILLS / "cron-job-org-api" / "scripts" / "cj.py"
OUTAGE_WORKFLOW = "check-outage.yml"
LINT_WORKFLOW = "lint.yml"
_JST = timezone(timedelta(hours=9))

# cron-job.org の実行結果コード（history の status）
CJ_STATUS = {
    0: "不明",
    1: "成功",
    2: "DNS 失敗",
    3: "接続失敗",
    4: "HTTP エラー",
    5: "タイムアウト",
    6: "応答が大きすぎる",
    7: "URL が不正",
    8: "cron-job.org 内部エラー",
    9: "不明なエラー",
}

_ERROR_LINE = re.compile(r" - (ERROR|CRITICAL) - (.*)|##\[error\](.*)")


def fmt(t: datetime) -> str:
    return (
        f"{t.astimezone(UTC):%m-%d %H:%M} UTC（JST {t.astimezone(_JST):%m-%d %H:%M}）"
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_arg_time(value: str) -> datetime:
    """引数の時刻を読む。時差の無い値はユーザーの感覚に合わせて JST とみなす。"""
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=_JST)).astimezone(UTC)


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip().splitlines()[-1:] or ["失敗"]
        )
    return result.stdout


def run_json(cmd: list[str]):
    return json.loads(run(cmd))


# GitHub の API はページ送りの Link ヘッダーに数値 ID 形式の URL（repositories/<id>/...）を返す。
# `gh run list` や `gh api --paginate` はそれをたどるため、owner/repo 形式しか通さない
# プロキシ（クラウドのセッションなど）では 2 ページ目以降で 403 になる。
# そこでページ番号を自分で付けて、常に repos/{owner}/{repo}/... の形で要求する。
MAX_RUNS = 1000
_PER_PAGE = 100


def gh_api(path: str):
    return run_json(["gh", "api", path])


def to_run(r: dict) -> dict:
    """REST のラン表現を、このスクリプトで使う形（gh run list --json と同じ名前）に揃える。"""
    return {
        "databaseId": r["id"],
        "event": r["event"],
        "status": r["status"],
        "conclusion": r["conclusion"],
        "createdAt": r["created_at"],
        "headSha": r["head_sha"],
    }


def list_runs(workflow: str, since: datetime, until: datetime) -> list[dict]:
    created = (
        f"{since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
        f"..{until.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
    )
    runs: list[dict] = []
    for page in range(1, MAX_RUNS // _PER_PAGE + 1):
        batch = gh_api(
            f"repos/{{owner}}/{{repo}}/actions/workflows/{workflow}/runs"
            f"?per_page={_PER_PAGE}&page={page}&created={created}"
            "&exclude_pull_requests=true"
        )["workflow_runs"]
        runs += [to_run(r) for r in batch]
        if len(batch) < _PER_PAGE:
            break
    return runs


def run_jobs(run_id: int) -> list[dict]:
    return gh_api(f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs")["jobs"]


def job_log(job_id: int) -> str:
    return run(["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs"])


def truncation_note(times: list[datetime], since: datetime, source: str) -> None:
    """取得件数の上限で期間の始めまで届いていないとき、その旨を出す（黙って短い期間を見せない）。"""
    if times and min(times) > since + timedelta(minutes=15):
        print(
            f"- 注意: {source}は取得件数の上限のため {fmt(min(times))} 以降しか見えていない"
        )


# ------------------------------------------------------------------ Actions


def actions_section(since: datetime, until: datetime) -> dict:
    all_runs = list_runs(OUTAGE_WORKFLOW, since, until)
    runs = [r for r in all_runs if since <= parse_time(r["createdAt"]) <= until]
    runs.sort(key=lambda r: r["createdAt"])

    print("## Actions（Check Toshima TV Outage）\n")
    if not runs:
        print("- 期間内のランが **1 件も無い**\n")
    else:
        by_event = Counter(r["event"] for r in runs)
        by_result = Counter(r["conclusion"] or r["status"] for r in runs)
        print(
            f"- ラン数: {len(runs)}（{', '.join(f'{k} {v}' for k, v in by_event.items())}）"
        )
        print(f"- 結果: {', '.join(f'{k} {v}' for k, v in by_result.items())}")
        print(
            f"- 最初: {fmt(parse_time(runs[0]['createdAt']))} / 最後: {fmt(parse_time(runs[-1]['createdAt']))}"
        )
        if len(all_runs) >= MAX_RUNS:
            truncation_note(
                [parse_time(r["createdAt"]) for r in runs], since, "Actions "
            )
        gaps = []
        for a, b in pairwise(runs):
            gap = parse_time(b["createdAt"]) - parse_time(a["createdAt"])
            if gap > timedelta(minutes=25):
                gaps.append((parse_time(a["createdAt"]), gap))
        for start, gap in gaps:
            print(
                f"- **起動の空白**: {fmt(start)} の後 {int(gap.total_seconds() // 60)} 分ランが無い"
            )
        print()

    failed = [r for r in runs if r["conclusion"] not in ("success", None, "")]
    if failed:
        print(f"失敗したラン（新しい順に最大 3 件のエラー行）: {len(failed)} 件\n")
        for r in reversed(failed[-3:]):
            print(
                f"- ラン {r['databaseId']}（{fmt(parse_time(r['createdAt']))}、{r['conclusion']}、"
                f"headSha `{r['headSha'][:7]}`）"
            )
            jobs = run_jobs(r["databaseId"])
            if not jobs:
                # ジョブが 1 つも起動していない。checker も Healthchecks への報告も動いていないので
                # ping が途絶え、続けば Healthchecks が down になる
                same_sha_ok = any(
                    o["headSha"] == r["headSha"] and o["conclusion"] == "success"
                    for o in runs
                )
                cause = (
                    "同じコミットのランは成功しているので GitHub 側の一時的な起動失敗の可能性が高い"
                    if same_sha_ok
                    else "同じコミットで成功したランが無い。そのコミットのワークフロー定義の誤りを疑う"
                )
                print(f"    - ジョブが起動していない（ログ無し）。{cause}")
                continue
            try:
                log = "\n".join(
                    job_log(j["id"]) for j in jobs if j["conclusion"] == "failure"
                )
                lines = []
                for raw in log.splitlines():
                    if "[36;1m" in raw:  # スクリプト本文のエコーは除く
                        continue
                    if m := _ERROR_LINE.search(raw):
                        lines.append(next(g for g in m.groups()[1:] if g))
                for line in lines[-5:]:
                    print(f"    - {line.strip()[:200]}")
                if not lines:
                    print(
                        "    - （ERROR 行なし。`gh run view <id> --log-failed` で全体を見る）"
                    )
            except RuntimeError as e:
                print(f"    - ログを取得できず: {e}")
        print()

    lint = gh_api(
        f"repos/{{owner}}/{{repo}}/actions/workflows/{LINT_WORKFLOW}/runs"
        "?branch=main&per_page=1"
    )["workflow_runs"]
    lint = [to_run(r) for r in lint]
    if lint:
        result = lint[0]["conclusion"] or "実行中"
        mark = (
            ""
            if result == "success"
            else " **← 壊れたコードのまま障害チェックが動いている可能性**"
        )
        print(
            f"Lint and Test（main の最新）: {result}（`{lint[0]['headSha'][:7]}`、{fmt(parse_time(lint[0]['createdAt']))}）{mark}\n"
        )
    return {"runs": runs}


# ------------------------------------------------------------- ログの走査

# checker のログ行: "<ランナー時刻> <JST 時刻>,<ms> - <logger> - <LEVEL> - <本文>"
_LOG_LINE = re.compile(r" - (?:[\w.]+) - (WARNING|ERROR|CRITICAL) - (.*)$")
_ANNOTATION = re.compile(r"##\[(warning|error)\](.*)$")
_ENV_LINE = re.compile(r"^\S+\s+(EXIT_CODE|COMMIT_OUTCOME): ?(.*)$")
_NOTIFIED = re.compile(r" - INFO - (.+を通知しました: .*)$")
_ARCHIVE_SUMMARY = re.compile(
    r" - (?:INFO|WARNING) - (魚拓を \d+ 件取得、\d+ 件失敗.*)$"
)
_SCAN_WORKERS = 8


def _normalize(message: str) -> str:
    """件数や時刻だけが違う同種のメッセージを 1 つにまとめるため、数字を N に置き換える。"""
    return re.sub(r"\d+", "N", message.strip())[:160]


def _scan_one(r: dict) -> dict:
    out = {
        "run": r,
        "exit": None,
        "commit": None,
        "lines": [],
        "notified": [],
        "archive": [],
        "error": None,
    }
    try:
        jobs = run_jobs(r["databaseId"])
        if not jobs:
            out["exit"] = out["commit"] = "ジョブが起動していない"
        for job in jobs:
            for raw in job_log(job["id"]).splitlines():
                if "[36;1m" in raw:  # スクリプト本文のエコーは除く
                    continue
                if m := _ENV_LINE.search(raw):
                    key = "exit" if m[1] == "EXIT_CODE" else "commit"
                    out[key] = m[2].strip() or "（空）"
                elif m := _LOG_LINE.search(raw):
                    out["lines"].append((m[1], m[2]))
                elif m := _ANNOTATION.search(raw):
                    out["lines"].append((m[1].upper(), m[2]))
                if m := _NOTIFIED.search(raw):
                    out["notified"].append(m[1].strip())
                if m := _ARCHIVE_SUMMARY.search(raw):
                    out["archive"].append(m[1].strip())
    except RuntimeError as e:
        out["error"] = str(e)
    return out


def scan_logs_section(runs: list[dict]) -> None:
    """全ランのログを読み、終了コード・commit の結果・WARNING/ERROR・通知・魚拓を集計する。

    conclusion が success でも、終了コード 2（上流不通）や魚拓の失敗、レート制限による
    スキップは WARNING としてログにしか出ないため、それらを拾う。
    """
    from concurrent.futures import ThreadPoolExecutor

    print("## ログの走査（全ラン）\n")
    if not runs:
        print("- 対象のランが無い\n")
        return
    with ThreadPoolExecutor(_SCAN_WORKERS) as pool:
        results = list(pool.map(_scan_one, runs))

    unread = [x for x in results if x["error"]]
    print(f"- 読んだラン: {len(results) - len(unread)} / {len(results)} 件")
    for x in unread[:5]:
        print(f"    - ラン {x['run']['databaseId']} のログを取得できず: {x['error']}")

    exits = Counter(
        x["exit"] or "不明（Healthchecks への報告ステップが動いていない）"
        for x in results
        if not x["error"]
    )
    print(
        f"- 終了コード: {', '.join(f'{k}: {v} 件' for k, v in sorted(exits.items()))}"
    )
    commits = Counter(x["commit"] or "不明" for x in results if not x["error"])
    print(
        f"- state のコミット: {', '.join(f'{k}: {v} 件' for k, v in sorted(commits.items()))}"
    )
    if commits.get("failure"):
        print(
            "    - **commit=failure がある。state.json を push できておらず二重投稿の恐れ**"
        )

    groups: dict[tuple[str, str], list] = {}
    for x in results:
        for level, message in x["lines"]:
            groups.setdefault((level, _normalize(message)), []).append(
                (x["run"], message)
            )
    if not groups:
        print("- WARNING / ERROR: なし")
    else:
        print(f"- WARNING / ERROR: {len(groups)} 種類")
        for (level, _), hits in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            first = parse_time(hits[0][0]["createdAt"])
            last = parse_time(hits[-1][0]["createdAt"])
            span = fmt(first) if first == last else f"{fmt(first)} 〜 {fmt(last)}"
            print(f"    - {level} ×{len(hits)}（{span}）: {hits[-1][1].strip()[:200]}")

    notified = [(x["run"], n) for x in results for n in x["notified"]]
    print(f"- 通知（X への投稿）: {len(notified)} 件")
    for r, n in notified[:20]:
        print(f"    - {fmt(parse_time(r['createdAt']))}: {n[:150]}")
    archive = [(x["run"], a) for x in results for a in x["archive"]]
    if archive:
        print(f"- 魚拓: {len(archive)} 回")
        for r, a in archive[:10]:
            print(f"    - {fmt(parse_time(r['createdAt']))}: {a[:150]}")
    print()


# -------------------------------------------------------------- Healthchecks


def healthchecks_section(since: datetime, until: datetime, check: str | None) -> None:
    print("## Healthchecks\n")
    try:
        checks = run_json([sys.executable, str(HC), "checks", "list"])["checks"]
    except (RuntimeError, json.JSONDecodeError, KeyError) as e:
        print(f"- 取得できず（`HEALTHCHECKS_API_KEY` 未設定など）: {e}\n")
        return
    if check:
        checks = [
            c for c in checks if check in (c.get("uuid"), c.get("name"), c.get("slug"))
        ]
    else:
        checks = [c for c in checks if "toshima" in c.get("name", "")] or checks[:1]
    if not checks:
        print(
            "- 対象のチェックが見つからない（`--hc-check` で uuid か名前を指定する）\n"
        )
        return
    c = checks[0]
    uuid = c.get("uuid") or c.get("unique_key")
    print(
        f"- 状態: **{c['status']}**（Period {c.get('timeout')} 秒 / Grace {c.get('grace')} 秒）"
    )
    if c.get("last_ping"):
        print(f"- 最終 ping: {fmt(parse_time(c['last_ping']))}")

    try:
        seconds = int((datetime.now(UTC) - since).total_seconds())
        flips = run_json(
            [sys.executable, str(HC), "flips", uuid, "--seconds", str(seconds)]
        )["flips"]
        for f in sorted(flips, key=lambda f: f["timestamp"]):
            if parse_time(f["timestamp"]) > until:
                continue
            print(
                f"- {'up に戻った' if f['up'] else '**down になった**'}: {fmt(parse_time(f['timestamp']))}"
            )
    except (RuntimeError, json.JSONDecodeError, KeyError) as e:
        print(f"- flips を取得できず: {e}")

    try:
        pings = run_json([sys.executable, str(HC), "pings", "list", uuid])["pings"]
    except (RuntimeError, json.JSONDecodeError, KeyError) as e:
        print(f"- ping 履歴を取得できず: {e}\n")
        return
    if len(pings) >= 100:
        truncation_note([parse_time(p["date"]) for p in pings], since, "ping 履歴")
    pings = [p for p in pings if since <= parse_time(p["date"]) <= until]
    kinds = Counter(p["type"] for p in pings)
    print(
        f"- 期間内の ping: {len(pings)} 件（{', '.join(f'{k} {v}' for k, v in kinds.items()) or 'なし'}）"
    )
    # success 以外（fail / log）は本文に run_id と exit_code が入っている
    for p in [p for p in pings if p["type"] != "success"][:10]:
        try:
            body = run(
                [sys.executable, str(HC), "pings", "body", uuid, str(p["n"])]
            ).strip()
        except RuntimeError:
            body = "（本文を取得できず）"
        print(f"    - {p['type']} {fmt(parse_time(p['date']))}: {body[:150]}")
    print()


# -------------------------------------------------------------- cron-job.org


def cronjob_section(since: datetime, until: datetime) -> None:
    print("## cron-job.org\n")
    try:
        jobs = run_json([sys.executable, str(CJ), "jobs", "list"])["jobs"]
    except (RuntimeError, json.JSONDecodeError, KeyError) as e:
        print(f"- 取得できず（`CRONJOB_API_KEY` 未設定など）: {e}\n")
        return
    jobs = [j for j in jobs if OUTAGE_WORKFLOW in j.get("url", "")]
    if not jobs:
        print(f"- `{OUTAGE_WORKFLOW}` を叩くジョブが無い\n")
        return
    job = jobs[0]
    enabled = "有効" if job.get("enabled") else "**無効（一時停止中）**"
    print(f"- ジョブ {job['jobId']}「{job.get('title')}」: {enabled}")
    try:
        hist = run_json([sys.executable, str(CJ), "history", "list", str(job["jobId"])])
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"- 実行履歴を取得できず: {e}\n")
        return
    history = hist.get("history", [])
    entries = [
        h for h in history if since <= datetime.fromtimestamp(h["date"], UTC) <= until
    ]
    if history and min(h["date"] for h in history) > since.timestamp():
        truncation_note(
            [datetime.fromtimestamp(h["date"], UTC) for h in history],
            since,
            "cron-job.org の履歴",
        )
    ok = [
        h
        for h in entries
        if h.get("status") == 1 and 200 <= (h.get("httpStatus") or 0) < 300
    ]
    print(f"- 期間内の実行: {len(entries)} 件（成功 {len(ok)} 件）")
    if entries:
        print(
            f"- 最後の実行: {fmt(datetime.fromtimestamp(max(h['date'] for h in entries), UTC))}"
        )
    for h in [h for h in entries if h not in ok][:10]:
        t = datetime.fromtimestamp(h["date"], UTC)
        print(
            f"    - **失敗** {fmt(t)}: {CJ_STATUS.get(h.get('status'), h.get('status'))}、"
            f"HTTP {h.get('httpStatus')} {h.get('statusText') or ''}"
        )
    if preds := hist.get("predictions"):
        print(f"- 次回予定: {fmt(datetime.fromtimestamp(preds[0], UTC))}")
    print()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--hours", type=float, default=6, help="--from が無いときに遡る時間（既定 6）"
    )
    p.add_argument(
        "--from",
        dest="start",
        help="調べる期間の始め（ISO 8601。時差の無い値は JST とみなす）",
    )
    p.add_argument("--to", dest="end", help="調べる期間の終わり（既定: 現在）")
    p.add_argument(
        "--hc-check",
        help="Healthchecks のチェック（uuid か名前。既定: 名前に toshima を含むもの）",
    )
    p.add_argument(
        "--scan-logs",
        action="store_true",
        help="全ランのログを読み、終了コード・WARNING/ERROR・通知・魚拓を集計する（1 日分で 1 分ほど）",
    )
    p.add_argument(
        "--skip-cronjob",
        action="store_true",
        help="cron-job.org を見ない（1 日 100 リクエストの制限を節約）",
    )
    args = p.parse_args()

    now = datetime.now(UTC)
    until = parse_arg_time(args.end) if args.end else now
    since = (
        parse_arg_time(args.start)
        if args.start
        else until - timedelta(hours=args.hours)
    )
    print(f"# 稼働状況（{fmt(since)} 〜 {fmt(until)}）\n")
    runs: list[dict] = []
    try:
        runs = actions_section(since, until)["runs"]
    except RuntimeError as e:
        print(f"## Actions\n\n- 取得できず: {e}\n")
    if args.scan_logs:
        scan_logs_section(runs)
    healthchecks_section(since, until, args.hc_check)
    if not args.skip_cronjob:
        cronjob_section(since, until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
