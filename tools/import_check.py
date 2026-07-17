#!/usr/bin/env python3
"""
导入完整性扫描器（Import Integrity Checker）

目标：静态扫描 bilibot 包内所有 import / from ... import 语句，
确认被引用的「项目内模块」文件是否真实存在。
可捕获诸如 `from bilibot.cli.setup import run_quickstart` 但
`bilibot/cli/` 目录根本不存在 这类「导入了从未实现过的模块」问题。

用法：
    python tools/import_check.py
    python tools/import_check.py --json
退出码：发现缺失返回 1，否则 0（便于接入 CI / pytest）。

基线协议：docs/progress/FULL_CHAIN_REVIEW_PROTOCOL.md
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BILIBOT = PROJECT_ROOT / "bilibot"


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


def resolve_module(parts: list[str], base: Path) -> bool:
    """检查模块路径（如 ['bilibot','cli','setup']）是否对应真实文件/包。"""
    if not parts:
        return False
    p = base.joinpath(*parts)
    if p.with_suffix(".py").is_file():
        return True
    if (p / "__init__.py").is_file():
        return True
    return False


def collect_imports(py_file: Path, project_root: Path | None = None):
    """返回该文件内所有『项目内』import 目标 [(module_parts, level, lineno)]。"""
    root = project_root or PROJECT_ROOT
    results = []
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] 无法解析 {py_file}: {e}", file=sys.stderr)
        return results

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对导入：基于文件所在目录向上 (level-1) 层
                cur = py_file.parent
                for _ in range(node.level - 1):
                    cur = cur.parent
                try:
                    base_parts = list(cur.relative_to(root).parts)
                except ValueError:
                    continue
                if node.module:
                    base_parts += node.module.split(".")
                if base_parts and base_parts[0] == "bilibot":
                    results.append((base_parts, node.level, node.lineno))
            elif node.module and node.module.startswith("bilibot."):
                results.append((node.module.split("."), 0, node.lineno))
            elif node.module == "bilibot":
                results.append((["bilibot"], 0, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bilibot" or alias.name.startswith("bilibot."):
                    results.append((alias.name.split("."), 0, node.lineno))
    return results


def scan(project_root: Path | None = None) -> dict:
    """扫描并返回结构化结果（可供 CLI 与单测复用）。"""
    root = project_root or PROJECT_ROOT
    bilibot = root / "bilibot"
    missing: list[dict] = []
    checked = 0
    if not bilibot.is_dir():
        return {
            "ok": False,
            "checked": 0,
            "missing": [
                {
                    "file": str(bilibot),
                    "line": 0,
                    "module": "bilibot",
                    "detail": "package missing",
                }
            ],
            "project_root": str(root),
        }
    for py in sorted(bilibot.rglob("*.py")):
        for parts, _level, lineno in collect_imports(py, root):
            checked += 1
            if not resolve_module(parts, root):
                try:
                    rel = str(py.relative_to(root))
                except ValueError:
                    rel = str(py)
                missing.append(
                    {
                        "file": rel,
                        "line": lineno,
                        "module": ".".join(parts),
                    }
                )
    return {
        "ok": len(missing) == 0,
        "checked": checked,
        "missing": missing,
        "project_root": str(root),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="BiliBot 内部导入完整性扫描")
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
    result = scan(root)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print(f"扫描项目内导入引用: {result['checked']} 处")
        if result["ok"]:
            _print("[OK] 未发现缺失的内部模块导入")
        else:
            _print(f"\n[FAIL] 发现 {len(result['missing'])} 处『引用了不存在的内部模块』：")
            for item in result["missing"]:
                _print(f"  - {item['file']}:{item['line']}  模块 '{item['module']}' 不存在")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
