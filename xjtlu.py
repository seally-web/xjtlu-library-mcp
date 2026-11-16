"""XJTLU 图书馆检索客户端（OPAC / Discover / elink 代理）。

实测结构（2026-11-16 验证）：
  OPAC      https://opac.xjtlu.edu.cn/opac/openlink.php   汇文 v5.7，纯 GET，校内 IP 或 VPN
  Item      https://opac.xjtlu.edu.cn/opac/item.php?marc_no=<b64>
  Discover  https://research.ebsco.com/c/wwk3pd/search/results?q=<kw>   EBSCO EDS，需 SSO
  代理跳板  https://elink.xjtlu.edu.cn/go?url=<url>   校外由它做 SSO + 代理
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # OPAC 用校内自签证书

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

OPAC_BASE = "https://opac.xjtlu.edu.cn/opac"
OPAC_IP = "10.7.0.140"
ELINK = "https://elink.xjtlu.edu.cn/go"
EBSCO_CUSTOMER = "wwk3pd"

HERE = Path(__file__).resolve().parent

# 运行时数据放固定的用户目录，而不是包目录：通过 uvx / pip 安装时包目录是临时缓存，
# 升级即失效，登录态和已下载的 PDF 跟着一起没。用 XJTLU_LIBRARY_HOME 可覆盖。
_data_home = os.environ.get("XJTLU_LIBRARY_HOME")
DATA_HOME = Path(_data_home).expanduser() if _data_home else Path.home() / ".xjtlu-library-mcp"
STATE_DIR = DATA_HOME / "state"
COOKIE_FILE = STATE_DIR / "cookies.json"
DOWNLOAD_DIR = DATA_HOME / "downloads"

SEARCH_FIELDS = {
    "title": "题名",
    "author": "著者",
    "subject": "主题",
    "isbn": "ISBN",
    "any": "任意词",
}


# ---------------------------------------------------------------- 基础工具

def _clean(text: str) -> str:
    """汇文把中英文都编码成 &#x4E2D; 实体，需还原；顺带压缩空白。"""
    t = html.unescape(html.unescape(text or ""))
    return re.sub(r"\s+", " ", t).strip()


def load_cookies() -> dict:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def save_cookies(raw: str) -> int:
    """接受 'a=1; b=2' 形式的 Cookie 头，或 Netscape cookies.txt 内容。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    jar: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:  # Netscape 格式：domain flag path secure expiry name value
            parts = line.split("\t")
            if len(parts) >= 7:
                jar[parts[5]] = parts[6]
            continue
        if "=" in line:
            name, _, value = line.partition("=")
            jar[name.strip()] = value.strip().rstrip(";")
    if jar:
        COOKIE_FILE.write_text(json.dumps(jar, ensure_ascii=False), "utf-8")
    return len(jar)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    for k, v in load_cookies().items():
        s.cookies.set(k, v)
    return s


def network_scope() -> str:
    """判断当前是否处于校内网：OPAC 只解析到内网地址。"""
    import socket

    try:
        ip = socket.gethostbyname("opac.xjtlu.edu.cn")
    except OSError:
        return "unknown"
    return "campus" if ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.")) else "off_campus"


# ---------------------------------------------------------------- OPAC

def search_books(keyword: str, field: str = "title", page: int = 1, limit: int = 20) -> dict:
    if field not in SEARCH_FIELDS:
        field = "any"
    limit = max(1, min(int(limit), 50))
    s = make_session()
    params = {
        "strSearchType": field,
        "match_flag": "forward",
        "historyCount": 1,
        "strText": keyword,
        "doctype": "ALL",
        "displaypg": limit,
        "showmode": "list",
        "sort": "CATA_DATE",
        "orderby": "desc",
        "dept": "ALL",
    }
    if page > 1:
        params["page"] = page
    r = s.get(f"{OPAC_BASE}/openlink.php", params=params, timeout=30, verify=False)
    r.encoding = "utf-8"
    if r.status_code != 200:
        return {"ok": False, "error": f"OPAC 返回 {r.status_code}", "hint": "校外请先连 VPN 或经 SSO 登录"}
    # 校外时 OPAC 会被 trust.xjtlu.edu.cn 的 SSO 门户接管，返回登录页而不是结果列表
    if "trust.xjtlu.edu.cn" in r.url or "search_book_list" not in r.text:
        return {
            "ok": False,
            "reason": "login_required",
            "network": network_scope(),
            "hint": "馆藏检索只对校园网 IP 开放：连上 XJTLU VPN 后重试，"
                    "或经 https://elink.xjtlu.edu.cn/go?url=https://opac.xjtlu.edu.cn/ 完成一次 SSO 登录。",
        }
    items = parse_book_list(r.text)[:limit]
    total = None
    m = re.search(r"共\s*(\d+)\s*条", r.text)
    if m:
        total = int(m.group(1))
    return {
        "ok": True,
        "query": keyword,
        "field": SEARCH_FIELDS[field],
        "page": page,
        "total": total,
        "count": len(items),
        "items": items,
    }


def parse_book_list(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "lxml")
    out: list[dict] = []
    for li in soup.select("ol#search_book_list li.book_list_info"):
        h3 = li.find("h3")
        if not h3:
            continue
        a = h3.find("a", href=True)
        kind = _clean(h3.find("span").get_text()) if h3.find("span") else ""
        title = _clean(a.get_text()) if a else ""
        title = re.sub(r"^\d+\.", "", title).strip().rstrip("/").strip()
        callno = ""
        if a:
            tail = a.find_next_sibling(string=True)
            if tail:
                callno = _clean(str(tail))
        p = li.find("p")
        authors, publisher, year, copies, available = "", "", "", None, None
        if p:
            span = p.find("span")
            if span:
                txt = _clean(span.get_text(" "))
                m1 = re.search(r"馆藏复本[:：]\s*(\d+)", txt)
                m2 = re.search(r"可借复本[:：]\s*(\d+)", txt)
                copies = int(m1.group(1)) if m1 else None
                available = int(m2.group(1)) if m2 else None
            # <p> 用 <br> 分行：馆藏复本 / 可借复本 / 责任者 / 出版者+年份 / (评分) / 馆藏
            lines = [x for x in (_clean(l) for l in p.get_text("\n").split("\n")) if x]
            body = [l for l in lines if not l.startswith(("馆藏复本", "可借复本"))
                    and l not in {"馆藏"} and not re.fullmatch(r"\(\s*\d+\s*\)", l)]
            if body:
                authors = body[0].strip().rstrip(".")
            if len(body) > 1:
                pub_line = body[1]
                m = re.search(r"((?:19|20)\d{2})", pub_line)
                if m:
                    year = m.group(1)
                    publisher = re.sub(r"\[\s*\]", "", pub_line[: m.start()] + pub_line[m.end():])
                    publisher = publisher.strip().strip(",;. ")
                else:
                    publisher = pub_line.strip().strip(",;. ")
        marc = None
        if a and "marc_no=" in a["href"]:
            marc = re.search(r"marc_no=([^&]+)", a["href"]).group(1)
        out.append(
            {
                "title": title,
                "type": kind,
                "call_number": callno,
                "copies": copies,
                "available": available,
                "author": authors[:120],
                "year": year,
                "publisher": publisher[:80],
                "marc_no": marc,
                "detail_url": f"{OPAC_BASE}/item.php?marc_no={marc}" if marc else None,
            }
        )
    return out


def book_detail(marc_no: str) -> dict:
    s = make_session()
    r = s.get(f"{OPAC_BASE}/item.php", params={"marc_no": marc_no}, timeout=30, verify=False)
    r.encoding = "utf-8"
    if r.status_code != 200:
        return {"ok": False, "error": f"item.php 返回 {r.status_code}"}
    if "trust.xjtlu.edu.cn" in r.url or "item_detail" not in r.text:
        return {
            "ok": False,
            "reason": "login_required",
            "network": network_scope(),
            "hint": "详情页同样只对校园网开放：连上 XJTLU VPN 后重试。",
        }
    soup = BeautifulSoup(r.text, "lxml")
    # 只取书目详情区，避免把页脚的“预约申请/图书评论/登录框”混进字段
    area = soup.select_one("div#bookdetail, div.bookdetail, table#item_detail, div#item_detail") or soup
    body = _clean(area.get_text(" "))
    fields = {}
    labels = ["题名/责任者", "版本说明", "出版发行项", "ISBN", "载体形态项", "丛编题名",
              "学科主题", "中图法分类号", "文献类型", "个人责任者", "一般附注"]
    stop = labels + ["豆瓣简介", "全部MARC", "馆藏信息", "预约申请", "图书评论",
                     "显示全部馆藏信息", "借阅趋势", "用户名", "西交利物浦大学"]
    for label, key in [
        ("题名/责任者", "title"),
        ("版本说明", "edition"),
        ("出版发行项", "published"),
        ("ISBN", "isbn"),
        ("载体形态项", "physical"),
        ("丛编题名", "series"),
        ("学科主题", "subject"),
        ("中图法分类号", "clc"),
        ("一般附注", "note"),
    ]:
        pat = re.escape(label) + r"[:：]\s*(.*?)(?=\s(?:" + "|".join(map(re.escape, stop)) + r")[:：]|\s(?:豆瓣简介|全部MARC)|$)"
        m = re.search(pat, body)
        if m:
            fields[key] = m.group(1).strip()[:300]
    # 馆藏表格：条码 / 索书号 / 状态 / 馆藏地
    holdings = []
    for tr in soup.select("table tr"):
        cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        if len(cells) >= 3 and any("可借" in c or "借出" in c for c in cells):
            holdings.append(cells)
    return {"ok": True, "marc_no": marc_no, "fields": fields, "holdings": holdings[:20], "url": f"{OPAC_BASE}/item.php?marc_no={marc_no}"}


# ---------------------------------------------------------------- Discover / EBSCO

def discover_url(keyword: str) -> str:
    return (
        f"{ELINK}?url=https://research.ebsco.com/c/{EBSCO_CUSTOMER}"
        f"/search/results?q={quote(keyword)}"
    )


def ebsco_session_state() -> dict:
    """探测 EBSCO 门户是否已带有效登录态。"""
    s = make_session()
    try:
        r = s.get(f"https://research.ebsco.com/c/{EBSCO_CUSTOMER}", timeout=25, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}
    logged_in = "oidc/callback" not in r.url and "login" not in r.url.lower()
    return {
        "reachable": True,
        "final_url": r.url,
        "logged_in": logged_in,
        "cookie_count": len(s.cookies),
    }


OPENALEX = "https://api.openalex.org/works"


def _openalex_items(query, limit, year_from, open_access_only):
    params = {
        "search": query,
        "per-page": limit,
        "mailto": "library-mcp@xjtlu.edu.cn",
        "select": "id,doi,title,publication_year,cited_by_count,primary_location,"
                  "best_oa_location,open_access,authorships,type",
    }
    if year_from:
        params["filter"] = f"from_publication_date:{year_from}-01-01"
    if open_access_only:
        params["filter"] = (params.get("filter", "") + ",is_oa:true").lstrip(",")
    r = requests.get(OPENALEX, params=params, timeout=30,
                     headers={"User-Agent": f"xjtlu-library-mcp ({UA})"})
    if r.status_code != 200:
        raise RuntimeError(f"OpenAlex {r.status_code}")
    data = r.json()
    items = []
    for w in data.get("results", []):
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        oa = w.get("best_oa_location") or {}
        doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
        authors = [a["author"]["display_name"] for a in (w.get("authorships") or [])[:6]
                   if a.get("author", {}).get("display_name")]
        items.append({
            "title": _clean(w.get("title") or ""),
            "year": w.get("publication_year"),
            "venue": src, "type": w.get("type"), "doi": doi,
            "cited_by": w.get("cited_by_count"),
            "is_oa": (w.get("open_access") or {}).get("is_oa"),
            "oa_pdf": oa.get("pdf_url"), "authors": authors,
            "library_url": elink(f"https://doi.org/{doi}") if doi else None,
        })
    return items, data.get("meta", {}).get("count")


def _crossref_items(query, limit, year_from, open_access_only):
    """Crossref 兜底：免费、无需 key，但没有 OA 标记与 PDF 直链。"""
    params = {"query": query, "rows": min(limit * 3, 100),
              "select": "DOI,title,container-title,issued,is-referenced-by-count,"
                        "author,type,link"}
    if year_from:
        params["filter"] = f"from-pub-date:{year_from}-01-01"
    r = requests.get("https://api.crossref.org/works", params=params, timeout=30,
                     headers={"User-Agent": f"xjtlu-library-mcp/1.0 (mailto:{UA})"})
    if r.status_code != 200:
        raise RuntimeError(f"Crossref {r.status_code}")
    items = []
    for it in r.json()["message"]["items"]:
        doi = it.get("DOI")
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        authors = []
        for a in (it.get("author") or [])[:6]:
            nm = " ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name")
            if nm:
                authors.append(nm)
        pdfs = [l.get("URL") for l in (it.get("link") or [])
                if "pdf" in (l.get("content-type") or "")]
        items.append({
            "title": _clean((it.get("title") or [""])[0]),
            "year": parts[0] if parts else None,
            "venue": (it.get("container-title") or [None])[0],
            "type": it.get("type"), "doi": doi,
            "cited_by": it.get("is-referenced-by-count"),
            "is_oa": True if pdfs else None, "oa_pdf": pdfs[0] if pdfs else None,
            "authors": authors,
            "library_url": elink(f"https://doi.org/{doi}") if doi else None,
        })
    if open_access_only:
        items = [i for i in items if i["oa_pdf"]]
    return items[:limit], len(items)


def search_articles(query: str, limit: int = 10, year_from: int | None = None,
                    open_access_only: bool = False) -> dict:
    """文章检索：主用 OpenAlex（元数据全、有 OA 标记），503 时自动回落 Crossref。

    全文一律走 elink 代理 -> 学校订阅；EBSCO 门户只作人工核对入口，
    不抓它的私有前端接口（不稳定且可能违反条款）。
    """
    limit = max(1, min(int(limit), 50))
    source, err = "openalex", None
    try:
        items, total = _openalex_items(query, limit, year_from, open_access_only)
    except Exception as e:                                   # noqa: BLE001
        err, source = str(e), "crossref"
        try:
            items, total = _crossref_items(query, limit, year_from, open_access_only)
        except Exception as e2:                              # noqa: BLE001
            return {"ok": False, "error": f"检索失败：{err}；Crossref 兜底也失败：{e2}"}
    out = {"ok": True, "query": query, "source": source, "total": total,
           "count": len(items), "items": items[:limit]}
    if err:
        out["fallback_reason"] = err
        if open_access_only:
            out["warning"] = "OpenAlex 不可用，Crossref 无 OA 标记，open_access_only 只能按是否有 PDF 直链粗筛"
    return out


# ---------------------------------------------------------------- 代理 / 下载

def elink(url: str) -> str:
    return f"{ELINK}?url={quote(url, safe='')}"


def resolve_fulltext(url: str) -> dict:
    """经 elink 跳板检查目标是否可达、是否需要登录。"""
    s = make_session()
    target = elink(url)
    r = s.get(target, timeout=40, allow_redirects=True, stream=True, verify=False)
    ctype = r.headers.get("Content-Type", "")
    out = {
        "requested": url,
        "proxied": target,
        "status": r.status_code,
        "final_url": r.url,
        "content_type": ctype,
        "logged_in": "oidc" not in r.url and "login" not in r.url.lower(),
    }
    r.close()
    return out


# 已知可直接取到 PDF 的出版社直链模板（DOI 前缀 -> URL 模板）
PDF_PATTERNS = {
    "10.1007": "https://link.springer.com/content/pdf/{doi}.pdf",
    "10.2991": "https://link.springer.com/content/pdf/{doi}.pdf",
    "10.1186": "https://link.springer.com/content/pdf/{doi}.pdf",
    "10.1057": "https://link.springer.com/content/pdf/{doi}.pdf",
}


def _pdf_candidates(url: str) -> list[str]:
    """按可信度排列的候选直链：出版社 PDF 模板 > elink 代理 DOI > 原样。"""
    doi = None
    m = re.search(r"(10\.\d{4,9}/[^\s?#]+)", url)
    if m:
        doi = m.group(1).rstrip("/")
    out = []
    if doi:
        tpl = PDF_PATTERNS.get(doi.split("/")[0])
        if tpl:
            out.append(tpl.format(doi=doi))
    out += [elink(url), url]
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq


def download_fulltext(url: str, filename: str | None = None) -> dict:
    s = make_session()
    tried = []
    for cand in _pdf_candidates(url):
        try:
            r = s.get(cand, timeout=120, allow_redirects=True, stream=True, verify=False)
        except Exception as e:
            tried.append({"url": cand, "error": str(e)[:120]}); continue
        ctype = r.headers.get("Content-Type", "")
        if not ("pdf" in ctype.lower() or r.url.lower().endswith(".pdf")):
            tried.append({"url": cand, "status": r.status_code,
                          "content_type": ctype[:60], "final_url": r.url})
            r.close(); continue
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = filename or Path(quote(r.url, "")).name or "download.pdf"
        name = re.sub(r"[^\w.\-]+", "_", name)[-120:]
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = DOWNLOAD_DIR / name
        with dest.open("wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        r.close()
        return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
                "via": cand}
    return {"ok": False, "reason": "not_pdf", "tried": tried,
            "hint": "没拿到 PDF：可能是出版社风控挡住脚本（Wiley/T&F/Project Euclid 常见）、"
                    "直链需要文章页 token，或学校未订阅。用浏览器打开 elink 链接人工下载，"
                    "或让 agent 驱动 Chrome 打开。"}
