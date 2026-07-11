"""
scripts/interactive_test.py - 深度交互测试：检查 bug 和可用性

测试内容：
1. 登录流程
2. 页面导航与渲染
3. 表单交互（账号添加 Modal、配置保存）
4. 搜索功能
5. 图谱页面交互
6. 控制台错误监控
7. 响应式布局
8. 键盘导航
"""
import os, socket, subprocess, sys, tempfile, time, urllib.request, traceback
from pathlib import Path


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def _write_config(tmp_dir, port):
    data_dir_str = str(tmp_dir / "data").replace("\\", "/")
    c = f"""bilibili:
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
  secret_key: "interactive-secret"
  admin_username: "admin"
  admin_password: "test123"
data_dir: "{data_dir_str}"
reply:
  auto_reply: false
proactive:
  video_count: 0
  dynamic_count: 0
"""
    p = tmp_dir / "config.yaml"; p.write_text(c, encoding="utf-8"); return p


def main():
    repo = Path(__file__).parent.parent
    shot_dir = repo / "tests" / "screenshots" / "interactive"
    shot_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bilibot_int_") as tmp:
        tmp_dir = Path(tmp); port = _find_free_port()
        cfg = _write_config(tmp_dir, port); base = f"http://127.0.0.1:{port}"
        env = os.environ.copy(); env["PYTHONUNBUFFERED"]="1"; env["PYTHONIOENCODING"]="utf-8"
        proc = subprocess.Popen([sys.executable,"-m","bilibot","--config",str(cfg)],
            cwd=str(repo),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding="utf-8",errors="replace")
        try:
            dl = time.time()+20; ready=False
            while time.time()<dl:
                try:
                    with urllib.request.urlopen(f"{base}/api/status/public",timeout=2) as r:
                        if r.status==200: ready=True; break
                except: time.sleep(0.5)
            if not ready:
                print("[FAIL] 服务器未启动")
                # Print server logs
                out = proc.stdout.read(2000) if proc.stdout else ""
                print(f"服务器日志:\n{out}")
                return
            print(f"[OK] 服务器启动: {base}")

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                ctx = b.new_context(viewport={"width":1440,"height":900})
                pg = ctx.new_page()

                # Collect errors
                errors = []
                console_errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: console_errors.append(f"[{m.type}] {m.text}") if m.type in ("error","warning") else None)

                results = {"pass": 0, "fail": 0, "bugs": []}
                def check(name, cond, detail=""):
                    if cond:
                        results["pass"] += 1
                        print(f"  [PASS] {name}")
                    else:
                        results["fail"] += 1
                        results["bugs"].append(f"{name}: {detail}")
                        print(f"  [FAIL] {name} - {detail}")

                # ═══════ 1. 登录 ═══════
                print("\n═══════ 1. 登录流程 ═══════")
                pg.goto(f"{base}/login", wait_until="networkidle")
                pg.wait_for_timeout(500)
                check("登录页加载", "登录" in pg.title() or "BiliBot" in pg.title(), pg.title())
                check("Fraunces 字体加载", pg.evaluate("document.fonts.check('16px Fraunces')") or
                      "fraunces" in pg.evaluate("getComputedStyle(document.body).fontFamily.toLowerCase()"))
                # 错误密码测试
                pg.fill('input[name="username"], input[placeholder*="用户名"]', "admin")
                pg.fill('input[type="password"]', "wrongpass")
                pg.click('button:has-text("登录"), button[type="submit"]')
                pg.wait_for_timeout(1500)
                # 检查错误提示
                error_alert = pg.locator('[role="alert"], .error-message, .alert-error').count()
                check("错误密码有提示", error_alert > 0 or "密码" in pg.locator("body").inner_text()[:500],
                      f"alert count: {error_alert}")
                # 正确登录
                pg.fill('input[type="password"]', "test123")
                pg.click('button:has-text("登录"), button[type="submit"]')
                pg.wait_for_timeout(3000)
                check("登录成功跳转", "/login" not in pg.url, pg.url)
                pg.screenshot(path=str(shot_dir / "01_after_login.png"))

                # ═══════ 2. 布局与导航 ═══════
                print("\n═══════ 2. 布局与导航 ═══════")
                check("app-shell 渲染", pg.locator(".app-shell").count() > 0)
                check("sidebar 渲染", pg.locator(".sidebar").count() > 0)
                check("main-area 渲染", pg.locator(".main-area").count() > 0)
                check("topbar 渲染", pg.locator(".topbar").count() > 0)
                check("view-frame 渲染", pg.locator(".view-frame").count() > 0)
                check("nav-group 数量≥3", pg.locator(".nav-group").count() >= 3)
                check("nav-item 数量≥10", pg.locator(".nav-item").count() >= 10, f"数量: {pg.locator('.nav-item').count()}")
                check("品牌区存在", pg.locator(".brand-lockup").count() > 0)
                check("sidebar-footer 存在", pg.locator(".sidebar-footer").count() > 0)
                check("page-title 存在", pg.locator(".page-title").count() > 0)

                # 点击导航
                nav_items = pg.locator(".nav-item").all()
                first_nav = nav_items[0] if nav_items else None
                if first_nav:
                    href = first_nav.get_attribute("href")
                    first_nav.click()
                    pg.wait_for_timeout(1000)
                    current_hash = pg.evaluate("window.location.hash")
                    check("点击 nav-item 跳转", href in current_hash or current_hash.endswith(href.replace("#","")) ,
                          f"期望: {href}, 实际: {current_hash}")

                # ═══════ 3. 总览页 ═══════
                print("\n═══════ 3. 总览页 ═══════")
                pg.goto(f"{base}/#/"); pg.wait_for_timeout(1500)
                check("总览页 eyebrow 存在", pg.locator(".eyebrow").count() > 0)
                check("总览页 Card/article 存在", pg.locator("article, .card").count() > 0)
                check("总览页 btn 存在", pg.locator(".btn").count() > 0)
                # KPI 数字 — 在无数据测试环境中可能为 0，因此只检查页面有内容
                check("总览页有内容", len(pg.locator(".view-frame").first.inner_html()) > 50)
                pg.screenshot(path=str(shot_dir / "02_overview.png"))

                # ═══════ 4. 账号管理 ═══════
                print("\n═══════ 4. 账号管理 ═══════")
                pg.goto(f"{base}/#/accounts"); pg.wait_for_timeout(1500)
                check("账号页加载", pg.locator(".view-frame").count() > 0)
                # 点击"添加账号"按钮
                add_btn = pg.locator('button:has-text("添加账号")').first
                if add_btn.count() > 0:
                    add_btn.click()
                    pg.wait_for_timeout(800)
                    check("Modal 弹出", pg.locator('[role="dialog"]').count() > 0)
                    check("Modal aria-modal", pg.locator('[aria-modal="true"]').count() > 0)
                    # 检查 Modal 内表单
                    modal_inputs = pg.locator('[role="dialog"] input, [role="dialog"] select').count()
                    check("Modal 含表单字段", modal_inputs > 0, f"字段数: {modal_inputs}")
                    pg.screenshot(path=str(shot_dir / "03_accounts_modal.png"))
                    # Escape 关闭
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(500)
                    check("Escape 关闭 Modal", pg.locator('[role="dialog"]').count() == 0)
                else:
                    check("添加账号按钮存在", False, "未找到按钮")
                pg.screenshot(path=str(shot_dir / "04_accounts.png"))

                # ═══════ 5. 人格管理 ═══════
                print("\n═══════ 5. 人格管理 ═══════")
                pg.goto(f"{base}/#/personas"); pg.wait_for_timeout(1500)
                check("人格页加载", pg.locator(".view-frame").count() > 0)
                check("人格页 Card 存在", pg.locator("article, .card").count() > 0)
                # 尝试点击创建人格
                create_btn = pg.locator('button:has-text("创建"), button:has-text("新增"), button:has-text("添加")').first
                if create_btn.count() > 0:
                    create_btn.click()
                    pg.wait_for_timeout(800)
                    check("人格 Modal 弹出", pg.locator('[role="dialog"]').count() > 0)
                    # 检查 textarea（之前有 bug）
                    textarea_count = pg.locator('[role="dialog"] textarea').count()
                    check("人格 Modal 含 textarea", textarea_count > 0, f"textarea 数: {textarea_count}")
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(500)
                pg.screenshot(path=str(shot_dir / "05_personas.png"))

                # ═══════ 6. 记忆图谱 2D ═══════
                print("\n═══════ 6. 记忆图谱 2D ═══════")
                pg.goto(f"{base}/#/memory/graph"); pg.wait_for_timeout(2000)
                check("2D 图谱页加载", pg.locator(".view-frame").count() > 0)
                # 检查是否有 EmptyState（无账号情况）或 SVG 画布
                empty_state = pg.locator(".empty-state").count()
                svg_canvas = pg.locator("svg").count()
                check("2D 图谱有空状态或SVG", empty_state > 0 or svg_canvas > 0,
                      f"empty: {empty_state}, svg: {svg_canvas}")
                pg.screenshot(path=str(shot_dir / "06_memory_graph_2d.png"))

                # ═══════ 7. 记忆图谱 3D ═══════
                print("\n═══════ 7. 记忆图谱 3D ═══════")
                pg.goto(f"{base}/#/memory/graph-3d"); pg.wait_for_timeout(3000)
                check("3D 图谱页加载", pg.locator(".view-frame").count() > 0)
                # Three.js 可能未加载（无账号）或已加载
                webgl_canvas = pg.locator("canvas").count()
                check("3D 图谱有 canvas 或空状态", webgl_canvas > 0 or pg.locator(".empty-state").count() > 0,
                      f"canvas: {webgl_canvas}")
                pg.screenshot(path=str(shot_dir / "07_memory_graph_3d.png"))

                # ═══════ 8. 全局配置 ═══════
                print("\n═══════ 8. 全局配置 ═══════")
                pg.goto(f"{base}/#/config"); pg.wait_for_timeout(1500)
                check("配置页加载", pg.locator(".view-frame").count() > 0)
                check("配置页 field-wrap 存在", pg.locator(".field-wrap").count() > 0)
                check("配置页 btn 存在", pg.locator(".btn").count() > 0)
                # 尝试切换某个开关或输入
                fields = pg.locator(".field-wrap input, .field-wrap select").count()
                check("配置页表单字段存在", fields > 0, f"字段数: {fields}")
                pg.screenshot(path=str(shot_dir / "08_config.png"))

                # ═══════ 9. Topbar 搜索 ═══════
                print("\n═══════ 9. Topbar 交互 ═══════")
                pg.goto(f"{base}/#/"); pg.wait_for_timeout(1000)
                search_input = pg.locator('.topbar input[type="text"]').first
                if search_input.count() > 0:
                    search_input.fill("测试搜索")
                    pg.wait_for_timeout(500)
                    check("Topbar 搜索可输入", search_input.input_value() == "测试搜索")
                else:
                    check("Topbar 搜索框存在", False, "未找到搜索框")
                # 刷新状态按钮
                refresh_btn = pg.locator('button:has-text("刷新状态")').first
                check("刷新状态按钮存在", refresh_btn.count() > 0)
                # 查看总览按钮
                overview_btn = pg.locator('button:has-text("查看总览")').first
                check("查看总览按钮存在", overview_btn.count() > 0)

                # ═══════ 10. 所有路由渲染 ═══════
                print("\n═══════ 10. 所有路由渲染 ═══════")
                routes = [
                    "/", "/accounts", "/personas", "/llm",
                    "/memory/list", "/memory/graph", "/memory/graph-3d", "/memory/recall",
                    "/comments", "/logs", "/proactive", "/drafts",
                    "/image-gen", "/video-analysis", "/config", "/system",
                ]
                for r in routes:
                    pg.goto(f"{base}/#{r}")
                    pg.wait_for_load_state("networkidle", timeout=5000)
                    pg.wait_for_timeout(800)
                    vf = pg.locator(".view-frame").first
                    html_len = len(vf.inner_html()) if vf.count() > 0 else 0
                    check(f"路由 {r} 渲染", html_len > 20, f"内容长度: {html_len}")

                # ═══════ 11. 响应式布局 ═══════
                print("\n═══════ 11. 响应式布局 ═══════")
                pg.set_viewport_size({"width": 900, "height": 700})
                pg.goto(f"{base}/#/"); pg.wait_for_timeout(1000)
                pg.screenshot(path=str(shot_dir / "09_responsive_900.png"))
                # 检查是否切换为单栏（grid-template-columns 只有一个轨道）
                shell_display = pg.evaluate("getComputedStyle(document.querySelector('.app-shell')).gridTemplateColumns")
                # 单栏时 grid-template-columns 为 "900px" 或 "1fr"（只有一个轨道，无空格分隔）
                is_single_col = " " not in shell_display.strip()
                check("响应式单栏布局", is_single_col,
                      f"grid-template-columns: {shell_display}")
                # 恢复
                pg.set_viewport_size({"width": 1440, "height": 900})

                # ═══════ 12. 键盘导航 ═══════
                print("\n═══════ 12. 键盘导航 ═══════")
                pg.goto(f"{base}/#/"); pg.wait_for_timeout(500)
                pg.keyboard.press("Tab")
                pg.wait_for_timeout(300)
                focused = pg.evaluate("document.activeElement.tagName")
                check("Tab 焦点在交互元素", focused in ('BUTTON','A','INPUT','SELECT','TEXTAREA'),
                      f"焦点: {focused}")
                # skip-link
                skip = pg.locator(".skip-link").first
                check("skip-link 存在", skip.count() > 0)

                # ═══════ 13. 控制台错误汇总 ═══════
                print("\n═══════ 13. 控制台错误汇总 ═══════")
                # 过滤 favicon 404 和错误密码登录的 401（正常行为）
                real_errors = [e for e in errors]
                real_console = [c for c in console_errors
                                if "favicon" not in c.lower()
                                and "404" not in c
                                and "401" not in c]
                check("无页面 JS 错误", len(real_errors) == 0, f"错误: {real_errors[:3]}")
                check("无关键控制台错误", len(real_console) == 0, f"错误: {real_console[:3]}")
                if real_errors:
                    print(f"  [详情] 页面错误:")
                    for e in real_errors[:5]:
                        print(f"    - {e}")
                if real_console:
                    print(f"  [详情] 控制台错误:")
                    for c in real_console[:5]:
                        print(f"    - {c}")

                # ═══════ 14. 无障碍检查 ═══════
                print("\n═══════ 14. 无障碍检查 ═══════")
                pg.goto(f"{base}/#/accounts"); pg.wait_for_timeout(500)
                check("nav[aria-label]≥2", pg.locator('nav[aria-label]').count() >= 2)
                check("button[aria-label]≥1", pg.locator('button[aria-label]').count() >= 1)
                check("aria-live 区域", pg.locator('[aria-live="polite"]').count() > 0)
                check("main 语义标签", pg.locator("main").count() > 0)
                # 激活状态
                active = pg.locator('.nav-item[aria-current="page"]').count()
                check("激活 nav-item 有 aria-current", active >= 1, f"数量: {active}")

                pg.screenshot(path=str(shot_dir / "10_final.png"))

                b.close()

                # ═══════ 汇总 ═══════
                print("\n" + "="*60)
                print(f"测试汇总: {results['pass']} 通过, {results['fail']} 失败")
                print("="*60)
                if results["bugs"]:
                    print("\n发现的 Bug:")
                    for i, bug in enumerate(results["bugs"], 1):
                        print(f"  {i}. {bug}")
                print(f"\n截图保存至: {shot_dir}")

        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except: proc.kill()


if __name__ == "__main__":
    main()
