#!/usr/bin/env python3
"""
前后端 API 契约对照器（Frontend/Backend Contract Checker）

目标：扫描后端实际注册的路由路径，与前端实际调用的 /api/... 路径做对照，
自动发现：
  - 前端调用了、但后端没有注册的路由（运行时必 404）
  - 后端注册了、但前端从未调用的孤儿路由（低优先，仅提示）

路径归一化：把 {id} / ${id} / {account_id} / ${selectedAccount.value} 等
路径参数统一替换为占位符，使结构相同即视为匹配；忽略查询字符串。

用法：
    python3 tools/contract_check.py
退出码：发现「前端缺后端」返回 1，否则 0（便于 CI / pytest 回归）。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BILIBOT = PROJECT_ROOT / "bilibot"
WEB_PY = BILIBOT / "web"
STATIC_JS = WEB_PY / "static" / "js"

# 后端路由字符串提取：Route("/api/...") 或 Route(path="/api/...") 或 @x.get("/api/...")
_ROUTE_RE = re.compile(r"""Route\(\s*(?:path\s*=\s*)?["'](/api/[^"']+)["']""")
_DECOR_RE = re.compile(r"""\.(get|post|put|delete|patch|head|options)\(\s*["'](/api/[^"']+)["']""")
# 前端调用字符串提取：/api/... 直到空白/引号/括号
_FRONT_RE = re.compile(r"""(?<![A-Za-z0-9_/])(/api/[^"')\s`]+)""")


def norm(path: str) -> str:
    """归一化 API 路径：脱去查询串、统一路径参数占位。"""
    path = path.split("?")[0].split("#")[0]
    # 先处理 ${...} 模板，再处理 {…}
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    # 合并相邻占位（模板字符串拼接产生的 {}{} → {}）
    path = re.sub(r"[{}]+", "{}", path)
    return path.rstrip("/") or "/"


def extract_backend():
    paths = set()
    for py in sorted(BILIBOT.rglob("*.py")):
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _ROUTE_RE.findall(text):
            paths.add(norm(m))
        for _meth, p in _DECOR_RE.findall(text):
            paths.add(norm(p))
    return paths


def extract_frontend():
    raw = []
    if STATIC_JS.exists():
        for js in sorted(STATIC_JS.rglob("*.js")):
            try:
                text = js.read_text(encoding="utf-8")
            except Exception:
                continue
            # 去除 // 行注释与 /* */ 块注释，避免注释文字被误判为路由
            text = re.sub(r"//[^\n]*", "", text)
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            # 把 ${...} 模板占位统一替换为 {}，避免模板拼接导致路径断裂
            text = re.sub(r"\$\{[^}]*\}", "{}", text)
            for m in _FRONT_RE.findall(text):
                # 丢弃仍包含非路径字符（如中文/逗号）的脏片段
                if m.startswith("/api/") and re.fullmatch(r"/api/[A-Za-z0-9/_{}.\-]*", m):
                    raw.append(m)
    return raw


def main():
    backend = extract_backend()
    frontend_raw = extract_frontend()
    frontend = {norm(p) for p in frontend_raw}

    missing = sorted(frontend - backend)   # 前端调用但后端无 -> 404
    orphan = sorted(backend - frontend)     # 后端有但前端没用 -> 低优先

    print(f"后端注册路由(归一化去重): {len(backend)}")
    print(f"前端调用路径(归一化去重): {len(frontend)}")

    if missing:
        print(f"\n✗ 前端调用了但后端未注册的路由（{len(missing)} 条，运行时 404）：")
        for p in missing:
            print(f"  - {p}")
    else:
        print("\n✓ 前端调用的路由后端均有注册")

    if orphan:
        print(f"\n⚠ 后端注册但前端未调用的孤儿路由（{len(orphan)} 条，仅供参考）：")
        for p in orphan:
            print(f"  - {p}")

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
