# xjtlu-library-mcp

[![MCP](https://img.shields.io/badge/MCP-server-5B6EF5)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一个 MCP server，把西交利物浦大学图书馆塞进 AI agent 的工具箱：查馆藏、查论文、判断学校订没订、按需取全文。

> 专门解决两件磨人的事：一次检索要跨三个站（elink 网关 → 发现系统 → 出版社），以及"跑一趟图书馆或翻十几个页面才能确认一本书能不能借"。

## 它做什么

| 工具 | 作用 |
|---|---|
| `library_status` | 自查：在校内网吗、登录态还有效吗、EBSCO 门户能否自动登录 |
| `search_books` | 检索馆藏纸本 / 电子书（汇文 OPAC），返回索书号、复本数、可借数 |
| `book_detail` | 单本书的完整书目 + 逐册馆藏（哪个馆区、可否借、条码、分册） |
| `search_articles` | 检索期刊 / 会议论文，返回 DOI、期刊、年份、被引数、OA 直链 |
| `open_fulltext` | 经学校 elink 代理探一篇全文能否拿到（不下载） |
| `download_pdf` | 按需下载单篇 PDF（自动尝试出版社直链） |
| `save_library_cookie` | 校外使用时写入登录态（只存本机） |

一个真实返回：

```json
{
  "title": "Measure theory, probability, and stochastic processes",
  "call_number": "QA312./L46/4/EN",
  "copies": 1, "available": 1,
  "author": "Jean-François Le Gall", "year": "2022", "publisher": "Springer",
  "holdings": [["QA312./L46/4/EN", "23134802", "", "SIP Floor 9", "可借"]]
}
```

## 装

```bash
git clone https://github.com/seally-web/xjtlu-library-mcp.git
cd xjtlu-library-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py          # stdio 传输，正常启动后等待 JSON-RPC
```

接到任意 MCP 客户端，以 Claude Desktop 为例：

```json
{
  "mcpServers": {
    "xjtlu-library": {
      "command": "/绝对路径/xjtlu-library-mcp/.venv/bin/python",
      "args": ["server.py"],
      "cwd": "/绝对路径/xjtlu-library-mcp"
    }
  }
}
```

不想手动建虚拟环境的话，装了 [uv](https://docs.astral.sh/uv/) 之后一条命令即可：

```bash
uvx --from git+https://github.com/seally-web/xjtlu-library-mcp xjtlu-library-mcp
```

## 装到 DeepSeek Harness

**方式一：插件索引（推荐）**

**设置 → 插件 → 实验性插件 → 打开「自定义插件索引」**，在 **Discover** 里添加索引地址：

```
https://cdn.jsdelivr.net/gh/seally-web/xjtlu-library-mcp@main/market/index.json
```

> 国内网络访问 `raw.githubusercontent.com` 经常超时（实测直接连不上），而 DSH 拉索引时**拒绝重定向**，所以别用 `github.com/…/raw/…` 那种会 302 的地址。jsDelivr 实测 200/1.8s。

然后搜「XJTLU 图书馆」安装。插件包见 [`bundle/`](bundle/)，它只做一件事：把本服务注册成一个名为 `xjtlu-library` 的 stdio MCP 连接。

**方式二：手动连接器**

新建 MCP 连接，`command` 指向 venv 里的 python，`args: ["server.py"]`，`cwd` 指向本仓库。

> 两种方式都需要本机有 `uv`（插件索引走 `uvx`），且首次启动要能访问 GitHub 拉取代码。

## 校外怎么用

校园 IP 自带权限，什么配置都不用。离开校园网时有三条路：

1. **XJTLU VPN**（首选，最省心）
2. **EZproxy / CARSI** 由 elink 跳板自动完成
3. **粘贴 cookie**：浏览器登录 `lib.xjtlu.edu.cn` 并走一次 elink 跳转后，复制 Cookie 请求头交给 `save_library_cookie`

> ⚠️ 只从 `lib.xjtlu.edu.cn` 首页抓到的 Cookie 是**百度统计 + Google Analytics**，不含任何身份信息，存了也没用。真正的会话在 `elink.xjtlu.edu.cn`。

## 能力边界（重要）

- **只做单篇、按需**。不提供批量抓取，请遵守学校与出版社的订阅协议。
- **不绕过鉴权**。不代填密码、不处理 MFA；cookie 由你自己提供、只存本机 `~/.xjtlu-library-mcp/state/`（可用 `XJTLU_LIBRARY_HOME` 改路径）。
- **下载分三档**，别期待一键全下：
  - ✅ 开放获取（arXiv / PMC / MDPI）直链可用
  - ✅ Springer 系（`10.1007` / `10.2991` / `10.1186` / `10.1057`）走直链模板
  - ⚠️ Wiley / Taylor & Francis / Project Euclid / ScienceDirect 有反爬风控，脚本会拿到 403 或 JS 挑战页，需用浏览器打开
- **元数据来源**：文章检索主用 [OpenAlex](https://openalex.org)，不可用时自动回落 [Crossref](https://crossref.org)。两者都无需登录，也不碰 EBSCO 的私有前端接口——那接口无文档、随改版失效。
- **馆藏接口**只允许校内 IP，所以本工具对校外同学的价值取决于你有无 VPN。

## 维护

学校改版后大概率要动的地方都在 `xjtlu.py` 顶部常量：`OPAC_BASE`、`ELINK`、`EBSCO_CUSTOMER`。OPAC 用校内自签证书，请求必须 `verify=False`。

发布新版本（git source，推荐）：

1. 改 `bundle/package.json` 的 `version`，并把 `bundle/cordis.patch.yml` 里 `uvx --from` 的 `@vX.Y.Z` 改成同一个 tag
2. 提交 → `git tag -a vX.Y.Z -m ...` → 推送 main 和 tag
3. `SOURCE=git python3 market/build_index.py` 把索引钉到该 commit → 提交推送

`@tag` 是必须的：`uvx --from git+…` 不带 ref 时会缓存已构建的工具环境，改了代码不生效。

npm 那条路（`SOURCE=npm`）也能用，但如果账号强制 2FA，`npm publish` 会走 staged publishing——先返回 `+ pkg@ver`，实际是 202 待审，要人工批准才真的发布。

## License

[MIT](LICENSE)。与西交利物浦大学官方无关，非官方项目。
