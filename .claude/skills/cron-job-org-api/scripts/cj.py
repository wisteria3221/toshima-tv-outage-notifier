#!/usr/bin/env python3
"""cron-job.org の REST API を操作する CLI。

標準ライブラリのみで動作する（Python 3.10 以上）。
認証情報は環境変数または引数で受け取り、ジョブのヘッダーに入った秘密値
（Authorization など）と一緒に、出力や dry-run 表示ではマスクする。

使用例:
    cj.py jobs list --table
    cj.py jobs get 1234567
    cj.py history list 1234567 --table
    cj.py jobs update 1234567 --cron '7,37 * * * *' --timezone Asia/Tokyo
    cj.py jobs update 1234567 --header "Authorization: Bearer $NEW_GITHUB_TOKEN"
    cj.py jobs disable 1234567

環境変数:
    CRONJOB_API_KEY   API キー（cron-job.org Console → Settings → API で発行）
    CRONJOB_BASE_URL  API のベース URL（既定: https://api.cron-job.org。テスト用サーバーに向ける用途）

    環境変数が未設定の項目は、カレントディレクトリから親へ辿って最初に見つかった
    .env の CRONJOB_* 行で補う（既に設定済みの環境変数は上書きしない）。

レート制限:
    既定で 1 日 100 リクエストまで（Sustaining member は 5000）。一覧を何度も叩かず、
    jobId が分かったら get / history を直接使う。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://api.cron-job.org"
TIMEOUT_SECONDS = 30

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2

METHODS = [
    "GET",
    "POST",
    "OPTIONS",
    "HEAD",
    "PUT",
    "DELETE",
    "TRACE",
    "CONNECT",
    "PATCH",
]

JOB_STATUS = {
    0: "未実行",
    1: "OK",
    2: "失敗(DNS)",
    3: "失敗(接続不可)",
    4: "失敗(HTTPエラー)",
    5: "失敗(タイムアウト)",
    6: "失敗(応答過大)",
    7: "失敗(URL不正)",
    8: "失敗(内部エラー)",
    9: "失敗(不明)",
    10: "失敗(チャレンジページ)",
}

# 値をマスクするヘッダー名（部分一致・大文字小文字無視）
_RE_SECRET_HEADER = re.compile(
    r"authorization|cookie|token|secret|password|api[-_]?key", re.I
)

# cron 式の各フィールド: (スケジュールのキー, 最小, 最大)
_CRON_FIELDS = [
    ("minutes", 0, 59),
    ("hours", 0, 23),
    ("mdays", 1, 31),
    ("months", 1, 12),
    ("wdays", 0, 6),
]


class ApiError(Exception):
    """HTTP エラーまたは接続エラー。status が None なら接続レベルの失敗。"""

    def __init__(self, status: int | None, body: str, url: str, method: str):
        super().__init__(body)
        self.status = status
        self.body = body
        self.url = url
        self.method = method


def _mask(secret: str) -> str:
    """秘密値をマスクする。"Bearer xxx" のようなスキーム付きはスキームを残す。"""
    scheme, sep, rest = secret.partition(" ")
    if sep and rest and len(scheme) <= 10:
        return f"{scheme} {_mask(rest)}"
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "…" + "*" * 4


def _redact(obj: Any) -> Any:
    """ジョブ表現から秘密値（秘密っぽいヘッダーの値、Basic 認証のパスワード）をマスクした複製を返す。"""
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k == "headers" and isinstance(v, dict):
            out[k] = {
                hk: _mask(str(hv)) if _RE_SECRET_HEADER.search(hk) and hv else hv
                for hk, hv in v.items()
            }
        elif k == "password" and isinstance(v, str) and v:
            out[k] = _mask(v)
        else:
            out[k] = _redact(v)
    return out


def _explain_error(err: ApiError) -> str:
    """ステータスコードと操作の文脈から、ありがちな原因を説明する。"""
    s = err.status
    if s is None:
        return "接続に失敗しました（DNS / ネットワーク / タイムアウト）。--base-url とネットワークを確認してください。"
    if s == 400:
        return "リクエストが不正です。フィールド名・型・値の範囲（schedule の各配列、requestMethod は 0〜8）を確認してください。"
    if s == 401:
        return "API キーが未指定か無効です。CRONJOB_API_KEY（Console → Settings で発行）を確認してください。"
    if s == 403:
        return "アクセス拒否。API キーに IP 制限を付けている場合、今の送信元 IP が許可リストに入っているか確認してください。"
    if s == 404:
        return "ジョブ / 履歴 / フォルダが存在しません。jobId（jobs list で確認）を見直してください。"
    if s == 409:
        return "同じタイトルのフォルダが既にあります。"
    if s == 429:
        return "レート制限に達しました（既定 1 日 100 リクエスト、作成は 1 回/秒・5 回/分）。翌日まで待つか呼び出し回数を減らしてください。"
    if s >= 500:
        return "cron-job.org 側のエラーです。しばらく待って再試行してください。"
    return ""


def request(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> tuple[int, Any] | None:
    """HTTP リクエストを送り (status, parsed_body) を返す。dry_run なら送らずに内容を表示して None を返す。"""
    headers = {"User-Agent": "cj.py (cron-job-org-api skill)"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if dry_run:
        preview = {
            "dry_run": True,
            "method": method,
            "url": url,
            "headers": _redact({"headers": headers})["headers"],
            "body": _redact(body),
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return None

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raise ApiError(
            e.code, e.read().decode("utf-8", errors="replace"), url, method
        ) from None
    except urllib.error.URLError as e:
        raise ApiError(None, str(e.reason), url, method) from None

    if not raw:
        return status, None
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


# ---------------------------------------------------------------- 出力


def _tzinfo(name: str | None) -> dt.tzinfo | None:
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        _die(f"タイムゾーン {name!r} を解釈できません。", EXIT_USAGE)
    return None


def _fmt_ts(ts: Any, tz: dt.tzinfo | None) -> str:
    """UNIX 秒を表示用の日時文字列にする。tz が None ならローカル時刻。"""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "-"
    return (
        dt.datetime.fromtimestamp(ts, tz).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    )


def _fmt_schedule(s: dict[str, Any] | None) -> str:
    """schedule オブジェクトを cron 式風の文字列にする。"""
    if not s:
        return ""
    parts = []
    for key, _lo, _hi in _CRON_FIELDS:
        values = s.get(key) or []
        parts.append("*" if values in ([], [-1]) else ",".join(str(v) for v in values))
    tz = s.get("timezone") or "UTC"
    return " ".join(parts) + f" ({tz})"


def _emit(payload: Any, *, status: int | None = None) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload is None:
        print(f"HTTP {status} (本文なし)")
    else:
        print(payload)


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*header))
    for row in rows:
        print(fmt.format(*(str(c) for c in row)))
    if not rows:
        print("(0 件)")


# ---------------------------------------------------------------- 共通


def _url(args: argparse.Namespace, path: str) -> str:
    return f"{args.base_url.rstrip('/')}/{path.lstrip('/')}"


def _require_api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("CRONJOB_API_KEY")
    if not key and not args.dry_run:
        _die(
            "API キーがありません。--api-key か環境変数 CRONJOB_API_KEY を設定してください。",
            EXIT_USAGE,
        )
    return key or ""


def _die(message: str, code: int) -> None:
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(code)


def parse_cron(expr: str) -> dict[str, list[int]]:
    """5 フィールドの cron 式を cron-job.org の schedule 配列に変換する。

    "*" は [-1]。カンマ区切り・範囲（a-b）・ステップ（*/n, a-b/n）に対応。曜日の 7 は 0（日曜）に読み替える。
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            "cron 式は「分 時 日 月 曜日」の 5 フィールドで指定してください。"
        )
    result: dict[str, list[int]] = {}
    for text, (key, lo, hi) in zip(fields, _CRON_FIELDS, strict=True):
        hi_in = 7 if key == "wdays" else hi
        if text == "*":
            result[key] = [-1]
            continue
        values: set[int] = set()
        for part in text.split(","):
            rng, _, step_s = part.partition("/")
            step = int(step_s) if step_s else 1
            if step < 1:
                raise ValueError(f"ステップが不正です: {part}")
            if rng == "*":
                start, end = lo, hi_in
            elif "-" in rng:
                a, b = rng.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = int(rng)
                end = hi_in if step_s else start
            if not (lo <= start <= hi_in and lo <= end <= hi_in and start <= end):
                raise ValueError(f"{key} の値が範囲外です: {part}（{lo}〜{hi_in}）")
            values.update(range(start, end + 1, step))
        if key == "wdays" and 7 in values:
            values.discard(7)
            values.add(0)
        result[key] = sorted(values)
    return result


def _parse_header(text: str) -> tuple[str, str]:
    name, sep, value = text.partition(":")
    if not sep or not name.strip():
        _die(f"--header は 'Name: Value' 形式で指定してください: {text!r}", EXIT_USAGE)
    return name.strip(), value.strip()


def _collect_job_fields(args: argparse.Namespace) -> dict[str, Any]:
    """引数からジョブのフィールド辞書を組み立てる。未指定のものは含めない（PATCH で既存値を保つため）。

    extendedData（headers / body）はここでは扱わない（_extended_data を参照）。
    """
    job: dict[str, Any] = {}
    if getattr(args, "json", None):
        raw = sys.stdin.read() if args.json == "-" else args.json
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as e:
            _die(f"--json の JSON が不正です: {e}", EXIT_USAGE)
        if not isinstance(extra, dict):
            _die("--json にはジョブのオブジェクトを渡してください。", EXIT_USAGE)
        job.update(extra)

    simple = [
        ("url", "url"),
        ("title", "title"),
        ("enabled", "enabled"),
        ("save_responses", "saveResponses"),
        ("request_timeout", "requestTimeout"),
        ("redirect_success", "redirectSuccess"),
        ("folder_id", "folderId"),
    ]
    for arg_name, api_name in simple:
        value = getattr(args, arg_name, None)
        if value is not None:
            job[api_name] = value
    if getattr(args, "method", None):
        job["requestMethod"] = METHODS.index(args.method)

    schedule: dict[str, Any] = dict(job.get("schedule") or {})
    if getattr(args, "cron", None):
        try:
            schedule.update(parse_cron(args.cron))
        except ValueError as e:
            _die(str(e), EXIT_USAGE)
    if getattr(args, "timezone", None):
        schedule["timezone"] = args.timezone
    if getattr(args, "expires_at", None) is not None:
        schedule["expiresAt"] = args.expires_at
    if schedule:
        job["schedule"] = schedule

    notification: dict[str, Any] = dict(job.get("notification") or {})
    for arg_name, api_name in [
        ("notify_failure", "onFailure"),
        ("notify_failure_count", "onFailureCount"),
        ("notify_success", "onSuccess"),
        ("notify_disable", "onDisable"),
    ]:
        value = getattr(args, arg_name, None)
        if value is not None:
            notification[api_name] = value
    if notification:
        job["notification"] = notification
    return job


def _wants_extended(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "header", None)
        or getattr(args, "remove_header", None)
        or getattr(args, "body", None) is not None
    )


def _extended_data(
    args: argparse.Namespace, current: dict[str, Any] | None
) -> dict[str, Any]:
    """現在の extendedData に --header / --remove-header / --body を重ねた完全な extendedData を返す。

    PATCH で headers だけ送ると body が消える（またはその逆）事故を避けるため、常に両方そろえて送る。
    """
    headers: dict[str, str] = dict((current or {}).get("headers") or {})
    body = (current or {}).get("body") or ""
    for name in getattr(args, "remove_header", None) or []:
        for existing in [k for k in headers if k.lower() == name.lower()]:
            del headers[existing]
    for text in getattr(args, "header", None) or []:
        name, value = _parse_header(text)
        for existing in [k for k in headers if k.lower() == name.lower()]:
            del headers[existing]
        headers[name] = value
    if getattr(args, "body", None) is not None:
        body = args.body
    return {"headers": headers, "body": body}


def _get_job(args: argparse.Namespace, key: str, job_id: int) -> dict[str, Any]:
    result = request("GET", _url(args, f"jobs/{job_id}"), api_key=key)
    assert result is not None
    return (result[1] or {}).get("jobDetails") or {}


# ---------------------------------------------------------------- サブコマンド


def cmd_jobs_list(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request("GET", _url(args, "jobs"), api_key=key, dry_run=args.dry_run)
    if not result:
        return EXIT_OK
    payload = result[1] or {}
    if payload.get("someFailed"):
        print(
            "# 警告: 一部のジョブを取得できませんでした（someFailed=true）。",
            file=sys.stderr,
        )
    if args.table:
        tz = _tzinfo(args.tz)
        rows = []
        for j in payload.get("jobs", []):
            rows.append(
                (
                    str(j.get("jobId", "")),
                    "on" if j.get("enabled") else "off",
                    j.get("title", ""),
                    METHODS[j.get("requestMethod", 0)]
                    if 0 <= j.get("requestMethod", 0) < len(METHODS)
                    else "?",
                    _fmt_schedule(j.get("schedule")),
                    JOB_STATUS.get(j.get("lastStatus", 0), str(j.get("lastStatus"))),
                    _fmt_ts(j.get("lastExecution"), tz),
                    _fmt_ts(j.get("nextExecution"), tz),
                    j.get("url", ""),
                )
            )
        _print_table(
            (
                "JOB_ID",
                "EN",
                "TITLE",
                "METHOD",
                "SCHEDULE(分 時 日 月 曜)",
                "LAST",
                "LAST_RUN",
                "NEXT_RUN",
                "URL",
            ),
            rows,
        )
    else:
        _emit(_redact(payload))
    return EXIT_OK


def cmd_jobs_get(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET", _url(args, f"jobs/{args.job_id}"), api_key=key, dry_run=args.dry_run
    )
    if result:
        payload = result[1]
        _emit(payload if args.show_secrets else _redact(payload))
    return EXIT_OK


def cmd_jobs_create(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    job = _collect_job_fields(args)
    if not job.get("url"):
        _die("--url（または --json の url）は必須です。", EXIT_USAGE)
    if _wants_extended(args):
        job["extendedData"] = _extended_data(args, job.get("extendedData"))
    if "enabled" not in job:
        print(
            "# 注意: --enable を付けていないため、ジョブは無効（enabled=false）で作成されます。",
            file=sys.stderr,
        )
    result = request(
        "PUT", _url(args, "jobs"), api_key=key, body={"job": job}, dry_run=args.dry_run
    )
    if result:
        print(f"# 作成しました (HTTP {result[0]})", file=sys.stderr)
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_jobs_update(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    job = _collect_job_fields(args)
    if _wants_extended(args):
        if key:
            current = _get_job(args, key, args.job_id).get("extendedData")
        else:
            # dry-run でキーが無いと現在値を取れない。指定分だけで組み立てる
            print(
                "# 注意: API キーが無いため現在のヘッダー/本文を取得できず、指定した分だけで表示しています。"
                "実行時は既存のヘッダー/本文と合成して送ります。",
                file=sys.stderr,
            )
            current = None
        job["extendedData"] = _extended_data(args, current)
    if not job:
        _die("更新するフィールドが指定されていません。", EXIT_USAGE)
    result = request(
        "PATCH",
        _url(args, f"jobs/{args.job_id}"),
        api_key=key,
        body={"job": job},
        dry_run=args.dry_run,
    )
    if result:
        print(f"# 更新しました (HTTP {result[0]})", file=sys.stderr)
    return EXIT_OK


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    key = _require_api_key(args)
    result = request(
        "PATCH",
        _url(args, f"jobs/{args.job_id}"),
        api_key=key,
        body={"job": {"enabled": enabled}},
        dry_run=args.dry_run,
    )
    if result:
        state = "有効" if enabled else "無効"
        print(
            f"# ジョブ {args.job_id} を{state}にしました (HTTP {result[0]})",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_jobs_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, True)


def cmd_jobs_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, False)


def cmd_jobs_delete(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    if not args.yes and not args.dry_run:
        _die(
            "削除は取り消せません。jobs get で対象の内容を確認し、その内容をユーザーに見せて同意を得てから --yes を付けて再実行してください。"
            "止めたいだけなら jobs disable を使ってください。",
            EXIT_USAGE,
        )
    result = request(
        "DELETE", _url(args, f"jobs/{args.job_id}"), api_key=key, dry_run=args.dry_run
    )
    if result:
        print(
            f"# ジョブ {args.job_id} を削除しました (HTTP {result[0]})", file=sys.stderr
        )
    return EXIT_OK


def cmd_history_list(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET",
        _url(args, f"jobs/{args.job_id}/history"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if not result:
        return EXIT_OK
    payload = result[1] or {}
    if not args.table:
        _emit(payload)
        return EXIT_OK
    tz = _tzinfo(args.tz)
    rows = []
    for h in payload.get("history", [])[: args.limit]:
        rows.append(
            (
                _fmt_ts(h.get("date"), tz),
                _fmt_ts(h.get("datePlanned"), tz),
                f"{(h.get('jitter') or 0) / 1000:.1f}s",
                JOB_STATUS.get(h.get("status", 0), str(h.get("status"))),
                str(h.get("httpStatus") or "-"),
                f"{h.get('duration', 0)}ms",
                h.get("identifier", ""),
            )
        )
    _print_table(
        ("DATE", "PLANNED", "JITTER", "STATUS", "HTTP", "DURATION", "IDENTIFIER"), rows
    )
    predictions = [_fmt_ts(p, tz) for p in payload.get("predictions", [])]
    if predictions:
        print("\n次回実行予定: " + ", ".join(predictions))
    return EXIT_OK


def cmd_history_get(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET",
        _url(args, f"jobs/{args.job_id}/history/{urllib.parse.quote(args.identifier)}"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_folders_list(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request("GET", _url(args, "folders"), api_key=key, dry_run=args.dry_run)
    if result:
        payload = result[1] or {}
        if args.table:
            rows = [
                (str(f.get("folderId", "")), f.get("title", ""))
                for f in payload.get("folders", [])
            ]
            _print_table(("FOLDER_ID", "TITLE"), rows)
        else:
            _emit(payload)
    return EXIT_OK


def cmd_folders_create(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "PUT",
        _url(args, "folders"),
        api_key=key,
        body={"folder": {"title": args.title}},
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_folders_rename(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "PATCH",
        _url(args, f"folders/{args.folder_id}"),
        api_key=key,
        body={"folder": {"title": args.title}},
        dry_run=args.dry_run,
    )
    if result:
        print(f"# フォルダ名を変更しました (HTTP {result[0]})", file=sys.stderr)
    return EXIT_OK


def cmd_folders_delete(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    if not args.yes and not args.dry_run:
        _die(
            "フォルダの削除は取り消せません（中のジョブはルートに移動します）。同意を得てから --yes を付けて再実行してください。",
            EXIT_USAGE,
        )
    result = request(
        "DELETE",
        _url(args, f"folders/{args.folder_id}"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if result:
        print(
            f"# フォルダ {args.folder_id} を削除しました（中のジョブはルートへ移動）",
            file=sys.stderr,
        )
    return EXIT_OK


# ---------------------------------------------------------------- パーサ


def _add_job_field_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="実行する URL（作成時は必須）")
    parser.add_argument("--title", help="ジョブのタイトル")
    parser.add_argument(
        "--method", type=str.upper, choices=METHODS, help="HTTP メソッド（既定 GET）"
    )
    parser.add_argument(
        "--header",
        action="append",
        metavar="'NAME: VALUE'",
        help="リクエストヘッダーを追加・置換（複数指定可）。更新時は既存ヘッダーと合成する",
    )
    parser.add_argument(
        "--remove-header",
        dest="remove_header",
        action="append",
        metavar="NAME",
        help="リクエストヘッダーを削除（更新時、複数指定可）",
    )
    parser.add_argument("--body", help='リクエスト本文（例: \'{"ref":"main"}\'）')
    parser.add_argument(
        "--cron",
        help="スケジュールを cron 式（分 時 日 月 曜日）で指定。例: '7,37 * * * *'",
    )
    parser.add_argument("--timezone", help="スケジュールのタイムゾーン（既定 UTC）")
    parser.add_argument(
        "--expires-at",
        dest="expires_at",
        type=int,
        help="有効期限 YYYYMMDDhhmmss（ジョブのタイムゾーン）。0 で無期限",
    )
    parser.add_argument(
        "--enable",
        "--enabled",
        dest="enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="有効 / 無効（作成時の既定は無効）",
    )
    parser.add_argument(
        "--save-responses",
        dest="save_responses",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="応答のヘッダーと本文を履歴に保存する",
    )
    parser.add_argument(
        "--request-timeout",
        dest="request_timeout",
        type=int,
        help="タイムアウト秒。-1 で既定値",
    )
    parser.add_argument(
        "--redirect-success",
        dest="redirect_success",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="3xx を成功とみなす",
    )
    parser.add_argument(
        "--folder-id", dest="folder_id", type=int, help="所属フォルダ（0 = ルート）"
    )
    parser.add_argument(
        "--notify-failure",
        dest="notify_failure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="失敗時にメール等で通知する",
    )
    parser.add_argument(
        "--notify-failure-count",
        dest="notify_failure_count",
        type=int,
        help="何回連続で失敗したら通知するか（1 以上）",
    )
    parser.add_argument(
        "--notify-success",
        dest="notify_success",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="失敗後に成功へ戻ったら通知する",
    )
    parser.add_argument(
        "--notify-disable",
        dest="notify_disable",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="失敗続きで自動無効化されたら通知する",
    )
    parser.add_argument(
        "--json",
        help="追加フィールドをジョブの JSON オブジェクトで指定（'-' で標準入力）。個別引数が優先される",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cj.py",
        description="cron-job.org REST API クライアント（標準ライブラリのみ）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("環境変数:", 1)[1]
        if "環境変数:" in (__doc__ or "")
        else None,
    )
    parser.add_argument("--api-key", help="API キー（既定: 環境変数 CRONJOB_API_KEY）")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CRONJOB_BASE_URL", DEFAULT_BASE_URL),
        help=f"API のベース URL（既定: {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="リクエストを送らず、送る内容（秘密値はマスク）を表示する",
    )

    # --dry-run はサブコマンドの後ろに置いても効くようにする。
    # 親の値を上書きしないよう、サブ側の既定値は SUPPRESS にする
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    tz_opt = argparse.ArgumentParser(add_help=False)
    tz_opt.add_argument(
        "--tz", help="表の日時を表示するタイムゾーン（既定: ローカル。例: Asia/Tokyo）"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # jobs
    jobs = sub.add_parser(
        "jobs", help="ジョブの一覧・取得・作成・更新・有効化・無効化・削除"
    )
    jobs_sub = jobs.add_subparsers(dest="subcommand", required=True)

    p = jobs_sub.add_parser("list", help="ジョブ一覧", parents=[common, tz_opt])
    p.add_argument("--table", action="store_true", help="JSON ではなく表で表示")
    p.set_defaults(func=cmd_jobs_list)

    p = jobs_sub.add_parser(
        "get", help="ジョブ詳細（ヘッダー・本文・通知設定を含む）", parents=[common]
    )
    p.add_argument("job_id", type=int)
    p.add_argument(
        "--show-secrets",
        action="store_true",
        help="Authorization などのヘッダー値をマスクせずに表示する",
    )
    p.set_defaults(func=cmd_jobs_get)

    p = jobs_sub.add_parser("create", help="ジョブを作成", parents=[common])
    _add_job_field_args(p)
    p.set_defaults(func=cmd_jobs_create)

    p = jobs_sub.add_parser(
        "update", help="ジョブを更新（指定したフィールドのみ変更）", parents=[common]
    )
    p.add_argument("job_id", type=int)
    _add_job_field_args(p)
    p.set_defaults(func=cmd_jobs_update)

    p = jobs_sub.add_parser("enable", help="ジョブを有効化", parents=[common])
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_jobs_enable)

    p = jobs_sub.add_parser(
        "disable", help="ジョブを無効化（削除はしない）", parents=[common]
    )
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_jobs_disable)

    p = jobs_sub.add_parser(
        "delete", help="ジョブを削除（取り消し不可、--yes 必須）", parents=[common]
    )
    p.add_argument("job_id", type=int)
    p.add_argument("--yes", action="store_true", help="削除を実行する")
    p.set_defaults(func=cmd_jobs_delete)

    # history
    history = sub.add_parser("history", help="実行履歴と次回実行予定")
    history_sub = history.add_subparsers(dest="subcommand", required=True)
    p = history_sub.add_parser(
        "list", help="実行履歴（新しい順）と次回実行予定", parents=[common, tz_opt]
    )
    p.add_argument("job_id", type=int)
    p.add_argument("--table", action="store_true", help="表で表示")
    p.add_argument("--limit", type=int, default=50, help="表に出す件数（既定 50）")
    p.set_defaults(func=cmd_history_list)
    p = history_sub.add_parser(
        "get", help="履歴 1 件の詳細（応答ヘッダー・本文を含む）", parents=[common]
    )
    p.add_argument("job_id", type=int)
    p.add_argument("identifier", help="history list の identifier")
    p.set_defaults(func=cmd_history_get)

    # folders
    folders = sub.add_parser("folders", help="フォルダの一覧・作成・名前変更・削除")
    folders_sub = folders.add_subparsers(dest="subcommand", required=True)
    p = folders_sub.add_parser("list", help="フォルダ一覧", parents=[common])
    p.add_argument("--table", action="store_true", help="表で表示")
    p.set_defaults(func=cmd_folders_list)
    p = folders_sub.add_parser("create", help="フォルダを作成", parents=[common])
    p.add_argument("title")
    p.set_defaults(func=cmd_folders_create)
    p = folders_sub.add_parser("rename", help="フォルダ名を変更", parents=[common])
    p.add_argument("folder_id", type=int)
    p.add_argument("title")
    p.set_defaults(func=cmd_folders_rename)
    p = folders_sub.add_parser(
        "delete",
        help="フォルダを削除（中のジョブはルートへ移動、--yes 必須）",
        parents=[common],
    )
    p.add_argument("folder_id", type=int)
    p.add_argument("--yes", action="store_true", help="削除を実行する")
    p.set_defaults(func=cmd_folders_delete)

    return parser


def load_dotenv_defaults(start: str | None = None) -> str | None:
    """.env から CRONJOB_* だけを読み、未設定の環境変数を補う。

    カレントディレクトリから親へ辿り、最初に見つかった .env を使う。
    読み込んだファイルのパスを返す（見つからなければ None）。
    """
    directory = os.path.abspath(start or os.getcwd())
    while True:
        path = os.path.join(directory, ".env")
        if os.path.isfile(path):
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line.startswith("CRONJOB_") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and not os.environ.get(name):
            os.environ[name] = value
    return path


def main(argv: list[str] | None = None) -> int:
    load_dotenv_defaults()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ApiError as e:
        hint = _explain_error(e)
        print(
            f"エラー: {e.method} {e.url} -> HTTP {e.status if e.status is not None else '接続失敗'}",
            file=sys.stderr,
        )
        if e.body:
            print(f"  応答: {e.body.strip()[:500]}", file=sys.stderr)
        if hint:
            print(f"  ヒント: {hint}", file=sys.stderr)
        return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
