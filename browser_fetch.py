"""用真浏览器抓 PDF —— 专治「脚本拿不到、人肉能下」的文献。

为什么需要它：Wiley / Taylor & Francis / Oxford Academic / ScienceDirect / Project Euclid
会对非浏览器请求返回 403，或在 JS 里做挑战。requests 永远过不去，不是因为学校没订，
而是因为 TLS 指纹、cookie 时序、JS 环境都不对。既然插件本来就会为你拉起一个 Chrome，
那就用它的登录态把文件取回来。

三条经验（v0.2.0 起）：
  1. **导航比页内 fetch 强**。同一个 PDF 地址，用 fetch() 拿可能永远是 51KB 的挑战页，
     而让浏览器导航过去就能通过挑战落到真文件上。默认走导航 + CDP Fetch 拦截响应体。
  2. **先判断有没有权限，再花时间下载**。落地页 3 秒就能看出「有没有全文」，
     别等 60 秒超时才发现是没订阅。
  3. **失败要分类型**。bot_challenge / no_entitlement / not_found 是三件不同的事，
     含糊地报一句"失败了"等于没报。

不碰密码：登录仍然由你在浏览器里自己完成，这里只读取已登录会话的成果。
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

DEBUG_PORT = 9223
START_URL = "https://etd.xjtlu.edu.cn/index.html"
MAX_BYTES = 60 * 1024 * 1024

# 失败分类：调用方可以据此决定「换办法」还是「放弃」
REASONS = {
    "ok": "拿到 PDF",
    "bad_args": "参数不对",
    "no_browser": "没有可用的 Chrome/Chromium/Edge",
    "no_tab": "开不出标签页",
    "not_found": "落地页 404 / 记录不存在",
    "bot_challenge": "被反爬挑战挡住（需要真人环境，导航重试有时能过）",
    "no_entitlement": "学校没有这篇的全文权限（别再试了，走文献传递）",
    "fetch_failed": "取回过程报错",
    "empty_body": "响应是空的",
    "not_pdf": "拿到的不是 PDF（多半是登录页或挑战页）",
    "too_large": "文件超过 60MB",
    "timeout": "超时",
}

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

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


# ---------------------------------------------------------------- URL 规整


def normalize_target(target: str) -> str:
    """把用户给的东西变成浏览器能打开的地址。

    - 裸 DOI（`10.1007/s00039-016-0371-x`）→ `https://doi.org/...`
    - `....pdf.pdf` 这种被拼坏的 → 收敛成一个 `.pdf`
    - 签名直链（`X-Amz-Expires` 等查询串）**原样保留**，查询串是签名的一部分
    """
    t = (target or "").strip()
    if not t:
        return t
    if _DOI_RE.match(t):
        return "https://doi.org/" + t
    if re.match(r"^10\.\d{4,9}/", t) is None and "://" not in t and not t.startswith("//"):
        # 形如 doi.org/10.x/... 或 www.nature.com/articles/x
        if t.startswith("doi.org/"):
            return "https://" + t
    # 只对路径部分去重 `.pdf.pdf`，查询串（签名）保持原样
    try:
        p = urllib.parse.urlsplit(t)
    except ValueError:
        return t
    path = re.sub(r"(\.pdf)+$", ".pdf", p.path, flags=re.I)
    if p.query:
        # 签名链接：查询串一个字都不能动
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))


def looks_like_pdf_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or "/pdf/" in path or "/epdf/" in path \
        or "pdfdirect" in path or "sciencedirectassets" in urllib.parse.urlsplit(url).netloc


# ---------------------------------------------------------------- 浏览器控制


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
    """极简 CDP 客户端：能收事件、能等响应。"""

    def __init__(self, ws_url: str, timeout: float = 60.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=None)
        self._id = 0
        self._events: list[dict] = []

    def _send(self, method: str, params: dict) -> int:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        return self._id

    def _pump(self, deadline: float):
        """读一条消息。返回 (id|None, msg|None)；超时返回 (None, None)。"""
        remaining = deadline - time.time()
        if remaining <= 0:
            return None, None
        self.ws.settimeout(min(max(remaining, 0.2), 1.0))
        try:
            msg = json.loads(self.ws.recv())
        except websocket.WebSocketTimeoutException:
            return None, None
        except Exception:
            return None, None
        if "id" in msg:
            return msg["id"], msg
        self._events.append(msg)
        return None, msg

    def call(self, method: str, timeout: float = 20.0, **params):
        want = self._send(method, params)
        deadline = time.time() + timeout
        while True:
            got, msg = self._pump(deadline)
            if msg is None and time.time() >= deadline:
                raise TimeoutError(f"{method} 超时")
            if got == want:
                return msg

    def drain(self, seconds: float) -> list[dict]:
        """收若干秒内的事件（含已经攒下的）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._pump(deadline)
        out, self._events = self._events, []
        return out

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 页内脚本

_CLASSIFY_JS = r"""
(() => {
  const meta = document.querySelector('meta[name="citation_pdf_url"]');
  let pdfUrl = null;
  try { if (meta && meta.content) pdfUrl = new URL(meta.content, location.href).href; } catch (e) {}
  const txt = ((document.body && document.body.innerText) || '').slice(0, 30000);
  const html = document.documentElement.outerHTML.slice(0, 300000);
  const links = [...document.querySelectorAll('a[href]')].map(a => a.href);
  const pdfLink = links.find(h => /\.pdf(\?|#|$)/i.test(h) || /\/pdf\//i.test(h) || /\/epdf\//i.test(h)) || null;
  const challenge = /ca_js_challenge|cf-challenge|challenge-platform|Checking your browser|Just a moment|Please turn JavaScript on|Enable JavaScript and cookies/i.test(html);
  const noEnt = !pdfUrl && !pdfLink && /Get access|Purchase PDF|Buy article|Rent this article|Access options|Sign in to (read|access)|Access through your institution/i.test(txt);
  const notFound = /Page not found|404 Not Found|DOI Not Found|DOI not found|does not exist|no longer available|this (article|page) (is|was) not/i.test(txt + ' ' + html.slice(0, 20000)) && !pdfUrl;
  return {
    pdfUrl: pdfUrl || pdfLink,
    hasMeta: !!pdfUrl,
    hasLink: !!pdfLink,
    challenge: challenge,
    noEntitlement: !!noEnt,
    notFound: !!notFound,
    title: document.title,
    url: location.href,
    status: document.readyState,
    textHead: txt.slice(0, 300)
  };
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


# ---------------------------------------------------------------- 小工具


def _filename_from(url: str, fallback: str) -> str:
    from urllib.parse import unquote, urlparse

    name = unquote(Path(urlparse(url).path).name) or ""
    name = name.strip().replace("/", "_").replace("\\", "_")
    if not name or not name.lower().endswith(".pdf"):
        name = (name or fallback or "download").rstrip(".") + ".pdf"
    return name


def _classify_bytes(data: bytes, ctype: str, status: int | None) -> str | None:
    """返回 None 表示是正常 PDF；否则返回失败原因。"""
    if not data:
        return "empty_body"
    if len(data) > MAX_BYTES:
        return "too_large"
    if data[:5] == b"%PDF-":
        return None
    if "pdf" in (ctype or "").lower() and len(data) > 1024:
        return None
    head = data[:8192].decode("utf-8", "ignore")
    if re.search(r"ca_js_challenge|cf-challenge|challenge-platform|Checking your browser|"
                 r"Just a moment|Please turn JavaScript on|Enable JavaScript", head, re.I):
        return "bot_challenge"
    if status == 404:
        return "not_found"
    if status in (401, 403):
        return "no_entitlement"
    # 小体积的非 PDF，基本就是登录页/挑战页/拦截页
    return "not_pdf"


def _cookies_for(cdp: _CDP, origin_url: str) -> str:
    """取**这个 origin** 的 cookie。

    注意：`__cf_bm` 这类是 partitioned cookie，必须用 Network.getCookies 带上 urls 取，
    从别的标签页或者全局 getAllCookies 拿会是空的——而且不报错，最难查。
    """
    try:
        cdp.call("Network.enable", timeout=8)
        res = cdp.call("Network.getCookies", timeout=8, urls=[origin_url])
        cookies = res.get("result", {}).get("cookies", []) or []
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


# ---------------------------------------------------------------- 两条下载路径


def _download_via_fetch_js(cdp: _CDP, pdf_url: str, timeout: float) -> dict:
    """页内 fetch。适合 Springer / arXiv / OUP 这类不挑战的站。"""
    expr = _FETCH_B64_JS % json.dumps(pdf_url)
    res = cdp.call("Runtime.evaluate", timeout=timeout, expression=expr,
                   returnByValue=True, awaitPromise=True)
    payload = res.get("result", {})
    if payload.get("exceptionDetails"):
        return {"ok": False, "reason": "fetch_failed",
                "detail": str(payload["exceptionDetails"])[:300], "method": "fetch"}
    raw = (payload.get("result", {}) or {}).get("value")
    if not raw:
        return {"ok": False, "reason": "fetch_failed", "detail": "页面里没有拿到数据",
                "method": "fetch"}
    info = json.loads(raw)
    data = base64.b64decode(info.get("b64") or "")
    reason = _classify_bytes(data, info.get("ct") or "", info.get("status"))
    if reason:
        return {"ok": False, "reason": reason, "method": "fetch",
                "status": info.get("status"), "content_type": info.get("ct") or "",
                "bytes": len(data), "pdf_url": info.get("url") or pdf_url}
    return {"ok": True, "data": data, "method": "fetch",
            "content_type": info.get("ct") or "",
            "pdf_url": info.get("url") or pdf_url}


_PDF_IN_HTML = [
    re.compile(r'["\'](/doi/pdfdirect/[^"\'#?]+)'),
    re.compile(r'viewer\.html\?file=([^"\'&]+)'),
    re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)'),
    re.compile(r'["\'](https?://[^"\']+\.pdf(?:\?[^"\']*)?)["\']'),
]


def _pdf_from_html(html: str, base_url: str) -> str:
    """很多站先给你一个 45KB 的 HTML 外壳，真文件地址藏在里面。

    Wiley 就是典型：`/doi/pdf/x` 返回的是查看器页面，里面写着
    `var src = "/doi/pdfdirect/x"`；ScienceDirect 用的是 viewer.html?file=…
    """
    if not html:
        return ""
    for rx in _PDF_IN_HTML:
        m = rx.search(html)
        if m:
            return urllib.parse.urljoin(base_url, m.group(1))
    return ""


def _tab_state(cdp: _CDP) -> dict:
    """当前 tab 的 url 与 document.contentType。

    ScienceDirect 的 pdfft 会先回一个 52KB 的 JS 跳转壳，浏览器随后自己走到
    `pdf.sciencedirectassets.com` 并把 PDF 显示出来——这时再看外层 HTML 是没用的，
    只能看 `document.contentType`，确认是 PDF 后按当前 URL 重新取一次。
    """
    js = "JSON.stringify({url: location.href, ct: document.contentType || '', title: document.title || ''})"
    try:
        r = cdp.call("Runtime.evaluate", timeout=10, expression=js, returnByValue=True)
        val = (r.get("result", {}).get("result") or {}).get("value")
        return json.loads(val) if val else {}
    except Exception:
        return {}


def _safe_name(name: str) -> str:
    n = (name or "").strip().replace("\\", "_").replace("/", "_")
    return re.sub(r'[\x00-\x1f]', "", n)[:180]


def _download_via_navigation(cdp: _CDP, pdf_url: str, timeout: float,
                             dest_dir: Path, filename: str = "") -> dict:
    """导航到 PDF。两条腿都试：

    - **Fetch 拦截**：正常返回 PDF 的站（Springer / ScienceDirect / arXiv）直接拿响应体。
    - **下载接管**：有的站（Wiley 的 `pdfdirect`、带 `?download=true` 的链接）会带
      `Content-Disposition: attachment`，浏览器不渲染而是直接存文件——那就用
      `Browser.setDownloadBehavior` 把它接住。

    两条路径本质相同：都是浏览器顶层请求，能过 JS 挑战；页内 fetch 不能。
    """
    started_at = time.time()
    dl = {"guid": None, "suggested": None}
    for domain in ("Browser", "Page"):
        try:
            cdp.call(f"{domain}.setDownloadBehavior", timeout=10, behavior="allowAndName",
                     downloadPath=str(dest_dir), eventsEnabled=True)
        except Exception:
            pass

    interception = True
    try:
        cdp.call("Fetch.enable", timeout=10,
                 patterns=[{"urlPattern": "*", "requestStage": "Response"}])
    except Exception:
        interception = False

    try:
        # 不能等 Page.navigate 的响应：开了 Fetch 拦截后，导航请求本身会停在
        # Response 阶段，navigate 的响应要等我们放行才回——等它就成了死锁。
        cdp._send("Page.navigate", {"url": pdf_url})
    except Exception as exc:
        return {"ok": False, "reason": "fetch_failed", "method": "navigate",
                "detail": f"导航失败：{exc}"}

    deadline = time.time() + timeout
    saw_document = False
    last = {"ok": False, "reason": "timeout", "method": "navigate", "pdf_url": pdf_url}
    last_probe = time.time()
    while time.time() < deadline:
        got, msg = cdp._pump(min(deadline, time.time() + 3))
        if not msg:
            # 定期看一眼当前地址：被 302 回摘要页时不必再等满预算
            if time.time() - last_probe >= 6:
                last_probe = time.time()
                st = _tab_state(cdp)
                if re.search(r"article-abstract|/abstract|/doi/abs", st.get("url") or "", re.I):
                    return {"ok": False, "reason": "no_entitlement", "method": "navigate",
                            "landing_after_pdf": st.get("url"), "pdf_url": pdf_url}
            continue
        m = msg.get("method")
        if m in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            prm = msg.get("params", {})
            dl["guid"] = prm.get("guid")
            dl["suggested"] = prm.get("suggestedFilename")
            continue
        if m in ("Browser.downloadProgress", "Page.downloadProgress"):
            prm = msg.get("params", {})
            state = prm.get("state")
            if state == "completed" and dl["guid"]:
                src = Path(dest_dir) / str(dl["guid"])
                if src.exists():
                    name = _safe_name(filename) or _safe_name(dl.get("suggested") or "") \
                        or _filename_from(pdf_url, "download")
                    dest2 = Path(dest_dir) / name
                    try:
                        src.replace(dest2)
                    except Exception:
                        dest2 = src
                    size = dest2.stat().st_size if dest2.exists() else 0
                    return {"ok": True, "path": str(dest2), "method": "download",
                            "bytes": size, "content_type": "application/pdf",
                            "pdf_url": pdf_url}
                last = {"ok": False, "reason": "fetch_failed", "method": "download",
                        "detail": "下载完成了但没在目标目录找到文件"}
            elif state == "canceled":
                last = {"ok": False, "reason": "no_entitlement", "method": "download",
                        "detail": "浏览器取消了下载（多数是被权限拦下）"}
            continue
        if m != "Fetch.requestPaused" or not interception:
            continue
        prm = msg.get("params", {})
        rid = prm.get("requestId")
        req = prm.get("request") or {}
        url = req.get("url") or ""
        # Response 阶段的暂停事件里，Chrome 把响应拆成 responseStatusCode/responseHeaders，
        # 不总是给 `response` 对象——按两者都兼容来读，否则会误判成"不是文档"而放走。
        resp = prm.get("response") or {}
        hdrs = {h.get("name", "").lower(): h.get("value", "")
                for h in (prm.get("responseHeaders") or [])}
        mime = (resp.get("mimeType") or hdrs.get("content-type") or "").lower()
        status = resp.get("status") or prm.get("responseStatusCode")
        rtype = prm.get("resourceType") or ""
        is_doc = (rtype == "Document" or url == pdf_url or looks_like_pdf_url(url)) \
            and ("pdf" in mime or url.lower().split("?")[0].endswith(".pdf") or url == pdf_url)

        if not is_doc:
            try:
                cdp.call("Fetch.continueRequest", timeout=5, requestId=rid)
            except Exception:
                pass
            continue

        # 3xx 是跳转，不是响应内容：放行让它自己跳完，别把空 body 当成"文件是空的"。
        # OUP / INFORMS 的 PDF 地址都会先 302 到别的域，真正的内容在下一跳。
        if isinstance(status, int) and 300 <= status < 400:
            try:
                cdp.call("Fetch.continueRequest", timeout=5, requestId=rid)
            except Exception:
                pass
            continue

        saw_document = True
        try:
            body = cdp.call("Fetch.getResponseBody", timeout=max(10, timeout), requestId=rid)
            b64 = (body.get("result") or {}).get("body") or ""
            data = base64.b64decode(b64) if b64 else b""
        except Exception as exc:
            last = {"ok": False, "reason": "fetch_failed", "method": "navigate",
                    "detail": f"取响应体失败：{exc}", "pdf_url": url}
            data = b""
        reason = _classify_bytes(data, mime, status)
        if reason is None:
            try:
                cdp.call("Fetch.failRequest", timeout=5, requestId=rid, errorReason="Aborted")
            except Exception:
                pass
            return {"ok": True, "data": data, "method": "navigate",
                    "content_type": mime, "pdf_url": url, "status": status}
        last = {"ok": False, "reason": reason, "method": "navigate", "status": status,
                "content_type": mime, "bytes": len(data), "pdf_url": url}
        if reason in ("not_pdf", "bot_challenge") and data:
            last["body_head"] = data[:400000].decode("utf-8", "ignore")
        try:
            cdp.call("Fetch.continueRequest", timeout=5, requestId=rid)
        except Exception:
            pass
        time.sleep(0.5)

    # 兜底：不管事件有没有到，直接看目录里有没有新文件（.crdownload 未完成就再等）
    if last.get("reason") in ("timeout", "empty_body", "not_pdf"):
        try:
            fresh = [f for f in Path(dest_dir).iterdir()
                     if f.is_file() and f.stat().st_mtime >= started_at and f.suffix != ".crdownload"]
            if fresh:
                f = max(fresh, key=lambda x: x.stat().st_mtime)
                if f.stat().st_size > 1024:
                    name = _safe_name(filename) or f.name
                    dest2 = Path(dest_dir) / name
                    if dest2 != f:
                        try:
                            f.replace(dest2)
                        except Exception:
                            dest2 = f
                    return {"ok": True, "path": str(dest2), "method": "download",
                            "bytes": dest2.stat().st_size,
                            "content_type": "application/pdf", "pdf_url": pdf_url}
        except Exception:
            pass

    if not saw_document and last.get("reason") == "timeout":
        last["detail"] = "导航后既没等到文档响应，也没触发下载（可能被重定向到登录/挑战页）"
    return last


# ---------------------------------------------------------------- 主入口


def fetch_pdf(target: str, dest_dir: Path, filename: str = "",
              timeout_seconds: int = 60, probe_only: bool = False) -> dict:
    """打开 target → 判断有没有全文 → 用最可能成功的方式取回 → 写盘。

    target 可以是文章页、doi.org 链接、elink 代理链接、PDF 直链，或裸 DOI。
    probe_only=True 时只做权限预检，不下载（3 秒内出结论）。
    """
    url = normalize_target(target)
    if not url:
        return {"ok": False, "reason": "bad_args", "detail": "target 为空",
                "reasons": REASONS}

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    profile_dir = dest_dir.parent / "state" / "chrome-profile"
    boot = _ensure_browser(profile_dir)
    if boot.get("error"):
        return {"ok": False, "reason": "no_browser", "detail": boot["error"],
                "hint": "装一个 Chrome（或 Chromium/Edge），或改用 download_pdf（纯 HTTP 路径）"}

    tab = _new_tab(url)
    if not tab or not tab.get("webSocketDebuggerUrl"):
        return {"ok": False, "reason": "no_tab", "detail": "开不出标签页"}

    cdp = _CDP(tab["webSocketDebuggerUrl"], timeout=max(20, timeout_seconds))
    try:
        cdp.call("Page.enable", timeout=10)
        cdp.call("Runtime.enable", timeout=10)

        # ---- 1) 权限预检：落地页 3 秒定生死
        info: dict = {}
        deadline = time.time() + min(12, timeout_seconds)
        settled_url, settled_hits = "", 0
        while time.time() < deadline:
            try:
                res = cdp.call("Runtime.evaluate", timeout=10, expression=_CLASSIFY_JS,
                               returnByValue=True, awaitPromise=False)
                value = (res.get("result", {}).get("result") or {}).get("value") or {}
            except Exception:
                value = {}
            if isinstance(value, dict):
                info = value or info
                # 有结论就走：找到 PDF / 明确 404 / 明确没订阅
                if value.get("pdfUrl") or value.get("notFound") or value.get("noEntitlement"):
                    info = value
                    break
                # 挑战页不要立刻放弃：真浏览器往往几秒后自己过掉（Cloudflare "Just a moment..."），
                # 所以继续轮询到预检窗口结束，看能不能等到 pdfUrl。
                # 判"已稳定"要连续两次 URL 不变——doi.org 这类跳板会先给一个
                # 标题齐全、status=complete 的中间页，只看一次会误判成"没有全文"。
                cur_url = value.get("url") or ""
                if cur_url and cur_url == settled_url:
                    settled_hits += 1
                else:
                    settled_url, settled_hits = cur_url, 0
                if (value.get("status") == "complete" and value.get("title")
                        and not value.get("challenge") and settled_hits >= 2):
                    break
            time.sleep(1.0)

        pre = {
            "landing": url,
            "page_title": (info.get("title") or "")[:200],
            "resolved_url": info.get("url") or url,
            "found_by": "meta" if info.get("hasMeta") else ("link" if info.get("hasLink") else None),
        }
        pdf_url = info.get("pdfUrl") or (url if looks_like_pdf_url(url) else "")

        if info.get("notFound") and not pdf_url:
            return {"ok": False, "reason": "not_found", **pre,
                    "hint": "页面说这篇不存在或已下架，先确认 DOI/链接本身对不对"}
        if info.get("noEntitlement") and not pdf_url:
            return {"ok": False, "reason": "no_entitlement", **pre,
                    "hint": "落地页只有购买/登录入口，没有 PDF 链接——多半是学校没订这刊。"
                            "别再反复试，直接走文献传递（馆际互借）更快。"}
        if probe_only:
            return {"ok": bool(pdf_url), "probe": True, **pre,
                    "has_fulltext": bool(pdf_url),
                    "reason": "ok" if pdf_url else ("bot_challenge" if info.get("challenge") else "no_entitlement"),
                    "hint": "probe_only：只判断有没有全文，没有下载"}

        if not pdf_url:
            return {"ok": False, "reason": "bot_challenge" if info.get("challenge") else "not_pdf",
                    **pre,
                    "hint": "落地页没给出 PDF 地址。若页面显示挑战/验证，先在浏览器窗口里手动过一次。"}

        # ---- 2) 取文件：导航+拦截优先（能过挑战），失败退回页内 fetch；
        #         若拿回来的是"外壳 HTML"，从里面挖出真正的 PDF 地址再试一跳。
        attempts = []
        # 下载那一跳要给足时间：Cloudflare 挑战 + 大文件下载都要等
        budget = max(25, min(45, timeout_seconds))
        tried: set[str] = set()
        hop_url = pdf_url
        last_fail: dict = {}
        for hop in range(4):
            if not hop_url or hop_url in tried:
                break
            tried.add(hop_url)
            try:
                nav = _download_via_navigation(cdp, hop_url, budget, dest_dir, filename)
            except Exception as exc:
                nav = {"ok": False, "reason": "fetch_failed", "detail": str(exc)[:200],
                       "method": "navigate"}
            attempts.append({k: nav.get(k) for k in ("method", "reason", "status", "bytes")})
            if nav.get("ok") and nav.get("path") and not nav.get("data"):
                # 浏览器直接下载（Content-Disposition）的情况：文件已经在盘上了
                return {
                    "ok": True, "path": nav["path"], "bytes": nav.get("bytes"),
                    "via": "browser", "method": nav.get("method"),
                    "content_type": nav.get("content_type", "application/pdf"),
                    **pre, "pdf_url": nav.get("pdf_url") or hop_url,
                    "attempts": attempts,
                    "hint": "浏览器直接下载的（Content-Disposition），文件已落盘。",
                }
            if nav.get("ok"):
                data = nav["data"]
                ctype = nav.get("content_type", "")
                final_url = nav.get("pdf_url") or hop_url
                method = nav.get("method") or "navigate"

                try:
                    cdp.call("Fetch.disable", timeout=5)
                except Exception:
                    pass

                name = filename or _filename_from(final_url, "download")
                dest = dest_dir / name
                dest.write_bytes(data)
                return {
                    "ok": True,
                    "path": str(dest),
                    "bytes": len(data),
                    "via": "browser",
                    "method": method,
                    "content_type": ctype,
                    **pre,
                    "pdf_url": final_url,
                    "attempts": attempts,
                    "hint": "用的是浏览器登录态（等同你自己点下载）；PDF 在本地，没上传任何地方。",
                }

            last_fail = nav

            # (0) 被弹回摘要页 = 没有全文权限。OUP / INFORMS 的 PDF 地址会 302 到
            #     `/article-abstract/...?redirectedFrom=PDF`，这种情况再等下去也没用。
            st0 = _tab_state(cdp)
            cur = (st0.get("url") or "")
            if re.search(r"article-abstract|/abstract|/doi/abs/|/doi/abs\?|redirectedFrom=PDF", cur, re.I):
                return {"ok": False, "reason": "no_entitlement", **pre, "pdf_url": pdf_url,
                        "landing_after_pdf": cur,
                        "attempts": attempts,
                        "hint": "点进 PDF 又被弹回摘要页——学校没有这刊的全文权限，"
                                "走文献传递（library@xjtlu.edu.cn，附 DOI）最快。",
                        "reasons": REASONS.get("no_entitlement", "")}

            # (a) 页面已经在显示 PDF（ScienceDirect 的 pdf.sciencedirectassets.com）
            st = _tab_state(cdp)
            if (st.get("ct") or "").lower().startswith("application/pdf") and st.get("url"):
                try:
                    again = _download_via_fetch_js(cdp, st["url"], min(30, budget))
                except Exception as exc:
                    again = {"ok": False, "reason": "fetch_failed", "detail": str(exc)[:160],
                             "method": "fetch"}
                attempts.append({k: again.get(k) for k in ("method", "reason", "status", "bytes")})
                if again.get("ok"):
                    data = again["data"]
                    try:
                        cdp.call("Fetch.disable", timeout=5)
                    except Exception:
                        pass
                    name = filename or _filename_from(st["url"], "download")
                    dest = dest_dir / name
                    dest.write_bytes(data)
                    return {"ok": True, "path": str(dest), "bytes": len(data), "via": "browser",
                            "method": "refetch", "content_type": again.get("content_type", ""),
                            **pre, "pdf_url": st["url"], "attempts": attempts,
                            "hint": "浏览器已把 PDF 显示出来，按当前地址重新取了一次。"}

            # (b) 解壳：从 HTML 里挖真地址
            unwrapped = _pdf_from_html((nav.get("body_head") or ""), hop_url)
            if unwrapped and unwrapped not in tried:
                hop_url = unwrapped
                continue

            # (c) 页内 fetch 兜底（有些站只有这条路走得通）
            try:
                js = _download_via_fetch_js(cdp, hop_url, budget)
            except Exception as exc:
                js = {"ok": False, "reason": nav.get("reason") or "fetch_failed",
                      "detail": str(exc)[:200], "method": "fetch"}
            attempts.append({k: js.get(k) for k in ("method", "reason", "status", "bytes")})
            if js.get("ok"):
                data = js["data"]
                ctype = js.get("content_type", "")
                final_url = js.get("pdf_url") or hop_url
                try:
                    cdp.call("Fetch.disable", timeout=5)
                except Exception:
                    pass
                name = filename or _filename_from(final_url, "download")
                dest = dest_dir / name
                dest.write_bytes(data)
                return {
                    "ok": True, "path": str(dest), "bytes": len(data), "via": "browser",
                    "method": "fetch", "content_type": ctype, **pre,
                    "pdf_url": final_url, "attempts": attempts,
                    "hint": "用的是浏览器登录态（等同你自己点下载）；PDF 在本地，没上传任何地方。",
                }

            unwrapped2 = _pdf_from_html((js.get("body_head") or ""), hop_url)
            if unwrapped2 and unwrapped2 not in tried:
                hop_url = unwrapped2
                continue
            # 超时多半是挑战还没过完（Wiley 的 Cloudflare 第一次要几十秒）：
            # 冷启动下重试一次通常就过——profile 里已经种下 clearance cookie。
            if (last_fail or {}).get("reason") == "timeout" and "retried" not in tried:
                tried.add("retried")
                time.sleep(1.5)
                hop_url = pdf_url
                continue
            break

        reason = (last_fail or {}).get("reason") or "fetch_failed"
        if any(a.get("reason") == "bot_challenge" for a in attempts):
            reason = "bot_challenge"
        return {"ok": False, "reason": reason, **pre, "pdf_url": pdf_url,
                "attempts": attempts,
                "hint": "两条路径都没过。挑战类的站请先在浏览器窗口里手动打开这篇全文一次"
                        "（让 clearance cookie 落地），再重试；仍然不行就是学校没订。",
                "reasons": REASONS.get(reason, "")}
    except TimeoutError as exc:
        return {"ok": False, "reason": "timeout", "detail": str(exc)[:200], "target": url}
    except Exception as exc:
        return {"ok": False, "reason": "fetch_failed", "detail": str(exc)[:300], "target": url}
    finally:
        try:
            cdp.call("Page.close", timeout=5)
        except Exception:
            pass
        cdp.close()
