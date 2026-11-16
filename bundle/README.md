# dsh-xjtlu-library

把[西交利物浦大学图书馆](https://lib.xjtlu.edu.cn/)接给 DeepSeek Harness 的 agent：检索馆藏纸本与电子书、按 DOI 查论文元数据、经学校 elink 代理判断订阅并取回可得的 PDF。

这是个**配置型插件包**：它自己不实现检索，只是把 [`xjtlu-library-mcp`](https://github.com/seally-web/xjtlu-library-mcp) 这个 MCP 服务注册成一个名为 `xjtlu-library` 的 stdio 连接，提供 7 个工具：

| 工具 | 作用 |
|---|---|
| `library_status` | 自查：在校内网吗、EBSCO 会话有效吗 |
| `search_books` | 查馆藏（含索书号、复本/可借数） |
| `book_detail` | 单册级馆藏：哪个馆区、是否可借 |
| `search_articles` | 查论文元数据（DOI、期刊、被引数、OA 直链） |
| `open_fulltext` | 经 elink 代理判断能否拿到全文 |
| `download_pdf` | 单篇按需下载 |
| `save_library_cookie` | 校外时写入登录态 |

## 前置条件

- 本机装有 [`uv`](https://docs.astral.sh/uv/)（提供 `uvx`）；首次启动会从 GitHub 拉取服务代码
- 处于校园网，或已连 XJTLU VPN（馆藏接口只对校内 IP 开放）

## 边界

只做单篇按需检索与下载，不批量抓取；不代填密码、不绕 MFA。Wiley、Taylor & Francis、Project Euclid、ScienceDirect 有反爬风控，脚本会拿到 403 或 JS 挑战页，需用浏览器打开。登录态与下载文件只留在本机 `~/.xjtlu-library-mcp`。

非官方项目，与西交利物浦大学无关。MIT。
