#!/usr/bin/env python3
"""
前后端 API 契约对照器（Frontend/Backend Contract Checker）

目标：扫描后端实际注册的路由路径，与前端实际调用的 /api/... 路径做对照，
自动发现：
  - 前端调用了、但后端没有注册的路由（运行时必 404）
  - 前端方法与后端注册方法不匹配（如 POST 打到仅 GET 的路径）
  - 前端仍调用弃用根级 memory/tasks 写路径（应 410 / 禁止）
  - 后端注册了、但前端从未调用的孤儿路由（低优先，仅提示）

路径归一化：把 {id} / ${id} / {account_id} / ${selectedAccount.value} 等
路径参数统一替换为占位符，使结构相同即视为匹配；忽略查询字符串。

用法：
    python tools/contract_check.py
    python tools/contract_check.py --json
退出码：发现「前端缺后端」或方法不匹配或弃用根级写 返回 1，否则 0。

基线协议：docs/progress/FULL_CHAIN_REVIEW_PROTOCOL.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BILIBOT = PROJECT_ROOT / "bilibot"
WEB_PY = BILIBOT / "web"
STATIC_JS = WEB_PY / "static" / "js"

# 后端：Route("/api/...", ..., methods=["GET", "POST"])
_ROUTE_RE = re.compile(
    r"""Route\(\s*(?:path\s*=\s*)?["'](/api/[^"']+)["']"""
    r"""(?:[^)]*methods\s*=\s*\[([^\]]*)\])?""",
    re.S,
)
# 装饰器风格：@x.get("/api/...")
_DECOR_RE = re.compile(
    r"""\.(get|post|put|delete|patch|head|options)\(\s*["'](/api/[^"']+)["']"""
)
# 前端路径字面量
_FRONT_RE = re.compile(r"""(?<![A-Za-z0-9_/])(/api/[^"')\s`]+)""")
_PATH_OK_RE = re.compile(r"/api/[A-Za-z0-9/_{}.\-]*")
# 前端方法推断：api.get/post/... 或 method: 'POST' 近邻
_API_METHOD_RE = re.compile(
    r"""(?:api|request)\.(get|post|put|patch|delete)\s*\(\s*[`'"](/api/[^`'"]+)"""
)
_REQUEST_METHOD_RE = re.compile(
    r"""request\s*\(\s*[`'"](/api/[^`'"]+)[`'"]\s*,\s*\{[^}]*?method\s*:\s*['"](GET|POST|PUT|PATCH|DELETE)['"]""",
    re.I | re.S,
)
_FETCH_METHOD_RE = re.compile(
    r"""fetch\s*\(\s*[`'"](/api/[^`'"]+)[`'"]\s*,\s*\{[^}]*?method\s*:\s*['"](GET|POST|PUT|PATCH|DELETE)['"]""",
    re.I | re.S,
)
_METHOD_LIT_RE = re.compile(r"""["'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["']""", re.I)

# 已知弃用根级写路径：后端有意 410 Gone，前端不得再调用。
_DEPRECATED_ROOT_MEMORY_WRITES = frozenset(
    {
        "/api/memory/search",  # POST gone; GET 仍可能存在
        "/api/memory/recall",
        "/api/memory/graph/query",
        "/api/memory/reindex",
        "/api/memory/jobs/{}/retry",
        "/api/memory/migrate",
        "/api/memory/{}",
        "/api/tasks/proactive-video",
        "/api/tasks/dynamic",
    }
)

# 字段级历史契约点（人类复核清单，不参与 exit code）
_FIELD_CONTRACT_NOTES = [
    {
        "id": "QR",
        "path": "/api/accounts/{}/qr-login",
        "fields": (
            "POST init → qr_session_id, qrcode_url, qrcode_data_url, status=created; "
            "GET poll → status(created|scanned|confirmed|expired|cancelled); "
            "终态再 poll → 410"
        ),
    },
    {
        "id": "BACKUP_DL",
        "path": "/api/backup/{}/download",
        "fields": "GET blob; list/download 须跳过 .creating staging; 401 回跳 login+hash",
    },
    {
        "id": "TASKS",
        "path": "/api/accounts/{}/tasks",
        "fields": (
            "GET list items; "
            "POST .../proactive-video|dynamic → 202 + data.task_id/status/scene/account_id "
            "(dynamic scene=dynamic_post); 无 task_id 不得 toast 成功"
        ),
    },
    {
        "id": "DRAFT_REV",
        "path": "/api/accounts/{}/dynamic-drafts/{}/approve|patch",
        "fields": "approve/patch body.expected_revision 必填; reject body.note (optional expected_revision)",
    },
    {
        "id": "COMPANION_TRIGGER",
        "path": "/api/accounts/{}/companion/trigger",
        "fields": (
            "POST body.action∈tick|diary|dream|explore|creative|plan; "
            "envelope success + data.produced + data.action + message; "
            "失败 success:false; 未产出 produced=false 勿误报成功"
        ),
    },
    {
        "id": "MEMORY_ACCOUNT",
        "path": "/api/accounts/{}/memory/*",
        "fields": (
            "写/检索仅账号前缀; 根级 /api/memory POST|DELETE 410; "
            "GET recall=traces 与 POST recall 同路径 methods 分流; "
            "recall-traces 为 traces 别名（P3 orphan 可忽略）"
        ),
    },
    {
        "id": "TOKEN_USAGE",
        "path": "/api/token-usage/today|summary",
        "fields": "GET; today 按 account_id 过滤; 响应可附 scene_labels",
    },
    {
        "id": "LOGS_DL",
        "path": "/api/logs/download",
        "fields": "GET blob/text; 401 回跳 login+hash; 路径限制 data_dir",
    },
    {
        "id": "SAFETY",
        "path": "/api/safety/pause-status|pause|resume|blacklist",
        "fields": (
            "GET pause-status; POST pause/resume; "
            "GET/POST blacklist; DELETE blacklist/{user_id}"
        ),
    },
    {
        "id": "LOGIN_CSRF",
        "path": "/api/login|/api/logout",
        "fields": (
            "POST login/logout 须 X-Requested-With; "
            "401 存 bilibot_login_return hash 后跳 /login"
        ),
    },
    {
        "id": "AUDIT",
        "path": "/api/audit/generations|stats|analytics",
        "fields": "GET generations list/detail; GET stats; GET analytics（概览 KPI）",
    },
    {
        "id": "DRAFT_RETRY",
        "path": "/api/accounts/{}/dynamic-drafts/{}/retry|approve",
        "fields": (
            "approve/retry → 202 + publish_task_id/task 字段; "
            "前端无 task_id 不得 toast 发布成功"
        ),
    },
]


def _configure_stdio() -> None:
    """Windows GBK 控制台避免特殊符号触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(errors="replace")
            except Exception:
                pass


def _print(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def norm(path: str) -> str:
    """归一化 API 路径：脱去查询串、统一路径参数占位。"""
    path = path.split("?")[0].split("#")[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    path = re.sub(r"(\{\})+", "{}", path)
    path = path.rstrip("/") or "/"
    return path


def _parse_methods(methods_raw: str | None) -> set[str]:
    if not methods_raw:
        return {"GET"}  # Starlette Route 默认 GET
    found = {m.group(1).upper() for m in _METHOD_LIT_RE.finditer(methods_raw)}
    return found or {"GET"}


def extract_backend_methods(project_root: Path | None = None) -> dict[str, set[str]]:
    """path_norm -> set of HTTP methods."""
    root = project_root or PROJECT_ROOT
    bilibot = root / "bilibot"
    by_path: dict[str, set[str]] = defaultdict(set)
    if not bilibot.is_dir():
        return {}
    for py in sorted(bilibot.rglob("*.py")):
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _ROUTE_RE.finditer(text):
            p = norm(m.group(1))
            methods = _parse_methods(m.group(2))
            by_path[p] |= methods
        for meth, p in _DECOR_RE.findall(text):
            by_path[norm(p)].add(meth.upper())
    return dict(by_path)


def extract_backend(project_root: Path | None = None) -> set[str]:
    return set(extract_backend_methods(project_root).keys())


def extract_frontend(project_root: Path | None = None) -> list[str]:
    root = project_root or PROJECT_ROOT
    static_js = root / "bilibot" / "web" / "static" / "js"
    raw: list[str] = []
    if not static_js.exists():
        return raw
    for js in sorted(static_js.rglob("*.js")):
        try:
            text = js.read_text(encoding="utf-8")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"\$\{[^}]*\}", "{}", text)
        for m in _FRONT_RE.findall(text):
            path = m.split("?")[0].split("#")[0]
            if path.startswith("/api/") and _PATH_OK_RE.fullmatch(path):
                raw.append(path)
    return raw


def extract_frontend_methods(project_root: Path | None = None) -> dict[str, set[str]]:
    """尽力从 api.get/post 与 request/fetch method 推断前端方法。

    仅覆盖能静态解析的调用；裸路径字面量不进入方法表（仍走路径 missing 检查）。
    """
    root = project_root or PROJECT_ROOT
    static_js = root / "bilibot" / "web" / "static" / "js"
    by_path: dict[str, set[str]] = defaultdict(set)
    if not static_js.exists():
        return {}
    for js in sorted(static_js.rglob("*.js")):
        try:
            text = js.read_text(encoding="utf-8")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        # 模板 ${} → {}
        text_t = re.sub(r"\$\{[^}]*\}", "{}", text)
        for meth, p in _API_METHOD_RE.findall(text_t):
            path = norm(p.split("?")[0])
            by_path[path].add(meth.upper())
        for p, meth in _REQUEST_METHOD_RE.findall(text_t):
            by_path[norm(p.split("?")[0])].add(meth.upper())
        for p, meth in _FETCH_METHOD_RE.findall(text_t):
            by_path[norm(p.split("?")[0])].add(meth.upper())
        # fetch('/api/...') 无 method → GET
        for m in re.finditer(
            r"""fetch\s*\(\s*[`'"](/api/[^`'"]+)[`'"]\s*(?:,\s*\{([^}]*)\})?""",
            text_t,
            re.S,
        ):
            p = norm(m.group(1).split("?")[0])
            body = m.group(2) or ""
            mm = re.search(r"""method\s*:\s*['"](GET|POST|PUT|PATCH|DELETE)['"]""", body, re.I)
            by_path[p].add(mm.group(1).upper() if mm else "GET")
    return dict(by_path)


def check(project_root: Path | None = None) -> dict:
    """对照前后端路径与方法，返回结构化结果。

    致命项（ok=False）：
      - missing：前端路径后端无
      - method_mismatch：前端方法后端未注册
      - frontend_deprecated_root_writes：前端调弃用根级写
    孤儿路由仅记证。
    """
    root = project_root or PROJECT_ROOT
    backend_methods = extract_backend_methods(root)
    backend = set(backend_methods.keys())
    frontend_raw = extract_frontend(root)
    frontend = {norm(p) for p in frontend_raw}
    frontend_methods = extract_frontend_methods(root)

    missing = sorted(frontend - backend)

    method_mismatch: list[dict] = []
    for path, methods in sorted(frontend_methods.items()):
        if path not in backend_methods:
            continue  # 已由 missing 覆盖
        allowed = backend_methods[path]
        for meth in sorted(methods):
            if meth not in allowed:
                method_mismatch.append(
                    {
                        "path": path,
                        "method": meth,
                        "backend_methods": sorted(allowed),
                    }
                )

    orphan = sorted(backend - frontend)
    deprecated_orphans = sorted(
        p
        for p in orphan
        if p in _DEPRECATED_ROOT_MEMORY_WRITES
        or p.startswith("/api/memory")
        or p in {"/api/tasks/proactive-video", "/api/tasks/dynamic"}
    )
    frontend_root_writes = sorted(p for p in frontend if p in _DEPRECATED_ROOT_MEMORY_WRITES)

    # 账号级 memory 写路径是否被前端使用（正向健康信号）
    account_memory_used = sorted(
        p for p in frontend if p.startswith("/api/accounts/{}/memory")
    )

    ok = (
        len(missing) == 0
        and len(method_mismatch) == 0
        and len(frontend_root_writes) == 0
    )

    return {
        "ok": ok,
        "backend_count": len(backend),
        "frontend_count": len(frontend),
        "missing": missing,
        "method_mismatch": method_mismatch,
        "orphan": orphan,
        "orphan_count": len(orphan),
        "frontend_deprecated_root_writes": frontend_root_writes,
        "notes": {
            "deprecated_root_orphans": deprecated_orphans,
            "frontend_deprecated_root_writes": frontend_root_writes,
            "account_memory_prefix": "/api/accounts/{}/memory",
            "account_companion_prefix": "/api/accounts/{}/companion",
            "account_tasks_proactive": "/api/accounts/{}/tasks/proactive-video",
            "account_tasks_dynamic": "/api/accounts/{}/tasks/dynamic",
            "account_memory_used": account_memory_used,
            "field_contracts": _FIELD_CONTRACT_NOTES,
            "panel_aggregates": (
                "memory+account_memory+accounts+drafts+companion+tasks(410)+"
                "backup+safety+token_usage+audit+config+…"
            ),
        },
        "project_root": str(root),
        "severity": {
            "missing": "P1",
            "method_mismatch": "P1",
            "frontend_deprecated_root_writes": "P1",
            "orphan": "P3",
        },
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="前后端 /api 契约对照")
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出结果（便于日志与 CI）",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="项目根目录（默认：本工具所在仓库根）",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else PROJECT_ROOT
    result = check(root)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print(f"后端注册路由(归一化去重): {result['backend_count']}")
        _print(f"前端调用路径(归一化去重): {result['frontend_count']}")

        if result["missing"]:
            _print(
                f"\n[FAIL] 前端调用了但后端未注册的路由"
                f"（{len(result['missing'])} 条，P1 运行时 404）："
            )
            for p in result["missing"]:
                _print(f"  - {p}")
        else:
            _print("\n[OK] 前端调用的路由后端均有注册")

        mismatches = result.get("method_mismatch") or []
        if mismatches:
            _print(
                f"\n[FAIL] HTTP 方法不匹配（{len(mismatches)} 条，P1）："
            )
            for item in mismatches:
                _print(
                    f"  - {item['method']} {item['path']}"
                    f"  backend={item['backend_methods']}"
                )
        else:
            _print("[OK] 可解析的前端方法与后端 methods 一致")

        notes = result.get("notes") or {}
        if notes.get("frontend_deprecated_root_writes"):
            _print(
                "\n[FAIL] 前端仍在调用弃用根级写路径（应改为 /api/accounts/{}/...）："
            )
            for p in notes["frontend_deprecated_root_writes"]:
                _print(f"  - {p}")

        if result["orphan"]:
            dep = set(notes.get("deprecated_root_orphans") or [])
            live_orphans = [p for p in result["orphan"] if p not in dep]
            _print(
                f"\n[WARN] 后端注册但前端未调用的孤儿路由"
                f"（共 {len(result['orphan'])} 条；其中弃用根级 {len(dep)} 条可忽略）："
            )
            for p in live_orphans[:40]:
                _print(f"  - {p}")
            if len(live_orphans) > 40:
                _print(f"  … 另有 {len(live_orphans) - 40} 条省略")
            if dep:
                _print(f"  (弃用根级 orphan 已记证 {len(dep)} 条，P3)")

        used = notes.get("account_memory_used") or []
        if used:
            _print(f"\n[INFO] 账号级 memory 路径已被前端使用: {len(used)} 条")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
