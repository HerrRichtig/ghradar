"""ghradar 命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, db


def _conn():
    config.ensure_dirs()
    conn = db.get_conn()
    db.init_db(conn)
    return conn


def cmd_search(args: argparse.Namespace) -> int:
    from . import retriever
    conn = _conn()
    try:
        results = retriever.search(conn, args.query, top_k=args.top,
                                   min_stars=args.min_stars, language=args.lang,
                                   topic=args.topic)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_results(results)
    return 0


def _print_results(results: list[dict]) -> None:
    if not results:
        print("（无结果）")
        return
    for r in results:
        tags = " ".join(f"#{t}" for t in (r["topics"] or [])[:5])
        print(f"{r['rank']:>2}. {r['full_name']}  ⭐{r['stars']:,}  "
              f"[{r['language'] or '-'}]  ({r['matched_by']})")
        print(f"    {r['url']}")
        if r["description"]:
            print(f"    {r['description'][:160]}")
        if tags:
            print(f"    {tags}")
        print()


def cmd_crawl(args: argparse.Namespace) -> int:
    from . import crawler
    stats = crawler.crawl(min_stars=args.min_stars, force=args.force,
                          max_shards=args.max_shards)
    print(f"抓取完成：新增/更新 {stats['repos']} 条，分片 {stats['shards']}，跳过 {stats['skipped']}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from . import crawler
    stats = crawler.update(min_stars=args.min_stars, days=args.days)
    print(f"更新完成：新增/更新 {stats['repos']} 条，分片 {stats['shards']}，跳过 {stats['skipped']}")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from . import embed
    conn = _conn()
    try:
        n = embed.embed_new(conn, batch_size=args.batch)
    finally:
        conn.close()
    print(f"已生成 {n} 条向量" if n else "没有待生成的向量（索引已最新）")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = _conn()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    print(f"仓库总数：{s['total']:,}")
    print(f"已向量化：{s['embedded']:,}")
    print(f"最高星数：{s['max_stars']:,}" if s["max_stars"] else "最高星数：-")
    print("Top 语言：")
    for lang, c in s["top_languages"]:
        print(f"  {lang or '(无)'}: {c:,}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp_server
    mcp_server.main(transport=args.transport, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ghradar",
                                description="定期更新的 GitHub 项目检索器")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="按想法检索相关项目")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=config.DEFAULT_TOP_K)
    s.add_argument("--min-stars", type=int, default=0)
    s.add_argument("--lang")
    s.add_argument("--topic")
    s.add_argument("--json", action="store_true", help="输出 JSON")
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("crawl", help="抓取/刷新索引")
    c.add_argument("--min-stars", type=int, default=None)
    c.add_argument("--max-shards", type=int, default=None, help="最多抓取的分片数（调试用）")
    c.add_argument("--force", action="store_true", help="忽略近 7 天已抓分片，强制重抓")
    c.set_defaults(func=cmd_crawl)

    u = sub.add_parser("update", help="增量更新（最近新建/活跃 + 过期分片）")
    u.add_argument("--min-stars", type=int, default=None)
    u.add_argument("--days", type=int, default=7)
    u.set_defaults(func=cmd_update)

    e = sub.add_parser("embed", help="为新增/变更仓库生成向量")
    e.add_argument("--batch", type=int, default=config.EMBED_BATCH)
    e.set_defaults(func=cmd_embed)

    st = sub.add_parser("stats", help="索引统计")
    st.set_defaults(func=cmd_stats)

    m = sub.add_parser("mcp", help="以 MCP 服务运行")
    m.add_argument("--transport", choices=["stdio", "sse", "streamable-http"],
                   default=None, help="传输方式（默认 stdio，可用 GHRADAR_TRANSPORT 覆盖）")
    m.add_argument("--host", default=None, help="HTTP 监听地址（默认 127.0.0.1）")
    m.add_argument("--port", type=int, default=None, help="HTTP 监听端口（默认 8000）")
    m.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
