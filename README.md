# xjtlu-library-mcp

[![MCP](https://img.shields.io/badge/MCP-server-5B6EF5)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一个 MCP server，把西交利物浦大学图书馆塞进 AI agent 的工具箱：查馆藏、查论文、判断学校订没订、按需取全文；外加**往年试卷库 / FYP / 学位论文**的检索与下载。

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
| `browser_download` | 反爬站点（Wiley / T&F / OUP / Euclid）的 PDF：用真浏览器取回 |
| `save_library_cookie` | 校外使用时写入登录态（只存本机） |
| `browser_login` | 开窗口让你登录，自动抓 token + cookie（**不用复制粘贴**） |
| `browser_login_status` | 看 Chrome 在不在、调试端口通不通、会话还有效吗 |
| `save_exam_session` | 手动写入试卷库登录态（cookie + token，只存本机） |
| `exam_databases` | 列出试卷库 / FYP / 授课型论文 / 研究型论文四个库 |
| `exam_search` | 按课程代码、标题、学年检索往年试卷（如 `MTH002`、`ACC309`） |
| `exam_detail` | 一条记录的完整信息：学年、学期、院系、保密截止日、有无取件权限 |
| `exam_download` | 把一份试卷 / 论文存成本地 PDF |

一个真实返回（馆藏）：

```json
{
  "title": "Measure theory, probability, and stochastic processes",
  "call_number": "QA312./L46/4/EN",
  "copies": 1, "available": 1,
  "author": "Jean-François Le Gall", "year": "2022", "publisher": "Springer",
  "holdings": [["QA312./L46/4/EN", "23134802", "", "SIP Floor 9", "可借"]]
}
```

一个真实返回（试卷检索）：

```json
{
  "db": "西交利物浦大学试卷库", "total": 456,
  "items": [{
    "record_id": 13933,
    "title": "Multivariable Calculus and Statistics",
    "code": "MTH002",
    "department": "Department of Foundational Mathematics",
    "year": "2023-2024"
  }]
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
https://cdn.jsdelivr.net/npm/dsh-xjtlu-library@latest/market/index.json
```

索引文件**就在 npm 包里**（`bundle/market/index.json`），`@latest` 由 jsDelivr 的 npm 后端解析，所以：

- 发一版 npm，索引自动跟上，**不需要手动 purge**
- 不依赖 `github.com`（那个域名在国内时通时断，曾让安装连续失败三次）
- 安装也走 npm（`dsh-xjtlu-library@X.Y.Z`），比 `git+` 快一个数量级

> 早期版本用过 `…/gh/seally-web/xjtlu-library-mcp@index-vX.Y.Z/market/index.json`（钉 tag 的 GitHub 路径）。**钉 tag 的地址永远不会更新**——曾经因此让索引源停在 0.1.8 而无人察觉。那条路仍可用，但不推荐。
> **不能用 `github.com/…/raw/…`**：它 302，而 DSH 拉索引时拒绝重定向。

**装完之后不会自动升级。** DSH 把版本钉进 profile 依赖，索引里出现新版本只是「可安装」。要更新就重新装一次：先在插件列表里移除，再在 Discover 里装新版。

然后搜「XJTLU 图书馆」安装。插件包见 [`bundle/`](bundle/)，它只做一件事：把本服务注册成一个名为 `xjtlu-library` 的 stdio MCP 连接。

**方式二：手动连接器**

新建 MCP 连接，`command` 指向 venv 里的 python，`args: ["server.py"]`，`cwd` 指向本仓库。

> 方式一（插件索引）需要本机有 `uv`，并需要首次启动时能访问 GitHub 拉取代码；方式二只依赖上面建好的 venv，不需要 `uv`。

## 校外怎么用

校园 IP 自带权限，什么配置都不用。离开校园网时有三条路：

1. **XJTLU VPN**（首选，最省心）
2. **EZproxy / CARSI** 由 elink 跳板自动完成
3. **粘贴 cookie**：浏览器登录 `lib.xjtlu.edu.cn` 并走一次 elink 跳转后，复制 Cookie 请求头交给 `save_library_cookie`

> ⚠️ 只从 `lib.xjtlu.edu.cn` 首页抓到的 Cookie 是**百度统计 + Google Analytics**，不含任何身份信息，存了也没用。真正的会话在 `elink.xjtlu.edu.cn`。

## 启动卡住？（v0.1.8 修掉的坑）

**症状**：桌面端双击没反应 / 一直停在 `Loading plugins…`，但进程在、窗口不显示。

**原因**：这个 bundle 会插入一条 MCP 连接，命令是 `uvx --from <源> xjtlu-library-mcp`。早期用 `git+https://…@v0.1.x`，冷缓存下 uvx 要 clone 仓库 + 建构建环境，实测 **142 秒**；而 MCP 客户端默认没有启动超时（`startupTimeoutMs === undefined` 时 `remaining()` 返回 undefined，连接无限期等待），于是整个启动被吊死。

**现在**：

| 改动 | 效果 |
|---|---|
| `--from` 换成预构建 wheel（30KB 单文件） | 冷启动 **11 秒**（含装 37 个依赖） |
| `startupTimeoutMs: 30000` | 即使 uvx 抽风，30 秒后放弃该连接，启动继续 |
| `failOnStartupError: false` | 失败只影响这个插件，不拦 Harness |

**万一还是卡住**，编辑 `~/.dsh/profiles/desktop/cordis.patch.yml`，给这条连接加：

```yaml
- id: xjtlu-library
  disabled: true
```

## 试卷库（etd.xjtlu.edu.cn）

同一个站上挂着四个库：`EXAMXJTLU` 往年试卷、`ETDXJTLU` 本科 FYP、`ETDXJTLU1` 授课型硕士论文、`ETDXJTLU2` 研究型学位论文。

它比图书馆更挑剔：**要 cookie 和 token 两件套**，缺一样就失败：

| 缺什么 | 现象 |
|---|---|
| 缺 `XJTLU_UserInfo` cookie | `403`，body 是字符串 `"非法请求"` |
| 缺 `Authorization: bearer` token | `401`，前端跳 `sso.xjtlu.edu.cn` 重新登录 |

拿法有三个，按省心程度排：

**① 让插件自己开窗口（推荐，零复制）**

```
browser_login()          # 弹一个 Chrome 窗口，你在里面登录
```

窗口用的是独立 profile（`~/.xjtlu-library-mcp/state/chrome-profile`），不碰你日常浏览器；登录成功后插件通过 Chrome DevTools Protocol 读 `localStorage.token` 和 `XJTLU_UserInfo`，存好并关窗。因为 profile 会保留，下次多半免登录就能抓到。超时了窗口不关，登完再调一次会复用同一个窗口。

> 为什么不塞 Playwright：那要多下 100+ MB 浏览器，而 CDP 是 Chrome 自带能力，只多一个几 KB 的 websocket 客户端。为什么不抓密码：插件完全不碰登录表单。

**② 手动粘（备用）**

拿法（浏览器已登录的状态下）：

```js
localStorage.getItem('token')   // 一长串，填给 save_exam_session 的 token
localStorage.getItem('uId')     // 数字学号，填给 user_id（可选但建议）
```

cookie 取自 Network 里任一 `/api/v1/...` 请求的 Request Headers，**只需要 `XJTLU_UserInfo=...` 这一段**——别的（`_ga`、`_clsk`、`tfstk`）都是埋点。

```python
save_exam_session(cookie_string="XJTLU_UserInfo=nGUb...", token="O5mG...", user_id="2362344")
```

有个反直觉的点：`exam_detail` 里 `hasDownloadRight` 常常是 `false`，但**依然能取到 PDF**——只要有 browse 权限，走 `BrowserUrl` 的签名路径（签名带 `timestamp`，必须现取现用，所以每次下载都会先要一次新签名）。

保密期由 `security_date` 决定，没过期的记录任何人都下不了，工具会明确返回 `no_permission` 而不是含糊的失败。

## 能力边界（重要）

- **只做单篇、按需**。不提供批量抓取，请遵守学校与出版社的订阅协议。
- **不绕过鉴权**。不代填密码、不处理 MFA；cookie 由你自己提供、只存本机 `~/.xjtlu-library-mcp/state/`（可用 `XJTLU_LIBRARY_HOME` 改路径）。
- **下载分三档**，别期待一键全下：
  - ✅ 开放获取（arXiv / PMC / MDPI）直链可用 → `download_pdf`
  - ✅ Springer 系（`10.1007` / `10.2991` / `10.1186` / `10.1057`）走直链模板 → `download_pdf`
  - ✅/⚠️ Wiley / Taylor & Francis / ScienceDirect / OUP 有反爬风控（Cloudflare、JS 挑战、签名 URL），
    `requests` 一定拿 403 或挑战页 —— 用 `browser_download`，它走真浏览器，v0.2.0 起这四家实测都能下

### `browser_download` 怎么做事的（v0.2.0 重写）

旧做法是"页内 `fetch` 取 PDF"，这条路过不了 JS 挑战——ScienceDirect 的 `pdfft` 在 `fetch` 下永远回 51KB 挑战页。
现在改成三条腿，按顺序试：

1. **权限预检**（`probe_only=true` 可只跑到这一步）：打开落地页 1–4 秒内判断
   `ok` / `bot_challenge` / `no_entitlement` / `not_found`。**没有全文就别下**，省掉 60 秒空等。
2. **导航 + 拦截**：`Page.navigate` 到真正的 PDF 地址（顶层导航能过挑战），同时用
   `Fetch.enable(requestStage=Response)` 把响应体截下来；如果站点直接触发下载
   （`Content-Disposition: attachment`），改用 `Browser.setDownloadBehavior` 接住文件。
3. **解壳**：有的站先回一个 HTML 外壳，真地址藏在里面（Wiley 是
   `var src = "/doi/pdfdirect/…"`，ScienceDirect 是走到 `pdf.sciencedirectassets.com` 签名 URL）。
   最多解 4 跳，也能识别"页面已经在显示 PDF"的情况并按当前 URL 重取。

失败原因不再是含糊的 `not_pdf`，而是分开的：

| reason | 意思 | 你该做什么 |
|---|---|---|
| `ok` | 拿到了 | — |
| `bot_challenge` | 卡在 Cloudflare / JS 挑战 | 在浏览器窗口里手动打开这篇一次，再重试 |
| `no_entitlement` | 页面有购买/登录入口，没有 PDF | 学校没订，走文献传递，别再试 |
| `not_found` | DOI 不存在或页面 404 | 检查 DOI |
| `timeout` | 挑战没在预算内过完 | 会自动重试一次；仍失败就手动开一次 |

v0.2.0 实测（同一台机器、校内网）：Springer 6.6s / ScienceDirect 6.8s / arXiv 12.7s / Wiley 46s（挑战要等）。
作为对照，旧版 Springer 46 秒超时、ScienceDirect 直接 403。

> 要求只有一个：**在那个 Chrome 窗口里你确实能看这篇全文**。插件不破解任何付费墙。

- **元数据来源**：文章检索主用 [OpenAlex](https://openalex.org)，不可用时自动回落 [Crossref](https://crossref.org)。两者都无需登录，也不碰 EBSCO 的私有前端接口——那接口无文档、随改版失效。
- **馆藏接口**只允许校内 IP，所以本工具对校外同学的价值取决于你有无 VPN。

## 维护

学校改版后大概率要动的地方都在 `xjtlu.py` 顶部常量：`OPAC_BASE`、`ELINK`、`EBSCO_CUSTOMER`。OPAC 用校内自签证书，请求必须 `verify=False`。

发布新版本（git source，推荐）：

1. 改 `bundle/package.json` 的 `version`，并把 `bundle/cordis.patch.yml` 里 `uvx --from` 的 `@vX.Y.Z` 改成同一个 tag
2. 提交 → `git tag -a vX.Y.Z -m ...` → 推送 main 和 tag
3. `SOURCE=git python3 market/build_index.py` 同时生成两份索引：仓库的 `market/index.json`（git source）和 `bundle/market/index.json`（npm source，随包分发、`@latest` 指向它）→ 提交推送
4. 同步打通 PyPI 之外的第二条分发路径——**预构建 wheel**（bundle 就是靠它启动的）：
   `./.venv/bin/python -m pip wheel . --no-deps -w dist` → 把 `dist/xjtlu_library_mcp-X.Y.Z-py3-none-any.whl` 一起提交
5. 打 tag 并推送：`git tag -a vX.Y.Z -m ...` → `git push origin main vX.Y.Z`（索引随 npm 走，**不需要**再打 index tag）
6. `npm publish`（在 `bundle/` 里）→ 等 5–10 分钟让 tarball 落地 → 确认 `https://cdn.jsdelivr.net/npm/dsh-xjtlu-library@latest/market/index.json` 已显示新版本；bundle 里 wheel URL 的 `@vX.Y.Z` 也要一起改

`@tag` 是必须的：`uvx --from git+…` 不带 ref 时会缓存已构建的工具环境，改了代码不生效。

npm 那条路（`SOURCE=npm`）也能用，但如果账号强制 2FA，`npm publish` 会走 staged publishing——先返回 `+ pkg@ver`，实际是 202 待审，要人工批准才真的发布。

### 发 npm（实测流程，别被 `+ pkg@ver` 骗了）

即使把账号 2FA 改成 `auth-only`，npm 现在仍对「能绕过 2FA 的 token」收紧限制，发布依旧走 staged。**看到 `+ dsh-xjtlu-library@X.Y.Z` 不等于发布成功**，务必用三条命令确认并批准：

```bash
cd bundle
export npm_config_cache="$PWD/../.npmtmp/cache"   # 沙箱内 ~/.npm 不可写

npm publish --registry=https://registry.npmjs.org/     # 会打印 202，版本进 stage
npm stage list dsh-xjtlu-library --json --registry=https://registry.npmjs.org/
npm stage approve <上一步返回的 id> --registry=https://registry.npmjs.org/
```

两个坑：

- **`npm stage approve` 必须交互执行**，不能 `| tail`。一旦管道化，npm 拿不到 TTY，浏览器授权流程走不了，直接报 `EOTP`。
- 授权后如果 `npm stage list` 还是空，说明手里的 token 权限不够（旧 token 是在 `auth-and-writes` 时期发的）；先 `npm login --auth-type=web` 换一个新 token。

版本号不存在「0.1.7/0.1.8 了，下一个是 0.1.9」之外的跳跃：`0.1.8` 曾经发成一个 tarball 缺失的幽灵版本，所以补了一版 `0.1.9`，内容与 0.1.8 一致。

## License

[MIT](LICENSE)。与西交利物浦大学官方无关，非官方项目。
