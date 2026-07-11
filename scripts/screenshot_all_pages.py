"""
scripts/screenshot_all_pages.py - Playwright 视觉验证：截取所有页面截图

用法：
    python scripts/screenshot_all_pages.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_temp_config(tmp_dir, port):
    data_dir_str = str(tmp_dir / "data").replace("\\", "/")
    config_content = f"""bilibili:
  sessdata: ""
  bili_jct: ""
  dede_user_id: ""
llm:
  api_key: ""
  base_url: "http://localhost:8000/v1"
  model: "test"
web:
  enabled: true
  host: "127.0.0.1"
  port: {port}
  secret_key: "screenshot-secret"
  admin_username: "admin"
  admin_password: "test123"
data_dir: "{data_dir_str}"
reply:
  auto_reply: false
proactive:
  video_count: 0
  dynamic_count: 0
"""
    config_path = tmp_dir / "config.yaml"
    config_path.write_text(config_content, encoding="utf-8")
    return config_path


def main():
    repo_root = Path(__file__).parent.parent
    shot_dir = repo_root / "tests" / "screenshots" / "golden-time"
    shot_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bilibot_shot_") as tmp:
        tmp_dir = Path(tmp)
        port = _find_free_port()
        config_path = _write_temp_config(tmp_dir, port)
        base_url = f"http://127.0.0.1:{port}"

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            [sys.executable, "-m", "bilibot", "--config", str(config_path)],
            cwd=str(repo_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        try:
            # wait for server
            deadline = time.time() + 15.0
            ready = False
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"{base_url}/api/status/public", timeout=2.0) as r:
                        if r.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.5)
            if not ready:
                print("[shot] Server not ready")
                return
            print(f"[shot] Server ready at {base_url}")

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()

                # Login
                page.goto(f"{base_url}/login", wait_until="networkidle")
                page.fill('input[name="username"], input[placeholder*="用户名"]', "admin")
                page.fill('input[type="password"]', "test123")
                page.click('button:has-text("登录"), button[type="submit"]')
                page.wait_for_timeout(3000)

                # Pages to screenshot: (hash_route, filename, description)
                pages = [
                    ("#/login", "00_login.png", "登录页"),
                    ("#/", "01_overview.png", "总览页"),
                    ("#/accounts", "02_accounts.png", "账号管理"),
                    ("#/personas", "03_personas.png", "人格管理"),
                    ("#/llm", "04_llm.png", "LLM 管理"),
                    ("#/memory/list", "05_memory_list.png", "记忆列表"),
                    ("#/memory/graph", "06_memory_graph_2d.png", "记忆图谱 2D"),
                    ("#/memory/graph-3d", "07_memory_graph_3d.png", "记忆图谱 3D"),
                    ("#/memory/recall", "08_memory_recall.png", "召回测试"),
                    ("#/comments", "09_comments.png", "评论"),
                    ("#/logs", "10_logs.png", "日志"),
                    ("#/proactive", "11_proactive.png", "主动行为"),
                    ("#/drafts", "12_drafts.png", "动态草稿"),
                    ("#/image-gen", "13_image_gen.png", "文生图"),
                    ("#/video-analysis", "14_video_analysis.png", "视频理解"),
                    ("#/config", "15_config.png", "全局配置"),
                    ("#/system", "16_system.png", "系统设置"),
                ]

                for route, filename, desc in pages:
                    if route == "#/login":
                        # Logout to capture login page
                        continue
                    page.goto(f"{base_url}/{route}")
                    page.wait_for_load_state("networkidle", timeout=5000)
                    page.wait_for_timeout(1000)  # extra render time
                    shot_path = str(shot_dir / filename)
                    page.screenshot(path=shot_path, full_page=True)
                    print(f"  [OK] {desc} → {filename}")

                # Capture login page separately (logout first)
                page.goto(f"{base_url}/login", wait_until="networkidle")
                page.wait_for_timeout(500)
                page.screenshot(path=str(shot_dir / "00_login.png"), full_page=True)
                print(f"  [OK] 登录页 → 00_login.png")

                # Responsive layout test (≤ 1100px → single column)
                page.goto(f"{base_url}/login", wait_until="networkidle")
                page.fill('input[name="username"], input[placeholder*="用户名"]', "admin")
                page.fill('input[type="password"]', "test123")
                page.click('button:has-text("登录"), button[type="submit"]')
                page.wait_for_timeout(2000)
                page.set_viewport_size({"width": 900, "height": 700})
                page.goto(f"{base_url}/#/")
                page.wait_for_timeout(1000)
                page.screenshot(path=str(shot_dir / "17_responsive.png"), full_page=True)
                print(f"  [OK] 响应式布局 → 17_responsive.png")

                # Sidebar active state test
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(f"{base_url}/#/accounts")
                page.wait_for_timeout(1000)
                page.screenshot(path=str(shot_dir / "18_sidebar_active.png"), full_page=True)
                print(f"  [OK] 侧边栏激活状态 → 18_sidebar_active.png")

                browser.close()
                print(f"\n[shot] 截图保存至: {shot_dir}")
                print(f"[shot] 共 {len(list(shot_dir.glob('*.png')))} 张截图")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()
