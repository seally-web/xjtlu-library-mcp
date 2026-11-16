"""XJTLU Library MCP server — 让 agent 直接检索馆藏、找论文、经学校代理取全文。

启动：.venv/bin/python server.py   （stdio 传输）
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

import etd
import xjtlu

mcp = FastMCP("xjtlu-library")


@mcp.tool()
def library_status() -> dict:
    """检查图书馆访问状态：是否在校内网、是否已保存登录 cookie、EBSCO 门户能否自动登录。

    在检索失败或下载 403/302 到登录页时先调用它。
    """
    cookies = xjtlu.load_cookies()
    return {
        "network": xjtlu.network_scope(),  # campus / off_campus / unknown
        "cookie_names": sorted(cookies),
        "cookie_file": str(xjtlu.COOKIE_FILE),
        "ebsco": xjtlu.ebsco_session_state(),
        "note": "off_campus 时需要 VPN，或先登录 elink 并通过 save_library_cookie 写入登录态",
    }


@mcp.tool()
def search_books(keyword: str, field: str = "title", page: int = 1, limit: int = 20) -> dict:
    """检索西交利物浦大学馆藏纸本 / 电子书（汇文 OPAC）。

    Args:
        keyword: 检索词，如 "measure theory"。
        field: title / author / subject / isbn / any，默认按题名。
        page: 页码，从 1 开始。
        limit: 每页条数，1-50，默认 20。

    返回每题名、索书号（如 QA312./B64/7/EN）、馆藏复本数、可借数、marc_no（用于 book_detail）。
    """
    return xjtlu.search_books(keyword, field=field, page=page, limit=limit)


@mcp.tool()
def book_detail(marc_no: str) -> dict:
    """查看一本书的完整书目信息与馆藏状态。

    Args:
        marc_no: search_books 返回的 marc_no。
    """
    return xjtlu.book_detail(marc_no)


@mcp.tool()
def search_articles(query: str, limit: int = 10, year_from: int = 0, open_access_only: bool = False) -> dict:
    """检索期刊论文 / 会议论文，返回 DOI、期刊、年份、被引次数与校内外可用的全文入口。

    元数据来自 OpenAlex（2.5 亿条、无需登录）；全文链接自动套 elink 代理，
    在校园网或已登录状态下可直接打开出版社页面。

    Args:
        query: 检索词，支持英文词组，如 "stochastic gradient Langevin dynamics"。
        limit: 返回条数，1-50。
        year_from: 只要该年之后的文献，0 表示不限。
        open_access_only: 只返回有开放获取版本的文献。
    """
    return xjtlu.search_articles(
        query, limit=limit, year_from=year_from or None, open_access_only=open_access_only
    )


@mcp.tool()
def open_fulltext(doi_or_url: str) -> dict:
    """经学校 elink 代理检查一篇文献的全文是否可拿到（不下载）。

    Args:
        doi_or_url: DOI（如 10.1007/b97479）或完整 URL。

    返回最终落地地址、HTTP 状态、Content-Type；is_pdf=true 表示可直接下载。
    """
    target = doi_or_url
    if not target.startswith("http"):
        target = f"https://doi.org/{target.lstrip('doi:').strip()}"
    return xjtlu.resolve_fulltext(target)


@mcp.tool()
def download_pdf(doi_or_url: str, filename: str = "") -> dict:
    """下载一篇文献的 PDF 到插件目录 downloads/（仅限你有权访问的内容，单篇按需）。

    Args:
        doi_or_url: DOI、doi.org 链接、出版社 PDF 直链或任何全文落地页 URL 都行。
        filename: 可选文件名；留空则用最终 URL 路径的末段。

    已经以 .pdf 结尾的直链会原样使用，不会再套出版社模板；空 body 会被判为失败并换下一个候选，
    不会留下 0 字节文件。若返回 not_pdf，说明该地址是登录页或 HTML 落地页，先在浏览器确认直链再重试。
    """
    target = doi_or_url
    if not target.startswith("http"):
        target = f"https://doi.org/{target.lstrip('doi:').strip()}"
    return xjtlu.download_fulltext(target, filename or None)


@mcp.tool()
def save_library_cookie(cookie_string: str) -> dict:
    """保存登录态，用于校外访问。粘浏览器里的 Cookie 请求头或 Netscape cookies.txt 内容。

    获取方法：浏览器登录 lib.xjtlu.edu.cn 后打开开发者工具 → Network → 任一请求 →
    复制 Request Headers 里的 Cookie 值（形如 "JSESSIONID=...; elinkid=..."）。
    这会写入本机 state/cookies.json，不会上传到任何地方；失效后在浏览器重登再粘一次即可。
    """
    n = xjtlu.save_cookies(cookie_string)
    return {
        "saved": n,
        "file": str(xjtlu.COOKIE_FILE),
        "hint": "若仍 302 到登录页，说明该 cookie 不含 elink/SSO 会话，请在 elink 登录后再复制",
    }


@mcp.tool()
def save_exam_session(cookie_string: str, token: str, user_id: str = "") -> dict:
    """保存试卷库 etd.xjtlu.edu.cn 的登录态（搜索和下载都要它，缺一不可）。

    要两样东西，都来自同一次浏览器登录：
      1) token —— 控制台执行 localStorage.getItem('token')
      2) cookie —— 形如 XJTLU_UserInfo=nGUb...（Network 里任一 api 请求的 Cookie 头，
         只取 XJTLU_UserInfo 这一段即可）
    可选 user_id = localStorage.getItem('uId')，部分接口会用到。

    只保存在本机的 ~/.xjtlu-library-mcp/state/etd.json（权限 600），会上传吗？不会。
    保存时会立刻验证一次，无效则不落盘。
    """
    return etd.save_session(cookie_string, token, user_id)


@mcp.tool()
def exam_databases() -> dict:
    """列出 etd.xjtlu.edu.cn 上的库：试卷库 EXAMXJTLU、FYP ETDXJTLU、
    授课型硕士论文 ETDXJTLU1、研究型学位论文 ETDXJTLU2。"""
    return etd.db_list()


@mcp.tool()
def exam_search(query: str = "", field: str = "PaperCode", page: int = 1,
                limit: int = 20, db_code: str = "EXAMXJTLU", exact: bool = False) -> dict:
    """检索往年试卷 / FYP / 学位论文（需要先 save_exam_session）。

    field 取值（试卷库）：PaperCode（课程代码，如 MTH002、ACC309）、PaperTitle、Year。
    exact=True 为精确匹配，默认包含匹配。返回 record_id，交给 exam_detail / exam_download。
    一次 20 条，分页用 page。
    """
    try:
        return etd.exam_search(query, field=field, page=page, limit=limit,
                               db_code=db_code, exact=exact)
    except etd.NotLoggedIn as exc:
        return {"ok": False, "reason": "login_required", "detail": str(exc),
                "hint": "先 save_exam_session(cookie_string, token, user_id)"}


@mcp.tool()
def exam_detail(record_id: int, db_code: str = "EXAMXJTLU") -> dict:
    """看一条试卷/论文的完整信息：标题、课程码、学年、学期、院系、保密截止日，
    以及 can_browse / can_download（决定能否取到 PDF）。"""
    try:
        return etd.exam_detail(record_id, db_code=db_code)
    except etd.NotLoggedIn as exc:
        return {"ok": False, "reason": "login_required", "detail": str(exc)}


@mcp.tool()
def exam_download(record_id: int, filename: str = "", db_code: str = "EXAMXJTLU") -> dict:
    """把一份往年试卷 / FYP / 学位论文存成本地 PDF，落在 ~/.xjtlu-library-mcp/downloads/。

    权限说明：只要能浏览（hasBrowserRight）就能取到，走 BrowserUrl 的签名路径；
    hasDownloadRight 是另一回事，用于直链，不影响这里。若返回 no_permission，
    表示这份记录在保密期内（看 security_date）或你的账号未被授权。
    """
    try:
        return etd.exam_download(record_id, filename=filename, db_code=db_code)
    except etd.NotLoggedIn as exc:
        return {"ok": False, "reason": "login_required", "detail": str(exc)}


def main() -> None:
    """入口点：也供 pyproject 的 console_scripts 调用（xjtlu-library-mcp）。"""
    mcp.run()


if __name__ == "__main__":
    main()
