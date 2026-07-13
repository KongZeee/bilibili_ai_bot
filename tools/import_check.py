#!/usr/bin/env python3
"""
导入完整性扫描器（Import Integrity Checker）

目标：静态扫描 bilibot 包内所有 import / from ... import 语句，
确认被引用的「项目内模块」文件是否真实存在。
可捕获诸如 `from bilibot.cli.setup import run_quickstart` 但
`bilibot/cli/` 目录根本不存在 这类「导入了从未实现过的模块」问题。

用法：
    python3 tools/import_check.py
退出码：发现缺失返回 1，否则 0（便于接入 CI / pytest）。
"""
import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BILIBOT = PROJECT_ROOT / "bilibot"


def resolve_module(parts: list[str], base: Path) -> bool:
    """检查 模块路径（如 ['bilibot','cli','setup']）是否对应真实文件/包。"""
    p = base.joinpath(*parts)
    if p.with_suffix(".py").is_file():
        return True
    if (p / "__init__.py").is_file():
        return True
    return False


def collect_imports(py_file: Path):
    """返回该文件内所有『项目内』import 目标 [(module_parts, level, lineno)]。"""
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
                base_parts = list(cur.relative_to(PROJECT_ROOT).parts)
                if node.module:
                    base_parts += node.module.split(".")
                if base_parts and base_parts[0] == "bilibot":
                    results.append((base_parts, node.level, node.lineno))
            elif node.module and node.module.startswith("bilibot."):
                results.append((node.module.split("."), 0, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bilibot."):
                    results.append((alias.name.split("."), 0, node.lineno))
    return results


def main():
    missing = []
    checked = 0
    for py in sorted(BILIBOT.rglob("*.py")):
        for parts, level, lineno in collect_imports(py):
            checked += 1
            if not resolve_module(parts, PROJECT_ROOT):
                missing.append((py, lineno, ".".join(parts)))

    print(f"扫描项目内导入引用: {checked} 处")
    if not missing:
        print("✓ 未发现缺失的内部模块导入")
        return 0

    print(f"\n✗ 发现 {len(missing)} 处『引用了不存在的内部模块』：")
    for py, lineno, mod in missing:
        rel = py.relative_to(PROJECT_ROOT)
        print(f"  - {rel}:{lineno}  模块 '{mod}' 不存在")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
