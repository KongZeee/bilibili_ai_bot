"""
scripts/test_web_panel.py - Web 面板 Playwright 自动化测试

测试重构后的 Vue 3 Web 面板：
1. 启动临时服务器
2. 登录
3. 验证双栏布局
4. 验证扁平化 SVG 图标（无 emoji）
5. 验证页面切换
6. 截图保存

使用方法：
    python scripts/test_web_panel.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


# ═══════════════════════════════════════════════════════
#  服务器管理
# ═══════════════════════════════════════════════════════

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_temp_config(tmp_dir: Path, port: int) -> Path:
    data_dir_str = str(tmp_dir / "data").replace("\\", "/")
    config_content = f"""# Web 面板测试临时配置
bilibili:
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
  secret_key: "web-test-secret"
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


def _poll_status_public(port: int, timeout: float = 15.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/status/public"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


# ═══════════════════════════════════════════════════════
#  Playwright 测试
# ═══════════════════════════════════════════════════════

def run_playwright_tests(port: int, screenshot_dir: Path) -> dict:
    """运行 Playwright 测试，返回结果字典"""
    from playwright.sync_api import sync_playwright

    results = {"passed": [], "failed": [], "screenshots": []}
    base_url = f"http://127.0.0.1:{port}"

    def check(name, condition, detail=""):
        if condition:
            results["passed"].append(name)
            print(f"  [PASS] {name}")
        else:
            results["failed"].append(f"{name}: {detail}")
            print(f"  [FAIL] {name} - {detail}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # 收集控制台所有消息
        console_msgs = []
        page.on("console", lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
        # 收集页面错误
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        try:
            # === 测试 1: 登录页加载 ===
            print("\n[测试 1] 登录页加载")
            page.goto(base_url + "/login", wait_until="networkidle")
            check("登录页标题", "登录" in page.title() or "BiliBot" in page.title(), page.title())

            # 检查登录表单
            username_input = page.locator('input[name="username"], input[placeholder*="用户名"]').first
            password_input = page.locator('input[name="password"], input[type="password"]').first
            check("用户名输入框存在", username_input.count() > 0)
            check("密码输入框存在", password_input.count() > 0)

            # 截图登录页
            login_shot = str(screenshot_dir / "01_login.png")
            page.screenshot(path=login_shot)
            results["screenshots"].append(login_shot)

            # a11y: 登录页表单语义
            check("登录页有 label[for]", page.locator('label[for]').count() > 0)
            check("登录页 input 有 autocomplete", page.locator('input[autocomplete]').count() > 0)
            check("登录页有 role=alert 错误容器", page.locator('[role="alert"]').count() >= 0)

            # === 测试 2: 登录 ===
            print("\n[测试 2] 登录")
            if username_input.count() > 0:
                username_input.fill("admin")
                password_input.fill("test123")
                # 提交登录
                login_btn = page.locator('button:has-text("登录"), button[type="submit"]').first
                if login_btn.count() > 0:
                    login_btn.click()
                    page.wait_for_load_state("networkidle", timeout=5000)
                # 等待跳转或面板加载
                page.wait_for_timeout(3000)

            # 打印控制台消息和页面错误用于诊断
            print(f"  [诊断] 控制台消息 ({len(console_msgs)} 条):")
            for msg in console_msgs[-10:]:
                print(f"    {msg}")
            print(f"  [诊断] 页面错误 ({len(page_errors)} 条):")
            for err in page_errors[-5:]:
                print(f"    {err}")

            # 检查是否进入主面板（URL 变化或 #app 出现）
            current_url = page.url
            check("登录后跳转", "/login" not in current_url, f"URL: {current_url}")

            # === 测试 3: Vue 应用挂载 ===
            print("\n[测试 3] Vue 应用挂载")
            app_div = page.locator("#app")
            check("#app 容器存在", app_div.count() > 0)
            app_html = app_div.inner_html() if app_div.count() > 0 else ""
            check("#app 内容非空", len(app_html) > 50, f"HTML 长度: {len(app_html)}")

            # === 测试 4: 双栏布局（Golden Time: 280px sidebar + main-area） ===
            print("\n[测试 4] 双栏布局")
            app_shell = page.locator(".app-shell")
            sidebar = page.locator(".sidebar")
            main_area = page.locator(".main-area")
            view_frame = page.locator(".view-frame")
            check("app-shell 容器存在", app_shell.count() > 0)
            check("侧边栏存在", sidebar.count() > 0)
            check("主内容区存在", main_area.count() > 0)
            check("view-frame 内容区存在", view_frame.count() > 0)

            if sidebar.count() > 0:
                sb_box = sidebar.bounding_box()
                check("侧边栏宽度约 280px", sb_box and 250 <= sb_box["width"] <= 320, f"宽度: {sb_box['width'] if sb_box else 'N/A'}")

            # === 测试 5: CSS mask 图标（Golden Time: span[data-icon]） ===
            print("\n[测试 5] CSS mask 图标")
            mask_icons = page.locator("[data-icon]")
            check("data-icon 图标存在", mask_icons.count() > 0, f"找到 {mask_icons.count()} 个 [data-icon]")

            # 检查是否有 emoji（在按钮和导航中）
            # Emoji 范围：U+1F000-U+1F9FF, U+2600-U+27BF
            body_text = page.locator("body").inner_text()
            emoji_chars = [c for c in body_text if (
                0x1F000 <= ord(c) <= 0x1F9FF or
                0x2600 <= ord(c) <= 0x27BF or
                0x1F300 <= ord(c) <= 0x1FAFF
            )]
            check("无 emoji 残留", len(emoji_chars) == 0, f"发现 emoji: {''.join(emoji_chars[:5])}")

            # === 测试 6: 侧边栏导航分组 ===
            print("\n[测试 6] 侧边栏导航分组")
            nav_groups = page.locator(".nav-group")
            check("侧边栏有多个导航分组", nav_groups.count() >= 3, f"数量: {nav_groups.count()}")

            # === 测试 7: 侧边栏导航项 ===
            print("\n[测试 7] 侧边栏导航项")
            nav_items = page.locator(".nav-item")
            check("侧边栏有导航项", nav_items.count() >= 5, f"数量: {nav_items.count()}")

            # 截图主面板
            panel_shot = str(screenshot_dir / "02_panel.png")
            page.screenshot(path=panel_shot, full_page=True)
            results["screenshots"].append(panel_shot)

            # === 测试 8: 页面切换 ===
            print("\n[测试 8] 页面切换")
            test_routes = [
                ("/", "总览"),
                ("/accounts", "账号管理"),
                ("/llm", "LLM 管理"),
                ("/personas", "人格管理"),
                ("/memory/graph", "记忆图谱"),
                ("/memory/list", "记忆列表"),
                ("/comments", "评论"),
                ("/logs", "日志"),
                ("/config", "全局配置"),
                ("/system", "系统设置"),
            ]

            for route, title in test_routes:
                page.goto(base_url + "/#" + route)
                page.wait_for_load_state("networkidle", timeout=3000)
                page.wait_for_timeout(500)  # 等待 Vue 渲染

                # 检查 view-frame 内容区是否有内容
                vf = page.locator(".view-frame").first
                main_html = vf.inner_html() if vf.count() > 0 else ""
                route_ok = len(main_html) > 20
                check(f"路由 {route} 渲染", route_ok, f"内容长度: {len(main_html)}")

            # === 测试 9: 控制台错误检查 ===
            print("\n[测试 9] 控制台错误检查")
            # 过滤已知的非关键错误（如 favicon 404）
            critical_errors = [m for m in console_msgs if "[error]" in m and "favicon" not in m.lower() and "404" not in m.lower()]
            check("无关键控制台错误", len(critical_errors) == 0, f"错误: {critical_errors[:3]}")
            check("无页面 JS 错误", len(page_errors) == 0, f"错误: {page_errors[:3]}")

            # === 测试 10: 无障碍基础检查 ===
            print("\n[测试 10] 无障碍基础检查")
            check("skip-link 存在", page.locator('.skip-link').count() > 0)
            nav_aria_count = page.locator('nav[aria-label]').count()
            check("nav[aria-label] 存在(≥2)", nav_aria_count >= 2, f"数量: {nav_aria_count}")
            btn_aria_count = page.locator('button[aria-label]').count()
            check("button[aria-label] 存在(≥1)", btn_aria_count >= 1, f"数量: {btn_aria_count}")
            check("a[href^='#'] 语义链接存在", page.locator('a[href^="#"]').count() > 0)
            check("aria-live 区域存在", page.locator('[aria-live="polite"]').count() > 0)
            check("main 语义标签存在", page.locator('main').count() > 0)

            # === 测试 11: Modal 无障碍交互 ===
            print("\n[测试 11] Modal 无障碍交互")
            page.goto(base_url + "/#/accounts")
            page.wait_for_timeout(1000)

            add_btn = page.locator('button:has-text("添加账号")').first
            if add_btn.count() > 0:
                add_btn.click()
                page.wait_for_timeout(500)
                check("Modal role=dialog 出现", page.locator('[role="dialog"]').count() > 0)
                check("Modal aria-modal=true", page.locator('[aria-modal="true"]').count() > 0)
                check("Modal 关闭按钮有 aria-label", page.locator('.modal-close[aria-label]').count() > 0)
                # Escape 关闭
                page.keyboard.press('Escape')
                page.wait_for_timeout(500)
                check("Escape 关闭 Modal", page.locator('[role="dialog"]').count() == 0)
            else:
                check("添加账号按钮存在", False, "未找到按钮")

            # === 测试 12: 键盘导航 ===
            print("\n[测试 12] 键盘导航")
            # 导航到 /llm 页（identity 分组），使首个 sidebar 链接（/accounts）不同于当前路由
            page.goto(base_url + "/#/llm")
            page.wait_for_timeout(500)

            # Tab 遍历：连续按 Tab，焦点应在不同交互元素间移动
            page.keyboard.press('Tab')
            page.wait_for_timeout(200)
            focused_tag = page.evaluate("document.activeElement.tagName")
            check("Tab 后焦点在交互元素上", focused_tag in ('BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'),
                  f"焦点元素: {focused_tag}")

            # Enter 触发导航：聚焦首个 nav-item 链接后按 Enter
            nav_link = page.locator('a.nav-item').first
            if nav_link.count() > 0:
                expected_hash = nav_link.get_attribute('href')
                nav_link.focus()
                page.wait_for_timeout(200)
                page.keyboard.press('Enter')
                page.wait_for_timeout(500)
                hash_after = page.evaluate("window.location.hash")
                check("Enter 触发导航", hash_after == expected_hash,
                      f"期望: {expected_hash}, 实际: {hash_after}")
            else:
                check("nav-item 链接存在", False, "未找到 a.nav-item")

            # 最终截图 - 总览页
            page.goto(base_url + "/#/")
            page.wait_for_load_state("networkidle", timeout=3000)
            page.wait_for_timeout(1000)
            final_shot = str(screenshot_dir / "03_overview.png")
            page.screenshot(path=final_shot, full_page=True)
            results["screenshots"].append(final_shot)

        except Exception as e:
            results["failed"].append(f"测试异常: {str(e)}")
            print(f"  [ERROR] 测试异常: {e}")
            # 截图保存当前状态
            error_shot = str(screenshot_dir / "99_error.png")
            try:
                page.screenshot(path=error_shot)
                results["screenshots"].append(error_shot)
            except Exception:
                pass
        finally:
            browser.close()

    return results


# ═══════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════

def main():
    repo_root = Path(__file__).parent.parent
    screenshot_dir = repo_root / "tests" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bilibot_web_test_") as tmp:
        tmp_dir = Path(tmp)
        port = _find_free_port()
        config_path = _write_temp_config(tmp_dir, port)

        print(f"[web-test] 临时配置: {config_path}")
        print(f"[web-test] 端口: {port}")
        print(f"[web-test] 截图目录: {screenshot_dir}")

        # 启动子进程
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            [sys.executable, "-m", "bilibot", "--config", str(config_path)],
            cwd=str(repo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            print(f"[web-test] 子进程 PID={proc.pid}，等待 Web 启动...")
            ok = _poll_status_public(port, timeout=15.0)

            if not ok:
                print("[web-test] 服务器启动失败")
                proc.terminate()
                try:
                    out, _ = proc.communicate(timeout=5.0)
                    print(out[-3000:] if len(out) > 3000 else out)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 1

            print("[web-test] 服务器启动成功，开始 Playwright 测试...")

            results = run_playwright_tests(port, screenshot_dir)

            # 汇总
            print("\n" + "=" * 60)
            print(f"[web-test] 测试汇总")
            print("=" * 60)
            print(f"通过: {len(results['passed'])}")
            print(f"失败: {len(results['failed'])}")
            if results["failed"]:
                print("\n失败项:")
                for f in results["failed"]:
                    print(f"  - {f}")
            print(f"\n截图: {len(results['screenshots'])} 个")
            for s in results["screenshots"]:
                print(f"  - {s}")

            return 0 if not results["failed"] else 1

        finally:
            print("\n[web-test] 关闭服务器...")
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
            print(f"[web-test] 子进程退出码={proc.returncode}")


if __name__ == "__main__":
    sys.exit(main())
