"""用真浏览器抓 PDF —— 专治「脚本拿不到、人肉能下」的文献。

为什么需要它：Wiley / Taylor & Francis / Oxford Academic / Project Euclid 这些站点
会对非浏览器的请求返回 403，或在 JS 里做挑战。requests 永远过不去，不是因为学校没订，
而是因为 TLS 指纹、cookie 时序、JS 环境都不对。既然插件本来就会为你拉起一个 Chrome
（browser_login 那个），那就干脆用它的登录态去把文件取回来。

工作方式（不碰密码、不偷 cookie 到别处）：
  1. 在同一个专用 Chrome 里打开文献落地页
  2. 等页面加载完，读 <meta name="citation_pdf_url">（各出版社通用的标准标签），
     退而求其次找页面里的 .pdf 链接
  3. 在**页面自己的上下文**里 fetch 那个地址（credentials: include），把字节取成 base64
  4. 解码写盘
整个过程等价于「你在那个页面上按了一下下载」，区别只是没让你手动点。
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

DEBUG_PORT = 9223
START_URL = "https://etd.xjtlu.edu.cn/index.html"
MAX_BYTES = 60 * 1024 * 1024

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
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
    return None


def _http_json(path: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _port_alive() -> bool:
    return _http_json("/json/version") is not None


def _new_tab(url: str) -> dict | None:
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{DEBUG_PORT}/json/new?{urllib.parse.quote(url, safe=':/?&=%#')}",
                method=method,
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
    return None


def _ensure_browser(profile_dir: Path) -> dict:
    if _port_alive():
        return {"launched": False, "reused": True}
    chrome = _chrome_path()
    if not chrome:
        return {"launched": False, "error": "没找到 Chrome / Chromium / Edge"}
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        START_URL,
    ]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if hasattr(subprocess, "DETACHED_PROCESS"):
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS
    subprocess.Popen(args, **kwargs)
    for _ in range(60):
        if _port_alive():
            return {"launched": True, "chrome": chrome}
        time.sleep(0.5)
    return {"launched": False, "error": "Chrome 起来了但调试端口没响应"}


class _CDP:
    def __init__(self, ws_url: str, timeout: float = 60.0):
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


_FIND_PDF_JS = r"""
(() => {
  const meta = document.querySelector('meta[name="citation_pdf_url"]');
  if (meta && meta.content) return {via: 'meta', url: new URL(meta.content, location.href).href};
  const cands = [...document.querySelectorAll('a[href]')]
    .map(a => a.href)
    .filter(h => /\.pdf(\?|#|$)/i.test(h) || /\/pdf\//i.test(h) || /\/epdf\//i.test(h));
  if (cands.length) return {via: 'link', url: cands[0]};
  return {via: null, url: null};
})()
"""

_FETCH_B64_JS = r"""
(async () => {
  const r = await fetch(%s, {credentials: 'include', headers: {'Accept': 'application/pdf,*/*'}});
  const ct = r.headers.get('content-type') || '';
  const buf = await r.arrayBuffer();
  const bytes = new Uint8Array(buf);
  const CH = 0x8000;
  let bin = '';
  for (let i = 0; i < bytes.length; i += CH) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  }
  return JSON.stringify({status: r.status, ct: ct, url: r.url, len: bytes.length, b64: btoa(bin)});
})()
"""


def _filename_from(url: str, fallback: str) -> str:
    from urllib.parse import unquote, urlparse

    name = unquote(Path(urlparse(url).path).name) or ""
    name = name.strip().replace("/", "_").replace("\\", "_")
    if not name or not name.lower().endswith(".pdf"):
        name = (name or fallback or "download").rstrip(".") + ".pdf"
    return name


def fetch_pdf(target: str, dest_dir: Path, filename: str = "",
              timeout_seconds: int = 45) -> dict:
    """打开 target 落地页 → 找 PDF 地址 → 在页面上下文里取回字节 → 写盘。

    target 可以是文章页、doi.org 链接、elink 代理链接，或 PDF 直链。
    """
    if not target:
        return {"ok": False, "reason": "bad_args", "detail": "target 为空"}

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    profile_dir = dest_dir.parent / "state" / "chrome-profile"
    boot = _ensure_browser(profile_dir)
    if boot.get("error"):
        return {"ok": False, "reason": "no_browser", "detail": boot["error"],
                "hint": "装一个 Chrome，或改用 download_pdf（纯 HTTP 路径）"}

    tab = _new_tab(target)
    if not tab:
        return {"ok": False, "reason": "no_tab", "detail": "开不出标签页"}
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return {"ok": False, "reason": "no_tab", "detail": "标签页没有调试地址"}

    cdp = _CDP(ws_url, timeout=max(15, timeout_seconds))
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")

        deadline = time.time() + timeout_seconds
        found = {"via": None, "url": None}
        while time.time() < deadline:
            res = cdp.call("Runtime.evaluate", expression=_FIND_PDF_JS,
                           returnByValue=True, awaitPromise=True)
            value = (res.get("result", {}).get("result", {}) or {}).get("value") or {}
            if isinstance(value, dict) and value.get("url"):
                found = value
                break
            time.sleep(1.5)

        pdf_url = found.get("url") or target
        expr = _FETCH_B64_JS % json.dumps(pdf_url)
        res = cdp.call("Runtime.evaluate", expression=expr, returnByValue=True,
                       awaitPromise=True)
        payload = res.get("result", {})
        if payload.get("exceptionDetails"):
            return {"ok": False, "reason": "fetch_failed",
                    "detail": str(payload["exceptionDetails"])[:300],
                    "landing": target, "pdf_url": pdf_url}
        raw = (payload.get("result", {}) or {}).get("value")
        if not raw:
            return {"ok": False, "reason": "fetch_failed", "detail": "页面里没有拿到数据",
                    "landing": target, "pdf_url": pdf_url}

        info = json.loads(raw)
        data = base64.b64decode(info.get("b64") or "")
        ctype = (info.get("ct") or "").lower()
        if not data:
            return {"ok": False, "reason": "empty_body", "pdf_url": pdf_url,
                    "content_type": ctype}
        if "pdf" not in ctype and not data[:5] == b"%PDF-":
            return {"ok": False, "reason": "not_pdf", "pdf_url": pdf_url,
                    "content_type": ctype, "bytes": len(data),
                    "detail": "拿到的不是 PDF，多半是登录页或验证页",
                    "hint": "先在同一个浏览器窗口里确认能正常打开这篇全文"}
        if len(data) > MAX_BYTES:
            return {"ok": False, "reason": "too_large", "bytes": len(data),
                    "detail": f"超过 {MAX_BYTES // 1048576}MB，浏览器抓取不划算，用官方下载"}

        name = filename or _filename_from(info.get("url") or pdf_url, "download")
        dest = dest_dir / name
        dest.write_bytes(data)
        return {
            "ok": True,
            "path": str(dest),
            "bytes": len(data),
            "via": "browser",
            "landing": target,
            "pdf_url": pdf_url,
            "found_by": found.get("via"),
            "hint": "用的是浏览器登录态（等同你自己点下载）；PDF 在本地，没上传任何地方。",
        }
    finally:
        cdp.call("Page.close")
        cdp.close()
