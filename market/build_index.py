#!/usr/bin/env python3
"""生成 DSH 插件市场索引 market/index.json。

索引指向本仓库里的 bundle/ 目录（git source，pinned 到某个 commit），
所以发布顺序是：改版本 → commit → push → 运行本脚本 → commit 索引 → push。

用法：python3 market/build_index.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "bundle"
REPO_URL = "https://github.com/seally-web/xjtlu-library-mcp"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> None:
    pkg = json.loads((BUNDLE / "package.json").read_text("utf-8"))
    version = pkg["version"]
    commit = git("rev-parse", "HEAD")
    # 用引入当前版本的提交时间做发布时间戳；索引与版本一一对应时它就是发布时刻
    publish_ts = int(git("log", "-1", "--format=%ct"))

    index = {
        "format": 1,
        "title": {"en": "XJTLU Library", "zh": "XJTLU 图书馆"},
        "description": {
            "en": "Plugins for searching the XJTLU library catalogue and its subscribed literature.",
            "zh": "检索西交利物浦大学馆藏与订阅文献的插件。",
        },
        "metadata": {
            "categories": {
                "research": {"title": {"en": "Research", "zh": "科研"}},
            },
            "labels": {
                "mcp": {"title": {"en": "MCP", "zh": "MCP"}},
                "library": {"title": {"en": "Library", "zh": "图书馆"}},
            },
        },
        "plugins": [
            {
                "name": pkg["name"],
                "title": {"en": "XJTLU Library", "zh": "XJTLU 图书馆"},
                "description": {
                    "en": "Search XJTLU print and e-book holdings, look up article metadata by DOI, and fetch "
                          "available PDFs through the university's elink proxy. Requires uv and the campus network or VPN.",
                    "zh": "检索西浦馆藏纸本与电子书、按 DOI 查论文元数据、经学校 elink 代理取回可得的 PDF。"
                          "需要本机装 uv，并处于校园网或已连 XJTLU VPN。",
                },
                "icon": "icon.svg",
                "author": "seally-web",
                "homepage": f"{REPO_URL}#readme",
                "repository": {"url": REPO_URL, "directory": "bundle"},
                "metadata": {
                    "category": "research",
                    "keywords": ["library", "opac", "xjtlu", "mcp", "文献", "馆藏"],
                    "labels": ["mcp", "library"],
                },
                "versions": [
                    {
                        "version": version,
                        "publishTimestamp": publish_ts,
                        "source": {"type": "git", "url": REPO_URL, "commit": commit, "path": "bundle"},
                    }
                ],
            }
        ],
    }

    out = ROOT / "market" / "index.json"
    out.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"wrote {out} · {pkg['name']}@{version} · {commit[:12]}")


if __name__ == "__main__":
    main()
