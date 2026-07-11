"""逐个检查 ES Module 语法错误"""
import os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _write_config(tmp_dir, port):
    data_dir = str(tmp_dir / "data").replace("\\", "/")
    cfg = f"""bilibili:
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
  secret_key: "diag-secret"
  admin_username: "admin"
  admin_password: "test123"
data_dir: "{data_dir}"
reply:
  auto_reply: false
proactive:
  video_count: 0
  dynamic_count: 0
"""
    p = tmp_dir / "config.yaml"
    p.write_text(cfg, encoding="utf-8")
    return p

def main():
    repo = Path(__file__).parent.parent
    js_dir = repo / "bilibot" / "web" / "static" / "js"

    # 收集所有 JS 文件（相对路径）
    js_files = []
    for f in js_dir.rglob("*.js"):
        rel = f.relative_to(js_dir)
        js_files.append(str(rel).replace("\\", "/"))

    print(f"找到 {len(js_files)} 个 JS 文件")

    with tempfile.TemporaryDirectory(prefix="diag_") as tmp:
        tmp_dir = Path(tmp)
        port = _find_free_port()
        cfg = _write_config(tmp_dir, port)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "-m", "bilibot", "--config", str(cfg)],
            cwd=str(repo), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status/public", timeout=2) as r:
                        if r.status == 200: break
                except: time.sleep(0.5)
            else:
                print("服务器启动失败")
                return 1

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/login", wait_until="networkidle")

                # 逐个动态 import 每个 JS 文件
                print("\n逐个检查 ES Module 语法:")
                failed = []
                for js_file in js_files:
                    url = f"http://127.0.0.1:{port}/static/js/{js_file}"
                    try:
                        result = page.evaluate(f"""
                            async () => {{
                                try {{
                                    await import('{url}');
                                    return {{ ok: true }};
                                }} catch(e) {{
                                    return {{ ok: false, error: e.message, stack: e.stack || '' }};
                                }}
                            }}
                        """)
                        if result.get("ok"):
                            print(f"  [OK] {js_file}")
                        else:
                            err = result.get("error", "")
                            # 忽略导入依赖错误（如 window.Vue 未定义），只关注语法错误
                            if "Unexpected token" in err or "SyntaxError" in err:
                                print(f"  [SYNTAX ERROR] {js_file}")
                                print(f"    错误: {err}")
                                failed.append(js_file)
                            else:
                                print(f"  [RUNTIME ERROR] {js_file} (非语法错误，可忽略)")
                                print(f"    错误: {err[:100]}")
                    except Exception as e:
                        print(f"  [EVAL ERROR] {js_file}: {e}")

                print(f"\n汇总: {len(failed)} 个文件有语法错误")
                for f in failed:
                    print(f"  - {f}")

                browser.close()
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except: proc.kill()
    return 0

if __name__ == "__main__":
    sys.exit(main())
