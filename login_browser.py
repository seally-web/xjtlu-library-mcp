"""用「真浏览器」替你取 token —— 不需要任何手动复制粘贴。

思路：开一个专用的 Chrome 窗口（独立 profile，不碰你日常浏览用的那个），
把 etd.xjtlu.edu.cn 打开，你自己在里面登录一次；插件通过 Chrome DevTools
Protocol（本机 9223 端口）读 localStorage 里的 token/uId 与 Cookie 头，
写进 state/etd.json 之后就把窗口关掉。

为什么不用 Playwright：那要多下 100 多 MB 浏览器；而 CDP 是 Chrome 自带的能力，
只用到一个几 KB 的 websocket 客户端。为什么不抓密码：我们不碰登录页，
登录全过程在真实浏览器里由你完成，插件只读登录完成后的结果。
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

import etd

DEBUG_PORT = 9223
START_URL = "https://etd.xjtlu.edu.cn/index.html"
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def _chrome_path() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chrome", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _http_json(path: str, timeout: float = 2.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _port_alive() -> bool:
    try:
        _http_json("/json/version", timeout=1.0)
        return True
    except Exception:
        return False


def _port_free() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", DEBUG_PORT)) != 0


def _ensure_browser(profile_dir: Path) -> dict:
    """已经开着就复用，否则拉一个新的窗口。"""
    if _port_alive():
        return {"launched": False, "reason": "复用已开着的调试端口"}

    chrome = _chrome_path()
    if not chrome:
        return {"launched": False, "error": "没找到 Chrome / Chromium / Edge",
                "hint": "装一个 Chrome，或用 save_exam_session 手动粘 cookie+token"}

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1180,900",
        START_URL,
    ]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "stdin": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)

    for _ in range(60):  # 最多等 30s
        if _port_alive():
            return {"launched": True, "chrome": chrome}
        time.sleep(0.5)
    return {"launched": False, "error": "Chrome 起来了但调试端口没响应",
            "hint": f"手动执行：{chrome} --remote-debugging-port={DEBUG_PORT} --user-data-dir={profile_dir}"}


def _find_page() -> dict | None:
    for target in _http_json("/json/list"):
        if target.get("type") == "page" and "xjtlu.edu.cn" in target.get("url", ""):
            return target
    return None


def _open_tab() -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{DEBUG_PORT}/json/new?{START_URL}", method="PUT"
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # 新版 Chrome 允许 GET 之外的写法差异，找不到就走已有标签页


class _CDP:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _read_state(cdp: _CDP) -> dict:
    expr = ("JSON.stringify({t:localStorage.getItem('token'),"
            "u:localStorage.getItem('uId')})")
    res = cdp.call("Runtime.evaluate", expression=expr, returnByValue=True)
    raw = (res.get("result", {}).get("result", {}) or {}).get("value") or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return {"token": data.get("t") or "", "user_id": data.get("u") or ""}


def _read_cookies(cdp: _CDP) -> str:
    cdp.call("Network.enable")
    res = cdp.call("Network.getAllCookies")
    cookies = res.get("result", {}).get("cookies", []) or []
    keep = [c for c in cookies if c.get("domain", "").endswith("xjtlu.edu.cn")]
    return "; ".join(f"{c['name']}={c['value']}" for c in keep)


def login(timeout_seconds: int = 240, close_when_done: bool = True) -> dict:
    """开窗口 → 等你登录 → 读 token/cookie → 存盘 → 关窗口。

    返回 {"ok": True, "saved": {...}} 或 {"ok": False, "reason": "timeout"|...}。
    """
    profile = etd.STATE_DIR / "chrome-profile"
    boot = _ensure_browser(profile)
    if boot.get("error"):
        return {"ok": False, "reason": "no_browser", **boot}

    page = _find_page()
    if not page:
        _open_tab()
        for _ in range(20):
            page = _find_page()
            if page:
                break
            time.sleep(0.5)
    if not page:
        return {"ok": False, "reason": "no_tab",
                "hint": f"窗口开了但没找到页面，请在窗口里打开 {START_URL}"}

    cdp = _CDP(page["webSocketDebuggerUrl"])
    deadline = time.time() + max(20, timeout_seconds)
    try:
        while time.time() < deadline:
            state = _read_state(cdp)
            if state["token"]:
                cookie = _read_cookies(cdp)
                if "XJTLU_UserInfo" not in cookie:
                    # token 有了但 cookie 还没到，再等一轮
                    time.sleep(2)
                    cookie = _read_cookies(cdp)
                result = etd.save_session(cookie, state["token"], state["user_id"])
                if close_when_done:
                    try:
                        cdp.call("Page.close")
                    except Exception:
                        pass
                return {"ok": bool(result.get("ok")), "launched": boot.get("launched"),
                        "saved": result, "user_id": state["user_id"]}
            time.sleep(2)
    finally:
        cdp.close()

    return {"ok": False, "reason": "timeout",
            "hint": f"等了 {timeout_seconds}s 还没登录成功。窗口还开着——在里面完成登录后，再调一次本工具（会复用这个窗口）。"}


def status() -> dict:
    """看一眼浏览器/会话的现状，不弹窗。"""
    info = {"debug_port": DEBUG_PORT, "port_alive": _port_alive(),
            "chrome": _chrome_path(), "session": etd.session_status()}
    page = _find_page() if info["port_alive"] else None
    info["page"] = page.get("url") if page else None
    return info
