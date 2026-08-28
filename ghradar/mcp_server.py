"""ghradar MCP server。供 Claude Desktop / Cursor 等 AI 客户端调用。

启动命令：ghradar-mcp  （或 python -m ghradar.mcp_server）

支持三种传输（GHRADAR_TRANSPORT 环境变量或 main() 参数选择）：
  stdio            默认，标准输入输出，给本地 MCP 客户端
  streamable-http  HTTP 传输（默认 /mcp 端点），给远程或不支持 stdio 的 harness
  sse              SSE 传输（旧式 HTTP MCP）
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server import MCPServer

from . import config, db, retriever
from .autoupdate import maybe_trigger_background_update

server = MCPServer(
    name="ghradar",
    title="GitHub Project Radar",
    description="检索已索引的 GitHub 开源项目。用自然语言（中/英）描述想法，"
                "返回相关的成熟项目（星数、语言、描述、话题、链接），省去逐页浏览 GitHub 的 token。",
    version="0.1.0",
)

_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        config.ensure_dirs()
        _conn = db.get_conn()
        db.init_db(_conn)
    return _conn


@server.tool()
def search_repos(query: str, top_k: int = 20, min_stars: int = 0,
                 language: str = "", topic: str = "") -> list[dict[str, Any]]:
    """按想法检索 GitHub 项目。

    Args:
        query: 想法/需求的自然语言描述，中文或英文均可，例如 "本地运行的大模型 RAG 框架"。
        top_k: 返回条数（默认 20）。
        min_stars: 只返回星数不低于该值的项目（默认 0）。
        language: 按编程语言过滤，如 "python"、"go"（空串表示不过滤）。
        topic: 按 GitHub topic 过滤，如 "llm"、"rag"（空串表示不过滤）。
    """
    maybe_trigger_background_update()
    conn = _get_conn()
    return retriever.search(conn, query, top_k=top_k, min_stars=min_stars,
                            language=language or None, topic=topic or None)


@server.tool()
def get_repo(full_name: str) -> dict[str, Any]:
    """按 full_name（owner/repo）查询某个已索引项目的详情。"""
    maybe_trigger_background_update()
    conn = _get_conn()
    r = db.get_repo(conn, full_name)
    if not r:
        return {"error": f"not found: {full_name}"}
    r["topics"] = json.loads(r["topics"]) if r["topics"] else []
    return r


@server.tool()
def index_stats() -> dict[str, Any]:
    """查看索引统计：仓库总数、已向量化数量、Top 语言等。"""
    maybe_trigger_background_update()
    conn = _get_conn()
    return db.stats(conn)


def main(transport: str | None = None, host: str = "127.0.0.1", port: int = 8000) -> None:
    transport = transport or os.environ.get("GHRADAR_TRANSPORT", "stdio")
    if transport in ("sse", "streamable-http"):
        host = host or os.environ.get("GHRADAR_HOST", "127.0.0.1")
        port = int(port or os.environ.get("GHRADAR_PORT", "8000"))
        server.run(transport, host=host, port=port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
