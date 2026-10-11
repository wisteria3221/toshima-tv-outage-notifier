#!/usr/bin/env python3
"""Healthchecks.io の Management API と Ping API を操作する CLI。

標準ライブラリのみで動作する（Python 3.10 以上）。
認証情報は環境変数または引数で受け取り、ログや dry-run 出力ではマスクする。

使用例:
    hc.py checks list --tag prod
    hc.py checks create --name backup --slug backup --timeout 3600 --grace 900 --unique slug
    hc.py checks get <uuid>
    hc.py checks pause <uuid>
    hc.py ping <uuid>
    hc.py ping backup --ping-key <key> --kind fail

環境変数:
    HEALTHCHECKS_API_KEY   Management API キー（プロジェクト設定で発行。読み書き or 読み取り専用）
    HEALTHCHECKS_PING_KEY  Ping Key（slug で ping する場合に必要）
    HEALTHCHECKS_BASE_URL  セルフホスト時の URL（既定: https://healthchecks.io）
    HEALTHCHECKS_PING_URL  ping 先の URL（既定: https://hc-ping.com。セルフホストで BASE_URL のみ
                           指定した場合は <BASE_URL>/ping を使う）

    環境変数が未設定の項目は、カレントディレクトリから親へ辿って最初に見つかった
    .env の HEALTHCHECKS_* 行で補う（既に設定済みの環境変数は上書きしない）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://healthchecks.io"
DEFAULT_PING_URL = "https://hc-ping.com"
API_VERSION = "v3"
TIMEOUT_SECONDS = 30

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2

_RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# チェックの作成・更新で受け付けるフィールド。(引数名, API フィールド名, 型)
_CHECK_FIELDS: list[tuple[str, str, str]] = [
    ("name", "name", "str"),
    ("slug", "slug", "str"),
    ("tags", "tags", "str"),
    ("desc", "desc", "str"),
    ("timeout", "timeout", "int"),
    ("grace", "grace", "int"),
    ("schedule", "schedule", "str"),
    ("tz", "tz", "str"),
    ("channels", "channels", "str"),
    ("methods", "methods", "str"),
    ("manual_resume", "manual_resume", "bool"),
    ("start_kw", "start_kw", "str"),
    ("success_kw", "success_kw", "str"),
    ("failure_kw", "failure_kw", "str"),
    ("filter_subject", "filter_subject", "bool"),
    ("filter_body", "filter_body", "bool"),
    ("filter_http_body", "filter_http_body", "bool"),
    ("filter_default_fail", "filter_default_fail", "bool"),
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
    """キーの先頭 4 文字だけ残してマスクする。"""
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "…" + "*" * 4


def _explain_error(err: ApiError) -> str:
    """ステータスコードと操作の文脈から、ありがちな原因を説明する。"""
    path = urllib.parse.urlsplit(err.url).path
    s = err.status
    if s is None:
        return "接続に失敗しました（DNS / ネットワーク / タイムアウト）。--base-url とネットワークを確認してください。"
    if s == 400:
        return "リクエストが不正です。フィールド名・型・値の範囲（timeout/grace は 60〜31536000 秒、slug は a-z0-9-_）を確認してください。"
    if s == 401:
        return "API キーが未指定か無効です。HEALTHCHECKS_API_KEY（プロジェクト設定で発行）を確認してください。"
    if s == 403:
        if err.method == "POST" and path.endswith("/checks/"):
            return "アクセス拒否。読み取り専用キーで書き込みをした、またはプロジェクトのチェック数上限（Free プランは 20 件）に達した可能性があります。"
        return "アクセス拒否。読み取り専用キーで書き込み系エンドポイントを呼んでいないか確認してください。"
    if s == 404:
        if "/pings/" in path:
            return "ping が存在しないか、本文が記録されていません。"
        if "hc-ping" in err.url or "/ping/" in path:
            return "Ping Key と slug の組み合わせに一致するチェックがありません。自動作成するなら --create を付けてください。"
        return "チェックが存在しません。UUID / unique_key、および API キーのプロジェクトが正しいか確認してください。"
    if s == 409:
        if path.endswith("/resume"):
            return "一時停止中でないチェックに resume を呼びました。"
        return "slug が複数のチェックに一致して曖昧です。slug を一意にしてください。"
    if s == 429:
        return "レート制限に達しました（Management API は目安 100 req/分、ping はチェックごと 5 回/分）。"
    if s >= 500:
        return "Healthchecks.io 側のエラーです。しばらく待って再試行してください。"
    return ""


def request(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: dict[str, Any] | str | None = None,
    dry_run: bool = False,
) -> tuple[int, Any] | None:
    """HTTP リクエストを送り (status, parsed_body) を返す。dry_run なら送らずに内容を表示して None を返す。"""
    headers = {"User-Agent": "hc.py (healthchecks-api skill)"}
    if api_key:
        headers["X-Api-Key"] = api_key
    data: bytes | None = None
    if isinstance(body, dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif isinstance(body, str):
        data = body.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"

    if dry_run:
        shown_headers = dict(headers)
        if "X-Api-Key" in shown_headers:
            shown_headers["X-Api-Key"] = _mask(shown_headers["X-Api-Key"])
        preview = {
            "dry_run": True,
            "method": method,
            "url": url,
            "headers": shown_headers,
            "body": body,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return None

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise ApiError(
            e.code, e.read().decode("utf-8", errors="replace"), url, method
        ) from None
    except urllib.error.URLError as e:
        raise ApiError(None, str(e.reason), url, method) from None

    if not raw:
        return status, None
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type:
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            pass
    return status, text


# ---------------------------------------------------------------- 出力


def _emit(payload: Any, *, status: int | None = None, table: str | None = None) -> None:
    """結果を JSON（既定）またはテーブルで出力する。"""
    if (
        table == "checks"
        and isinstance(payload, dict)
        and isinstance(payload.get("checks"), list)
    ):
        _print_checks_table(payload["checks"])
        return
    if (
        table == "channels"
        and isinstance(payload, dict)
        and isinstance(payload.get("channels"), list)
    ):
        rows = [
            (c.get("kind", ""), c.get("name", ""), c.get("id", ""))
            for c in payload["channels"]
        ]
        _print_table(("KIND", "NAME", "ID"), rows)
        return
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload is None:
        print(f"HTTP {status} (本文なし)")
    else:
        print(payload)


def _print_checks_table(checks: list[dict[str, Any]]) -> None:
    rows = []
    for c in checks:
        period = c.get("schedule") or (
            f"{c.get('timeout')}s" if c.get("timeout") is not None else ""
        )
        ident = c.get("uuid") or c.get("unique_key") or ""
        rows.append(
            (
                c.get("status", ""),
                c.get("name", ""),
                c.get("slug", ""),
                str(period),
                f"{c.get('grace', '')}s",
                (c.get("last_ping") or "")[:19],
                ident,
            )
        )
    _print_table(
        ("STATUS", "NAME", "SLUG", "PERIOD", "GRACE", "LAST_PING", "UUID/KEY"), rows
    )


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


def _api_url(
    args: argparse.Namespace, path: str, query: dict[str, Any] | None = None
) -> str:
    base = args.base_url.rstrip("/")
    url = f"{base}/api/{API_VERSION}/{path.lstrip('/')}"
    if query:
        pairs = []
        for k, v in query.items():
            if v is None:
                continue
            if isinstance(v, list):
                pairs.extend((k, str(x)) for x in v)
            else:
                pairs.append((k, str(v)))
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
    return url


def _require_api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("HEALTHCHECKS_API_KEY")
    if not key and not args.dry_run:
        _die(
            "API キーがありません。--api-key か環境変数 HEALTHCHECKS_API_KEY を設定してください。",
            EXIT_USAGE,
        )
    return key or ""


def _die(message: str, code: int) -> None:
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(code)


def _collect_check_fields(args: argparse.Namespace) -> dict[str, Any]:
    """引数からチェックのフィールド辞書を組み立てる。未指定のものは含めない（更新時に既存値を保つため）。"""
    fields: dict[str, Any] = {}
    if getattr(args, "json", None):
        raw = sys.stdin.read() if args.json == "-" else args.json
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as e:
            _die(f"--json の JSON が不正です: {e}", EXIT_USAGE)
        if not isinstance(extra, dict):
            _die("--json にはオブジェクトを渡してください。", EXIT_USAGE)
        fields.update(extra)
    for arg_name, api_name, _kind in _CHECK_FIELDS:
        value = getattr(args, arg_name, None)
        if value is not None:
            fields[api_name] = value
    if getattr(args, "unique", None):
        fields["unique"] = args.unique
    return fields


def _add_check_field_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", help="チェック名（100 文字まで）")
    parser.add_argument("--slug", help="slug（a-z 0-9 - _ のみ、100 文字まで）")
    parser.add_argument("--tags", help="スペース区切りのタグ")
    parser.add_argument("--desc", help="説明（10000 文字まで）")
    parser.add_argument(
        "--timeout", type=int, help="期待する ping 間隔（秒、60〜31536000）"
    )
    parser.add_argument("--grace", type=int, help="猶予時間（秒、60〜31536000）")
    parser.add_argument(
        "--schedule", help="cron 式または systemd OnCalendar 式。timeout より優先される"
    )
    parser.add_argument("--tz", help="schedule のタイムゾーン（例: Asia/Tokyo）")
    parser.add_argument(
        "--channels",
        help='通知先。"*" で全 integration、名前/UUID のカンマ区切り、"" で解除（更新時）',
    )
    parser.add_argument(
        "--methods",
        choices=["", "POST"],
        help='受け付ける ping メソッド。"" = HEAD/GET/POST、"POST" = POST のみ',
    )
    parser.add_argument(
        "--manual-resume",
        dest="manual_resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="down 後に手動 resume を必須にする",
    )
    parser.add_argument(
        "--start-kw", dest="start_kw", help="開始とみなすキーワード（カンマ区切り）"
    )
    parser.add_argument(
        "--success-kw", dest="success_kw", help="成功とみなすキーワード（カンマ区切り）"
    )
    parser.add_argument(
        "--failure-kw", dest="failure_kw", help="失敗とみなすキーワード（カンマ区切り）"
    )
    parser.add_argument(
        "--filter-subject",
        dest="filter_subject",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="メールの件名でキーワード判定する",
    )
    parser.add_argument(
        "--filter-body",
        dest="filter_body",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="メールの本文でキーワード判定する",
    )
    parser.add_argument(
        "--filter-http-body",
        dest="filter_http_body",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="HTTP ping の本文でキーワード判定する",
    )
    parser.add_argument(
        "--filter-default-fail",
        dest="filter_default_fail",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="キーワード不一致を失敗扱いにする",
    )
    parser.add_argument(
        "--json",
        help="追加フィールドを JSON オブジェクトで指定（'-' で標準入力）。個別引数が優先される",
    )


# ---------------------------------------------------------------- サブコマンド


def cmd_checks_list(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    url = _api_url(args, "checks/", {"tag": args.tag or None, "slug": args.slug})
    result = request("GET", url, api_key=key, dry_run=args.dry_run)
    if result:
        _emit(result[1], status=result[0], table="checks" if args.table else None)
    return EXIT_OK


def cmd_checks_get(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET", _api_url(args, f"checks/{args.check}"), api_key=key, dry_run=args.dry_run
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_checks_create(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    fields = _collect_check_fields(args)
    result = request(
        "POST",
        _api_url(args, "checks/"),
        api_key=key,
        body=fields,
        dry_run=args.dry_run,
    )
    if result:
        status, payload = result
        verb = (
            "作成しました"
            if status == 201
            else "既存のチェックを更新しました（unique 一致）"
        )
        print(f"# {verb} (HTTP {status})", file=sys.stderr)
        _emit(payload, status=status)
    return EXIT_OK


def cmd_checks_update(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    fields = _collect_check_fields(args)
    if not fields:
        _die("更新するフィールドが指定されていません。", EXIT_USAGE)
    result = request(
        "POST",
        _api_url(args, f"checks/{args.uuid}"),
        api_key=key,
        body=fields,
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_checks_pause(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "POST",
        _api_url(args, f"checks/{args.uuid}/pause"),
        api_key=key,
        body={},
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_checks_resume(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "POST",
        _api_url(args, f"checks/{args.uuid}/resume"),
        api_key=key,
        body={},
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_checks_delete(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    if not args.yes and not args.dry_run:
        _die(
            "削除は取り消せません。checks get で対象の内容を確認し、その内容をユーザーに見せて同意を得てから --yes を付けて再実行してください。",
            EXIT_USAGE,
        )
    result = request(
        "DELETE",
        _api_url(args, f"checks/{args.uuid}"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if result:
        print("# 削除しました。以下は削除直前の内容です。", file=sys.stderr)
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_pings_list(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET",
        _api_url(args, f"checks/{args.uuid}/pings/"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_pings_body(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET",
        _api_url(args, f"checks/{args.uuid}/pings/{args.n}/body"),
        api_key=key,
        dry_run=args.dry_run,
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_flips(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    url = _api_url(
        args,
        f"checks/{args.check}/flips/",
        {"seconds": args.seconds, "start": args.start, "end": args.end},
    )
    result = request("GET", url, api_key=key, dry_run=args.dry_run)
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET", _api_url(args, "channels/"), api_key=key, dry_run=args.dry_run
    )
    if result:
        _emit(result[1], status=result[0], table="channels" if args.table else None)
    return EXIT_OK


def cmd_badges(args: argparse.Namespace) -> int:
    key = _require_api_key(args)
    result = request(
        "GET", _api_url(args, "badges/"), api_key=key, dry_run=args.dry_run
    )
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    result = request("GET", _api_url(args, "status/"), dry_run=args.dry_run)
    if result:
        _emit(result[1], status=result[0])
    return EXIT_OK


def cmd_ping(args: argparse.Namespace) -> int:
    """Ping API に成功 / start / fail / log / 終了コードを送る。UUID か「Ping Key + slug」で宛先を決める。"""
    ping_base = (args.ping_url or os.environ.get("HEALTHCHECKS_PING_URL") or "").rstrip(
        "/"
    )
    if not ping_base:
        base = args.base_url.rstrip("/")
        ping_base = DEFAULT_PING_URL if base == DEFAULT_BASE_URL else f"{base}/ping"

    if _RE_UUID.match(args.target):
        url = f"{ping_base}/{args.target}"
        if args.create:
            _die("--create は slug 指定（Ping Key 使用時）でのみ有効です。", EXIT_USAGE)
    else:
        ping_key = args.ping_key or os.environ.get("HEALTHCHECKS_PING_KEY")
        if not ping_key and not args.dry_run:
            _die(
                "slug で ping するには --ping-key か環境変数 HEALTHCHECKS_PING_KEY が必要です。",
                EXIT_USAGE,
            )
        url = f"{ping_base}/{ping_key or '<PING_KEY>'}/{args.target}"

    kind = args.kind
    if kind == "success":
        suffix = ""
    elif kind in ("start", "fail", "log"):
        suffix = f"/{kind}"
    elif kind.isdigit() and 0 <= int(kind) <= 255:
        suffix = f"/{int(kind)}"
    else:
        _die(
            "--kind は success / start / fail / log または 0〜255 の終了コードです。",
            EXIT_USAGE,
        )
    url += suffix
    query: dict[str, Any] = {}
    if args.create:
        query["create"] = 1
    if args.rid:
        query["rid"] = args.rid
    if query:
        url += "?" + urllib.parse.urlencode(query)

    body = args.body if args.body is not None else None
    method = "POST" if body is not None else "GET"
    result = request(method, url, body=body, dry_run=args.dry_run)
    if result:
        status, payload = result
        note = " (チェックを自動作成)" if status == 201 else ""
        print(f"HTTP {status}{note}: {payload if payload is not None else ''}".rstrip())
    return EXIT_OK


# ---------------------------------------------------------------- パーサ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hc.py",
        description="Healthchecks.io Management API / Ping API クライアント（標準ライブラリのみ）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("環境変数:", 1)[1]
        if "環境変数:" in (__doc__ or "")
        else None,
    )
    parser.add_argument(
        "--api-key", help="Management API キー（既定: 環境変数 HEALTHCHECKS_API_KEY）"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("HEALTHCHECKS_BASE_URL", DEFAULT_BASE_URL),
        help=f"API のベース URL（既定: {DEFAULT_BASE_URL}。セルフホスト用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="リクエストを送らず、送る内容（キーはマスク）を表示する",
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

    sub = parser.add_subparsers(dest="command", required=True)

    # checks
    checks = sub.add_parser(
        "checks", help="チェックの一覧・取得・作成・更新・一時停止・再開・削除"
    )
    checks_sub = checks.add_subparsers(dest="subcommand", required=True)

    p = checks_sub.add_parser("list", help="チェック一覧", parents=[common])
    p.add_argument(
        "--tag", action="append", help="タグで絞り込み（複数指定可、AND 条件）"
    )
    p.add_argument("--slug", help="slug で絞り込み（完全一致）")
    p.add_argument("--table", action="store_true", help="JSON ではなく表で表示")
    p.set_defaults(func=cmd_checks_list)

    p = checks_sub.add_parser("get", help="チェック 1 件を取得", parents=[common])
    p.add_argument("check", help="UUID または unique_key（読み取り専用キー用）")
    p.set_defaults(func=cmd_checks_get)

    p = checks_sub.add_parser(
        "create", help="チェックを作成（--unique で upsert）", parents=[common]
    )
    _add_check_field_args(p)
    p.add_argument(
        "--unique",
        action="append",
        choices=["name", "slug", "tags", "timeout", "grace"],
        help="一致判定に使うフィールド。一致する既存チェックがあれば作成せず更新する（複数指定可）",
    )
    p.set_defaults(func=cmd_checks_create)

    p = checks_sub.add_parser(
        "update",
        help="チェックを更新（指定したフィールドのみ変更）",
        parents=[common],
    )
    p.add_argument("uuid")
    _add_check_field_args(p)
    p.set_defaults(func=cmd_checks_update)

    p = checks_sub.add_parser("pause", help="監視を一時停止", parents=[common])
    p.add_argument("uuid")
    p.set_defaults(func=cmd_checks_pause)

    p = checks_sub.add_parser(
        "resume",
        help="一時停止中のチェックを再開（manual_resume のチェックにも使う）",
        parents=[common],
    )
    p.add_argument("uuid")
    p.set_defaults(func=cmd_checks_resume)

    p = checks_sub.add_parser(
        "delete",
        help="チェックを削除（取り消し不可、--yes 必須）",
        parents=[common],
    )
    p.add_argument("uuid")
    p.add_argument("--yes", action="store_true", help="削除を実行する")
    p.set_defaults(func=cmd_checks_delete)

    # pings
    pings = sub.add_parser("pings", help="ping 履歴")
    pings_sub = pings.add_subparsers(dest="subcommand", required=True)
    p = pings_sub.add_parser(
        "list", help="ping 一覧（新しい順。Free は最新 100 件）", parents=[common]
    )
    p.add_argument("uuid")
    p.set_defaults(func=cmd_pings_list)
    p = pings_sub.add_parser("body", help="ping の本文を取得", parents=[common])
    p.add_argument("uuid")
    p.add_argument("n", type=int, help="ping 番号（pings list の n）")
    p.set_defaults(func=cmd_pings_body)

    # flips
    p = sub.add_parser("flips", help="状態変化（up/down）の履歴", parents=[common])
    p.add_argument("check", help="UUID または unique_key")
    p.add_argument("--seconds", type=int, help="直近 N 秒")
    p.add_argument("--start", type=int, help="この UNIX 時刻より新しいもの")
    p.add_argument("--end", type=int, help="この UNIX 時刻より古いもの")
    p.set_defaults(func=cmd_flips)

    # channels / badges / status
    p = sub.add_parser("channels", help="通知先（integration）一覧", parents=[common])
    p.add_argument("--table", action="store_true", help="表で表示")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("badges", help="タグ別のバッジ URL 一覧", parents=[common])
    p.set_defaults(func=cmd_badges)

    p = sub.add_parser(
        "status", help="Healthchecks.io 自身の稼働確認（認証不要）", parents=[common]
    )
    p.set_defaults(func=cmd_status)

    # ping
    p = sub.add_parser(
        "ping",
        help="Ping API に通知を送る（成功/start/fail/log/終了コード）",
        parents=[common],
    )
    p.add_argument(
        "target", help="チェックの UUID、または slug（slug の場合は Ping Key が必要）"
    )
    p.add_argument(
        "--kind",
        default="success",
        help="success（既定）/ start / fail / log / 0〜255 の終了コード",
    )
    p.add_argument(
        "--ping-key", help="Ping Key（既定: 環境変数 HEALTHCHECKS_PING_KEY）"
    )
    p.add_argument(
        "--ping-url",
        help=f"ping 先ベース URL（既定: 環境変数 HEALTHCHECKS_PING_URL または {DEFAULT_PING_URL}）",
    )
    p.add_argument(
        "--create",
        action="store_true",
        help="slug に一致するチェックが無ければ自動作成する（?create=1）",
    )
    p.add_argument("--rid", help="実行 ID（UUID）。start と success/fail を対応付ける")
    p.add_argument(
        "--body", help="ping 本文（指定すると POST で送る。ログ等を添付する用途）"
    )
    p.set_defaults(func=cmd_ping)

    return parser


def load_dotenv_defaults(start: str | None = None) -> str | None:
    """.env から HEALTHCHECKS_* だけを読み、未設定の環境変数を補う。

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
        if not line.startswith("HEALTHCHECKS_") or "=" not in line:
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
