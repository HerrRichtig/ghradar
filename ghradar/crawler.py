"""GitHub Search API 分片抓取。

GitHub Search 单查询最多返回 1000 条，因此按「星数桶 → pushed 年份 → 语言」
逐级切分，把每片压到 1000 条以内；仍超出的片按 stars 降序截断（只保留最成熟的一批，
契合「找成熟项目做参考」的目标）。带限速、429/403 退避与分片断点续传。
"""
from __future__ import annotations

import datetime
import time
from typing import Any, Callable

import requests

from . import config, db


class GitHubClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": config.USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if config.GITHUB_TOKEN:
            self.session.headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        self._last = 0.0

    def search(self, q: str, page: int = 1, per_page: int = 100,
               sort: str = "stars", order: str = "desc") -> dict[str, Any] | None:
        wait = self._last + config.MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        params = {"q": q, "page": page, "per_page": per_page, "sort": sort, "order": order}
        for attempt in range(6):
            try:
                r = self.session.get(config.SEARCH_URL, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 + 2 * attempt)
                continue
            self._last = time.time()
            if r.status_code == 200:
                return r.json()
            if r.status_code == 422:  # 非法查询（如空语言片）
                return {"total_count": 0, "items": []}
            if r.status_code in (403, 429):
                reset = int(r.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset - time.time() + 1, 5) if reset else 30
                time.sleep(min(wait, 120))
                continue
            time.sleep(2 ** attempt)
        return None


def _year_shards() -> list[str]:
    now = datetime.date.today().year
    shards = [f"pushed:{y}-01-01..{y}-12-31" for y in range(now, 2007, -1)]
    shards.append("pushed:<2008-01-01")
    return shards


def _lang_q(lang: str) -> str:
    return f'language:"{lang}"' if " " in lang else f"language:{lang}"


def _upsert_items(conn, items: list[dict], stats: dict[str, int]) -> int:
    for it in items:
        db.upsert_repo(conn, it)
    stats["repos"] += len(items)
    return len(items)


def _fetch_pages(client: GitHubClient, conn, q: str, stats: dict[str, int]) -> int:
    """从 page 1 开始抓取一个叶子分片（最多 1000 条），返回入库条数。"""
    n = 0
    for page in range(1, config.MAX_PAGES_PER_QUERY + 1):
        data = client.search(q, page=page)
        if data is None:
            break
        items = data.get("items", [])
        if not items:
            break
        n += _upsert_items(conn, items, stats)
        if len(items) < config.SEARCH_PER_PAGE:
            break
    return n


def _fetch_leaf(client: GitHubClient, conn, q: str, first: dict, stats: dict[str, int]) -> int:
    """复用已请求的 page 1（first），续抓后续页，返回入库条数。"""
    items = first.get("items", [])
    n = _upsert_items(conn, items, stats)
    total = first.get("total_count", 0) or 0
    if total > len(items):
        for page in range(2, config.MAX_PAGES_PER_QUERY + 1):
            data = client.search(q, page=page)
            if data is None:
                break
            more = data.get("items", [])
            if not more:
                break
            n += _upsert_items(conn, more, stats)
            if len(more) < config.SEARCH_PER_PAGE:
                break
    return n


def _collect(client: GitHubClient, conn, q: str, depth: int,
             stats: dict[str, int], limiter: dict[str, Any], force: bool) -> None:
    """递归切分：读 page 1 的 total_count，超 1000 则按年份/语言再切。"""
    if limiter["max"] is not None and stats["shards"] >= limiter["max"]:
        return
    data = client.search(q, page=1)
    if data is None:
        return
    total = data.get("total_count", 0) or 0
    if total == 0:
        return
    if total <= 1000 or depth >= 2:
        # 叶子：直接抓取（depth>=2 表示按语言切后仍超 1000，截断为 stars 前 1000）
        if not force and db.shard_is_fresh(conn, q):
            stats["skipped"] += 1
            return
        n = _fetch_leaf(client, conn, q, data, stats)
        db.mark_shard_done(conn, q, n)
        conn.commit()
        stats["shards"] += 1
        return
    if depth == 0:
        for yq in _year_shards():
            _collect(client, conn, f"{q} {yq}", depth + 1, stats, limiter, force)
    else:  # depth == 1
        for lq in (_lang_q(l) for l in config.FALLBACK_LANGS):
            _collect(client, conn, f"{q} {lq}", depth + 1, stats, limiter, force)


def _buckets(min_stars: int | None) -> list[tuple[int, int | None]]:
    """返回按星数降序排列的分桶，让最高星（最成熟）的项目先入库。"""
    out: list[tuple[int, int | None]] = []
    for lo, hi in config.STAR_BUCKETS:
        if min_stars:
            if hi is not None and hi <= min_stars:
                continue
            lo = max(lo, min_stars)
        out.append((lo, hi))
    out.reverse()
    return out


def crawl(min_stars: int | None = None, force: bool = False,
          max_shards: int | None = None,
          progress: Callable[[dict[str, int]], None] | None = None) -> dict[str, int]:
    """完整/增量抓取。返回统计 {repos, shards, skipped}。"""
    config.ensure_dirs()
    conn = db.get_conn()
    db.init_db(conn)
    client = GitHubClient()
    stats = {"repos": 0, "shards": 0, "skipped": 0}
    limiter = {"max": max_shards}
    try:
        for lo, hi in _buckets(min_stars):
            base = f"stars:{lo}..{hi}" if hi is not None else f"stars:>={lo}"
            _collect(client, conn, base, 0, stats, limiter, force)
            if progress:
                progress(dict(stats))
            if limiter["max"] is not None and stats["shards"] >= limiter["max"]:
                break
    finally:
        conn.close()
    return stats


def update(min_stars: int | None = None, days: int = 7) -> dict[str, int]:
    """定期更新：先抓最近 N 天新建/活跃的仓库，再对过期分片重枚举。"""
    config.ensure_dirs()
    ms = min_stars or config.DEFAULT_MIN_STARS
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    client = GitHubClient()
    conn = db.get_conn()
    db.init_db(conn)
    stats = {"repos": 0, "shards": 0, "skipped": 0}
    try:
        for q in (f"created:>={since} stars:>={ms}",
                  f"pushed:>={since} stars:>={ms}"):
            n = _fetch_pages(client, conn, q, stats)
            conn.commit()
            stats["shards"] += 1 if n else 0
    finally:
        conn.close()
    # 再对过期分片做增量重枚举（跳过近 7 天已抓的分片）
    stats2 = crawl(min_stars=ms, force=False)
    stats["repos"] += stats2["repos"]
    stats["shards"] += stats2["shards"]
    stats["skipped"] += stats2["skipped"]
    return stats
