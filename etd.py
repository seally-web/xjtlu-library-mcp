"""XJTLU 试卷库 / 学位论文库（etd.xjtlu.edu.cn）客户端。

实测结构（2026-11-16 验证）：
  DbList   GET  /api/v1/Search/DbList              匿名可读，列出 4 个库
  Fields   GET  /api/v1/Search/<dbCode>            取 dbId 与可搜字段
  Search   POST /api/v1/Search/Search              JSON body（camelCase）
  Detail   GET  /api/v1/Detail?dbId&dbCode&recordId&userId
  Download GET  /api/v1/file/Download/<flag>?...   返回带文件名后缀的直链
  Viewer   GET  /api/v1/file/BrowserUrl/<flag>?... 返回带 signature 的取件路径

鉴权是两件套，缺一不可（这是本模块最反直觉的地方）：
  Cookie: XJTLU_UserInfo=<...>    没它 → 403 "非法请求"
  Authorization: bearer <token>   没它 → 401 → 前端跳 SSO
token 取自浏览器 localStorage.token，cookie 取自同一次登录的 XJTLU_UserInfo。

只有 Browse 权限也能取到 PDF：先拿 BrowserUrl（签名带 timestamp，必须现取），
再 GET 该路径即可。Download 权限另算，用于 file/Download 直链。
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from pathlib import Path

import requests

from xjtlu import DATA_HOME, DOWNLOAD_DIR, STATE_DIR, UA

ETD_BASE = "https://etd.xjtlu.edu.cn"
ETD_SESSION_FILE = STATE_DIR / "etd.json"

DB_EXAM = "EXAMXJTLU"       # 试卷库
DB_FYP = "ETDXJTLU"         # 本科毕业设计
DB_PGT = "ETDXJTLU1"        # 授课型硕士论文
DB_PGR = "ETDXJTLU2"        # 研究型学位论文

DB_NAMES = {
    DB_EXAM: "Past Exam Papers",
    DB_FYP: "Final Year Project",
    DB_PGT: "Dissertation",
    DB_PGR: "Thesis",
}

# 每个库的可搜字段（由 /api/v1/Search/<dbCode> 实测返回）
SEARCH_FIELDS = {
    DB_EXAM: ["PaperCode", "PaperTitle", "Year"],
    DB_FYP: ["Title", "Author", "Year"],
    DB_PGT: ["Title", "Author", "Year"],
    DB_PGR: ["Title", "Author", "Year"],
}

CONTAINS = 2   # RetrievalMode: 2=Contains, 0=Is


class NotLoggedIn(RuntimeError):
    pass


# ---------------------------------------------------------------- session

def load_session() -> dict:
    if ETD_SESSION_FILE.exists():
        try:
            return json.loads(ETD_SESSION_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def session_status() -> dict:
    """看一眼当前存的试卷库会话是否还有效（会真发一次请求）。"""
    data = load_session()
    if not data:
        return {"saved": False, "hint": "还没登录过：用 browser_login 开窗口登录，或 save_exam_session 手动粘"}
    names = sorted((data.get("cookie") or {}).keys())
    try:
        probe = exam_search("MTH", limit=1)
        alive = bool(probe.get("ok"))
    except NotLoggedIn:
        alive = False
    except Exception as exc:
        return {"saved": True, "alive": None, "error": str(exc)[:200],
                "cookie_names": names, "user_id": data.get("user_id")}
    return {"saved": True, "alive": alive, "cookie_names": names,
            "user_id": data.get("user_id"), "file": str(ETD_SESSION_FILE)}


def save_session(cookie_header: str, token: str, user_id: str = "") -> dict:
    """保存并立即验证一次；验证不过不落盘。"""
    cookie_header = (cookie_header or "").strip()
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    jar = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            jar[k.strip()] = v.strip()
    if not jar:
        return {"ok": False, "reason": "bad_cookie", "hint": "Cookie 头为空，形如 XJTLU_UserInfo=..."}
    if not token:
        return {"ok": False, "reason": "bad_token", "hint": "token 为空，取自 localStorage.token"}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"cookie": jar, "token": token, "user_id": str(user_id or "").strip()}
    ETD_SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    try:
        os.chmod(ETD_SESSION_FILE, 0o600)
    except OSError:
        pass

    try:
        probe = exam_search("MTH", limit=1)
    except NotLoggedIn as exc:
        ETD_SESSION_FILE.unlink(missing_ok=True)
        return {"ok": False, "reason": "login_required", "detail": str(exc),
                "hint": "cookie 或 token 无效/已过期：重新登录 etd.xjtlu.edu.cn，重新取 localStorage.token 与 XJTLU_UserInfo"}
    if not probe.get("ok"):
        return {"ok": False, "reason": "probe_failed", "detail": probe}
    return {
        "ok": True,
        "saved": str(ETD_SESSION_FILE),
        "user_id": payload["user_id"] or "(未填，部分接口可能要)",
        "probe_total": probe.get("total"),
        "hint": "会话已保存，可用 exam_search / exam_download。",
    }


def _session() -> requests.Session:
    state = load_session()
    if not state.get("token"):
        raise NotLoggedIn("还没有保存 etd 会话：先调用 save_exam_session")
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Authorization": f"bearer {state['token']}",
        "Origin": ETD_BASE,
        "Referer": f"{ETD_BASE}/index.html",
    })
    for k, v in (state.get("cookie") or {}).items():
        s.cookies.set(k, v)
    return s


def _guard(r: requests.Response) -> None:
    """把前端的两种拒绝翻译成明确原因。"""
    body = (r.text or "")[:200]
    if r.status_code == 401 or "非法请求" in body and r.status_code in (401, 403):
        raise NotLoggedIn("服务器拒绝：cookie 或 token 失效")
    if r.status_code == 403:
        raise NotLoggedIn(f"服务器拒绝（403）：{body}")


# ---------------------------------------------------------------- helpers

def _json(r: requests.Response):
    """解析响应；这个站用纯字符串表达拒绝（"非法请求"/"没有权限"），统一翻成 NotLoggedIn。"""
    try:
        data = r.json()
    except ValueError:
        head = (r.text or "")[:400].lstrip().lower()
        if head.startswith("<!doctype") or head.startswith("<html"):
            raise NotLoggedIn(
                "接口返回的是登录页 HTML —— 会话过期了（这个站的 token 撑不了太久）。"
                "用 browser_login 重新登录一次即可。")
        raise NotLoggedIn(f"非 JSON 响应（HTTP {r.status_code}）：{(r.text or '')[:200]}")
    if isinstance(data, str):
        text = data[:300]
        if "非法" in text or "登录" in text or "权限" in text:
            raise NotLoggedIn(f"服务端拒绝：{text}")
    return data


def _strip(value) -> str:
    """搜索结果里的高亮是 <span style="color:red">…</span>，且全角空格是 &nbsp;。"""
    t = str(value or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("\t", " ")
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def db_list() -> dict:
    r = requests.get(f"{ETD_BASE}/api/v1/Search/DbList", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    items = r.json()
    return {
        "ok": True,
        "databases": [
            {"code": d.get("databasecode"), "name": d.get("databasename").split("#")[0].strip(),
             "id": d.get("sysid"), "login": d.get("loginFlag") == "1"}
            for d in items
        ],
        "note": "dbCode 传 exam_search 的 db_code 参数；默认 EXAMXJTLU（试卷库）",
    }


def db_fields(db_code: str = DB_EXAM) -> dict:
    r = requests.get(f"{ETD_BASE}/api/v1/Search/{db_code}", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    d = _json(r)
    if not isinstance(d, dict):
        return {"ok": False, "reason": "unexpected_response", "body": str(d)[:200]}
    return {
        "ok": bool(d.get("isSuccess")),
        "db_id": d.get("dbId"),
        "db_name": d.get("dbDisplayName"),
        "search_fields": [f.get("fieldName") for f in (d.get("searchField") or [])],
        "sort_fields": [f.get("fieldName") for f in (d.get("sortField") or [])],
        "categories": [{"cls_id": c.get("clsId"), "expression": c.get("expression")}
                       for c in (d.get("dbClass") or [])],
    }


# ---------------------------------------------------------------- search

def exam_search(query: str = "", field: str = "PaperCode", page: int = 1,
                limit: int = 20, db_code: str = DB_EXAM, exact: bool = False) -> dict:
    """检索试卷/论文。field: PaperCode | PaperTitle | Year（不同库字段名不同）。"""
    s = _session()
    fields = SEARCH_FIELDS.get(db_code, SEARCH_FIELDS[DB_EXAM])
    if field not in fields:
        return {"ok": False, "reason": "bad_field",
                "hint": f"field 只能是 {fields}"}
    body = {
        "dbId": 3 if db_code == DB_EXAM else None,
        "dbCode": db_code,
        "pageIndex": max(1, int(page)),
        "pageSize": max(1, min(int(limit), 100)),
        "advancedSearchParams": (
            [{"searchType": 0 if exact else CONTAINS, "searchField": field, "searchContent": query}]
            if query else []
        ),
        "categoryExpression": "",
        "catalogCode": "",
    }
    if body["dbId"] is None:
        body["dbId"] = db_fields(db_code).get("db_id")

    r = s.post(f"{ETD_BASE}/api/v1/Search/Search", json=body, timeout=45)
    _guard(r)
    if r.status_code != 200:
        return {"ok": False, "reason": "http_error", "status": r.status_code, "body": (r.text or "")[:300]}
    d = _json(r)          # 会话过期时这里会抛 NotLoggedIn（登录页 HTML / "非法请求"）
    if not isinstance(d, dict):
        return {"ok": False, "reason": "unexpected_response", "body": str(d)[:300]}

    rows = ((d.get("result") or {}).get("overviewResult") or [])
    items = []
    for it in rows:
        rid = it.get("syS_FLD_SYSID")
        items.append({
            "record_id": rid,
            "title": _strip(it.get("paperTitle") or it.get("Title")),
            "code": _strip(it.get("paperCode") or ""),
            "department": _strip(it.get("department") or ""),
            "year": _strip(it.get("year") or ""),
        })
    return {
        "ok": True,
        "db": d.get("dbName") or DB_NAMES.get(db_code, db_code),
        "total": d.get("recordCount"),
        "page": d.get("pageIndex"),
        "count": len(items),
        "items": items,
        "next": (f"exam_detail(record_id) 看完整信息；exam_download(record_id) 取 PDF" if items else
                 "没有结果：换个 field（PaperCode / PaperTitle / Year）或把 query 缩短"),
    }


def exam_detail(record_id: int, db_code: str = DB_EXAM) -> dict:
    s = _session()
    state = load_session()
    params = {"dbId": 3 if db_code == DB_EXAM else db_fields(db_code).get("db_id"),
              "dbCode": db_code, "recordId": int(record_id)}
    if state.get("user_id"):
        params["userId"] = state["user_id"]
    r = s.get(f"{ETD_BASE}/api/v1/Detail", params=params, timeout=30)
    _guard(r)
    if r.status_code != 200:
        return {"ok": False, "reason": "http_error", "status": r.status_code}
    raw = _json(r)
    d = (raw.get("detailResult") if isinstance(raw, dict) else None) or {}
    rows = ((d.get("result") or {}).get("detailResult") or [])
    fields = {}
    if rows:
        for k, v in rows[0].items():
            if isinstance(v, str):
                fields[k] = _strip(v)
            else:
                fields[k] = v
    return {
        "ok": bool(d.get("isSuccess")),
        "db": d.get("dbName"),
        "record_id": int(record_id),
        "title": fields.get("paperTitle") or fields.get("Title"),
        "code": fields.get("paperCode"),
        "year": fields.get("year"),
        "department": fields.get("department"),
        "semester": fields.get("semsterABB"),
        "type": fields.get("typeABB"),
        "security_date": fields.get("securitydate"),
        "flag": d.get("flag", fields.get("flag")),
        "can_browse": d.get("hasBrowserRight"),
        "can_download": d.get("hasDownloadRight"),
        "raw": fields,
    }


def exam_download(record_id: int, filename: str = "", db_code: str = DB_EXAM) -> dict:
    """取一份试卷/论文 PDF。Browse 权限即可（走 BrowserFile 签名路径）。"""
    detail = exam_detail(record_id, db_code)
    if not detail.get("ok"):
        return {"ok": False, "reason": "detail_failed", "detail": detail}
    if not (detail.get("can_browse") or detail.get("can_download")):
        return {"ok": False, "reason": "no_permission",
                "hint": "这份记录你没有浏览/下载权限（保密期内或未授权），"
                        f"security_date={detail.get('security_date')}"}

    s = _session()
    state = load_session()
    flag = detail.get("flag") or 0
    base = {"dbId": 3 if db_code == DB_EXAM else db_fields(db_code).get("db_id"),
            "dbCode": db_code, "recordId": int(record_id), "flag": flag}
    if state.get("user_id"):
        base["userId"] = state["user_id"]

    # 1) 直链里带着真实文件名：.../0;2023-2024_MTH002_F_S2.pdf
    name = ""
    try:
        r = s.get(f"{ETD_BASE}/api/v1/file/Download/{flag}", params=base, timeout=30)
        if r.status_code == 200:
            urls = _json(r)
            if isinstance(urls, list) and urls:
                name = str(urls[0]).rsplit(";", 1)[-1]
    except Exception:
        pass

    # 2) 取件路径（签名带 timestamp，必须现取现用）
    r = s.get(f"{ETD_BASE}/api/v1/file/BrowserUrl/{flag}", params=base, timeout=30)
    _guard(r)
    if r.status_code != 200:
        return {"ok": False, "reason": "browser_url_failed", "status": r.status_code,
                "body": (r.text or "")[:200]}
    urls = _json(r)
    if not isinstance(urls, list) or not urls:
        return {"ok": False, "reason": "no_browse_url", "body": str(urls)[:200]}

    r = s.get(f"{ETD_BASE}{urls[0]}", timeout=90)
    _guard(r)
    if r.status_code != 200:
        return {"ok": False, "reason": "fetch_failed", "status": r.status_code,
                "hint": "签名可能过期（timestamp 绑定时间）——重试一次即可"}
    if not r.content.startswith(b"%PDF"):
        return {"ok": False, "reason": "not_pdf", "content_type": r.headers.get("Content-Type"),
                "preview": (r.text or "")[:200]}

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        stem = re.sub(r"[^\w.\-]+", "_", name or f"{detail.get('code') or record_id}_{detail.get('year') or ''}")
        filename = stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"
    dest = DOWNLOAD_DIR / filename
    dest.write_bytes(r.content)
    return {
        "ok": True,
        "path": str(dest),
        "bytes": len(r.content),
        "title": detail.get("title"),
        "code": detail.get("code"),
        "year": detail.get("year"),
        "hint": "用的是浏览权限（BrowserFile 签名路径）；若站点改版导致签名失效，重试一次。",
    }
