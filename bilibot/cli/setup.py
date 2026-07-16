"""Interactive minimal setup wizard for --quickstart."""

from __future__ import annotations

import os
from typing import Any, Dict


def run_quickstart(config: dict, config_path: str) -> None:
    """Interactive minimal setup: prompts for admin password, optional sessdata, writes yaml."""
    use_rich = False
    try:
        from rich.console import Console

        console = Console()
        use_rich = True

        def _print(msg: str) -> None:
            console.print(msg)

        def _ask(prompt: str, default: str = "") -> str:
            suffix = f" [{default}]" if default else ""
            value = console.input(f"{prompt}{suffix}: ").strip()
            return value if value else default
    except ImportError:
        def _print(msg: str) -> None:
            print(msg)

        def _ask(prompt: str, default: str = "") -> str:
            suffix = f" [{default}]" if default else ""
            value = input(f"{prompt}{suffix}: ").strip()
            return value if value else default

    import yaml

    _print("[bold]BiliBot 快速配置向导[/]" if use_rich else "BiliBot 快速配置向导")
    _print(f"配置文件: {config_path}")
    _print("直接回车保留当前值；仅填写需要修改的项。")

    cfg: Dict[str, Any] = dict(config) if isinstance(config, dict) else {}
    web = dict(cfg.get("web") or {})
    bili = dict(cfg.get("bilibili") or {})

    current_user = str(web.get("admin_username") or "admin")
    current_pwd = str(web.get("admin_password") or "")
    pwd_hint = "(已设置)" if current_pwd else "(未设置)"

    admin_user = _ask("Web 管理员用户名", current_user)
    admin_pwd = _ask(f"Web 管理员密码 {pwd_hint}", "")
    sessdata = _ask("B站 SESSDATA（可选，可稍后在面板扫码）", str(bili.get("sessdata") or ""))
    bili_jct = _ask("B站 bili_jct（可选）", str(bili.get("bili_jct") or ""))

    if admin_user:
        web["admin_username"] = admin_user
    if admin_pwd:
        web["admin_password"] = admin_pwd
    web.setdefault("enabled", True)
    web.setdefault("host", "127.0.0.1")
    web.setdefault("port", 8080)

    if sessdata:
        bili["sessdata"] = sessdata
    if bili_jct:
        bili["bili_jct"] = bili_jct

    cfg["web"] = web
    if bili:
        cfg["bilibili"] = bili

    parent = os.path.dirname(os.path.abspath(config_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, config_path)
    try:
        os.chmod(config_path, 0o600)
    except Exception:
        pass

    _print(f"已写入 {config_path}")
    _print("下一步: python -m bilibot --config " + config_path)
