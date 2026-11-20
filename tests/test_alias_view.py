"""别名管理视图的端到端测试（真实 Chromium，离线运行）。

复现 Dashboard 的插件视图环境：视图被嵌在
``sandbox="allow-scripts allow-forms allow-downloads"`` 的 iframe 里
（不含 allow-modals，window.confirm/prompt 被浏览器静默禁用），
bridge 用桩替代。验证删除 / 清空 / 导入的页内确认对话框全流程。

运行（需要 playwright + 已缓存的 Chromium）：
    python3 tests/test_alias_view.py
或：
    python -m pytest tests/test_alias_view.py -v   # 无 playwright 时自动跳过
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
VIEW_PATH = "/views/aliases/index.html"

WRAPPER_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0">
<iframe sandbox="allow-scripts allow-forms allow-downloads"
        src="%s" style="width:100%%;height:900px;border:0"></iframe>
</body></html>""" % VIEW_PATH

# 注入到所有 frame 的 bridge 桩：带状态，删除/清空真的会改列表
BRIDGE_STUB = r"""
(() => {
  const state = {
    items: [
      { alias: "老头环", title: "Elden Ring", source: "user", hits: 3, updated_at: 1730000000 },
      { alias: "黑猴", title: "Black Myth: Wukong", source: "llm", hits: 1, updated_at: 1730000001 },
    ],
  };
  const stats = () => ({
    total: state.items.length,
    user: state.items.filter((i) => i.source === "user").length,
    llm: state.items.filter((i) => i.source === "llm").length,
    builtin: state.items.filter((i) => i.source === "builtin").length,
  });
  window.__bridgeCalls = [];
  window.AstrBotPluginView = {
    ready: async () => ({ locale: "zh-CN", themeColors: {} }),
    onContext: () => () => {},
    t: (key, fallback) => fallback,
    apiGet: async (endpoint, params) => {
      window.__bridgeCalls.push({ kind: "get", endpoint, params });
      if (endpoint === "aliases/list") {
        return { items: state.items.slice(), stats: stats(), path: "/tmp/aliases.json" };
      }
      return {};
    },
    apiPost: async (endpoint, body) => {
      window.__bridgeCalls.push({ kind: "post", endpoint, body });
      if (endpoint === "aliases/delete") {
        const before = state.items.length;
        state.items = state.items.filter((i) => i.alias !== body.alias);
        return { deleted: before - state.items.length, stats: stats() };
      }
      if (endpoint === "aliases/clear") {
        const src = body && body.source;
        const n = src ? state.items.filter((i) => i.source === src).length : state.items.length;
        state.items = src ? state.items.filter((i) => i.source !== src) : [];
        return { cleared: n, stats: stats() };
      }
      if (endpoint === "aliases/import_builtin") return { added: 0, stats: stats() };
      return {};
    },
  };
  // 双保险：即便不在沙箱里，也模拟原生对话框被禁用的行为
  window.confirm = () => false;
  window.prompt = () => null;
  window.alert = () => {};
})();
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    """静态服务插件目录；给沙箱 iframe（opaque origin）补 CORS 头。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PLUGIN_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        if self.path == "/__wrapper__":
            body = WRAPPER_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def _posts(frame, endpoint):
    return frame.evaluate(
        "(ep) => window.__bridgeCalls.filter(c => c.kind === 'post' && c.endpoint === ep)",
        endpoint,
    )


def _launch_chromium(p):
    """优先用 playwright 默认浏览器；版本与缓存不匹配时回退到最新缓存。"""
    args = ["--no-proxy-server"]
    try:
        return p.chromium.launch(args=args)
    except Exception:
        caches = [
            Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
            Path.home() / ".cache" / "ms-playwright",  # Linux
        ]
        candidates = sorted(
            c
            for cache in caches
            for c in cache.glob(
                "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        )
        if not candidates:
            raise
        return p.chromium.launch(executable_path=str(candidates[-1]), args=args)


def run_view_checks() -> None:
    from playwright.sync_api import sync_playwright

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    try:
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            page = browser.new_page()
            page.add_init_script(BRIDGE_STUB)
            page.goto(f"http://127.0.0.1:{port}/__wrapper__")
            frame = next(
                f for f in page.frames if f.url.endswith(VIEW_PATH)
            )

            # 初始列表渲染
            frame.wait_for_selector("#tbody tr")
            assert frame.locator("#tbody tr").count() == 2

            # ---- 删除：取消 ----
            frame.locator(".js-del").first.click()
            frame.wait_for_selector("#dialog:not([hidden])")
            assert frame.locator("#dialog-title").inner_text() == "删除别名"
            assert "老头环" in frame.locator("#dialog-message").inner_text()
            assert "danger" in frame.locator("#dialog-ok").get_attribute("class")
            frame.locator("#dialog-cancel").click()
            frame.wait_for_selector("#dialog[hidden]", state="attached")
            assert _posts(frame, "aliases/delete") == []
            assert frame.locator("#tbody tr").count() == 2

            # ---- 删除：确认 ----
            frame.locator(".js-del").first.click()
            frame.wait_for_selector("#dialog:not([hidden])")
            frame.locator("#dialog-ok").click()
            frame.wait_for_function(
                "() => window.__bridgeCalls.some(c => c.kind === 'post' && c.endpoint === 'aliases/delete')"
            )
            posts = _posts(frame, "aliases/delete")
            assert posts == [
                {"kind": "post", "endpoint": "aliases/delete", "body": {"alias": "老头环"}}
            ]
            frame.wait_for_function("() => document.querySelectorAll('#tbody tr').length === 1")
            assert "已删除" in frame.locator("#toast").inner_text()

            # ---- 清空：选择来源 llm ----
            frame.locator("#btn-clear").click()
            frame.wait_for_selector("#dialog:not([hidden])")
            assert frame.locator("#dialog-options button").count() == 4
            frame.locator('#dialog-options button[data-value="llm"]').click()
            frame.locator("#dialog-ok").click()
            frame.wait_for_function(
                "() => window.__bridgeCalls.some(c => c.kind === 'post' && c.endpoint === 'aliases/clear')"
            )
            assert _posts(frame, "aliases/clear") == [
                {"kind": "post", "endpoint": "aliases/clear", "body": {"source": "llm"}}
            ]
            frame.wait_for_function("() => document.querySelectorAll('#tbody tr').length === 0")

            # ---- 导入：Esc 取消 ----
            frame.locator("#btn-import").click()
            frame.wait_for_selector("#dialog:not([hidden])")
            page.keyboard.press("Escape")
            frame.wait_for_selector("#dialog[hidden]", state="attached")
            assert _posts(frame, "aliases/import_builtin") == []

            # ---- 导入：确认 ----
            frame.locator("#btn-import").click()
            frame.wait_for_selector("#dialog:not([hidden])")
            frame.locator("#dialog-ok").click()
            frame.wait_for_function(
                "() => window.__bridgeCalls.some(c => c.kind === 'post' && c.endpoint === 'aliases/import_builtin')"
            )
            assert _posts(frame, "aliases/import_builtin") == [
                {"kind": "post", "endpoint": "aliases/import_builtin", "body": {}}
            ]

            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_alias_view_dialogs():
    import pytest

    pytest.importorskip("playwright.sync_api")
    run_view_checks()


if __name__ == "__main__":
    run_view_checks()
    print("alias view E2E checks passed")