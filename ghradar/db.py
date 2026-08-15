"""SQLite 存储层：仓库表 + FTS5(trigram) 索引 + 抓取状态。

FTS 使用独立表（非 external content），由 upsert_repo 显式同步，
避免触发器在 INSERT OR REPLACE 下的复杂语义。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id                INTEGER PRIMARY KEY,
    full_name         TEXT NOT NULL UNIQUE,
    name              TEXT,
    owner             TEXT,
    description       TEXT,
    html_url          TEXT,
    homepage          TEXT,
    language          TEXT,
    stargazers_count  INTEGER DEFAULT 0,
    forks_count       INTEGER DEFAULT 0,
    open_issues_count INTEGER DEFAULT 0,
    topics            TEXT,            -- JSON 数组
    license           TEXT,
    archived          INTEGER DEFAULT 0,
    created_at        TEXT,
    updated_at        TEXT,
    pushed_at         TEXT,
    default_branch    TEXT,
    size_kb           INTEGER DEFAULT 0,
    fetched_at        TEXT,
    embedded          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_repos_stars  ON repos(stargazers_count DESC);
CREATE INDEX IF NOT EXISTS idx_repos_lang   ON repos(language);
CREATE INDEX IF NOT EXISTS idx_repos_pushed ON repos(pushed_at);
CREATE INDEX IF NOT EXISTS idx_repos_unemb  ON repos(embedded) WHERE embedded = 0;

CREATE VIRTUAL TABLE IF NOT EXISTS repos_fts USING fts5(
    name, full_name, description, topics, language, owner,
    tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS crawl_state (
    shard_key  TEXT PRIMARY KEY,
    done_at    TEXT,
    item_count INTEGER
);
"""


def get_conn(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def repo_document_text(name: str, description: str, topics: list[str], language: str) -> str:
    """用于 embedding / 检索文档的拼接文本。"""
    parts = [name or ""]
    if description:
        parts.append(description)
    if topics:
        parts.append(" ".join(topics))
    if language:
        parts.append(language)
    return " ".join(p for p in parts if p)


def upsert_repo(conn: sqlite3.Connection, repo: dict[str, Any]) -> None:
    topics = repo.get("topics") or []
    lic = (repo.get("license") or {}).get("spdx_id") if isinstance(repo.get("license"), dict) else None
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        """
        INSERT INTO repos (
            id, full_name, name, owner, description, html_url, homepage, language,
            stargazers_count, forks_count, open_issues_count, topics, license,
            archived, created_at, updated_at, pushed_at, default_branch, size_kb, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            full_name=excluded.full_name, name=excluded.name, owner=excluded.owner,
            description=excluded.description, html_url=excluded.html_url,
            homepage=excluded.homepage, language=excluded.language,
            stargazers_count=excluded.stargazers_count, forks_count=excluded.forks_count,
            open_issues_count=excluded.open_issues_count, topics=excluded.topics,
            license=excluded.license, archived=excluded.archived,
            created_at=excluded.created_at, updated_at=excluded.updated_at,
            pushed_at=excluded.pushed_at, default_branch=excluded.default_branch,
            size_kb=excluded.size_kb, fetched_at=excluded.fetched_at,
            embedded=CASE WHEN excluded.description IS NOT repos.description
                              OR excluded.topics IS NOT repos.topics
                              OR excluded.name IS NOT repos.name
                              OR excluded.language IS NOT repos.language
                          THEN 0 ELSE repos.embedded END
        """,
        (
            repo["id"], repo["full_name"], repo.get("name"), repo["owner"]["login"],
            repo.get("description"), repo["html_url"], repo.get("homepage"),
            repo.get("language"), repo.get("stargazers_count", 0),
            repo.get("forks_count", 0), repo.get("open_issues_count", 0),
            json.dumps(topics, ensure_ascii=False), lic,
            1 if repo.get("archived") else 0,
            repo.get("created_at"), repo.get("updated_at"), repo.get("pushed_at"),
            repo.get("default_branch"), repo.get("size", 0), now,
        ),
    )
    # 同步 FTS（独立表，显式删除+插入，避免残留）
    conn.execute("DELETE FROM repos_fts WHERE rowid = ?", (repo["id"],))
    conn.execute(
        "INSERT INTO repos_fts(rowid, name, full_name, description, topics, language, owner) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            repo["id"], repo.get("name") or "", repo["full_name"],
            repo.get("description") or "", " ".join(topics), repo.get("language") or "",
            repo["owner"]["login"],
        ),
    )


def mark_shard_done(conn: sqlite3.Connection, shard_key: str, item_count: int) -> None:
    conn.execute(
        "INSERT INTO crawl_state(shard_key, done_at, item_count) VALUES (?,?,?) "
        "ON CONFLICT(shard_key) DO UPDATE SET done_at=excluded.done_at, item_count=excluded.item_count",
        (shard_key, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), item_count),
    )


def shard_is_fresh(conn: sqlite3.Connection, shard_key: str, max_age_hours: float = 24 * 7) -> bool:
    row = conn.execute("SELECT done_at FROM crawl_state WHERE shard_key = ?", (shard_key,)).fetchone()
    if not row or not row["done_at"]:
        return False
    try:
        from datetime import datetime, timezone
        done = datetime.strptime(row["done_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - done).total_seconds() < max_age_hours * 3600
    except ValueError:
        return False


# ---- 检索辅助 ----

def fts_search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[tuple[int, float]]:
    """FTS5 trigram 检索，返回 [(repo_id, bm25_rank)]，rank 越小越相关。"""
    clean = " ".join(query.split())
    if len(clean) < 3:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid, rank FROM repos_fts WHERE repos_fts MATCH ? ORDER BY rank LIMIT ?",
            (clean, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["rowid"], r["rank"]) for r in rows]


def list_unembedded(conn: sqlite3.Connection, limit: int | None = None) -> list[tuple[int, str]]:
    q = ("SELECT id, name, description, topics, language FROM repos "
         "WHERE embedded = 0 ORDER BY stargazers_count DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    out = []
    for r in rows:
        topics = json.loads(r["topics"]) if r["topics"] else []
        out.append((r["id"], repo_document_text(r["name"], r["description"], topics, r["language"])))
    return out


def set_embedded(conn: sqlite3.Connection, repo_id: int) -> None:
    conn.execute("UPDATE repos SET embedded = 1 WHERE id = ?", (repo_id,))


def all_embedded(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    q = ("SELECT id, name, description, topics, language FROM repos "
         "WHERE embedded = 1 ORDER BY id")
    rows = conn.execute(q).fetchall()
    out = []
    for r in rows:
        topics = json.loads(r["topics"]) if r["topics"] else []
        out.append((r["id"], repo_document_text(r["name"], r["description"], topics, r["language"])))
    return out


def get_repo(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """按 full_name 或数字 id 取仓库。"""
    if key.isdigit():
        row = conn.execute("SELECT * FROM repos WHERE id = ?", (int(key),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM repos WHERE full_name = ?", (key,)).fetchone()
    return dict(row) if row else None


def repos_by_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM repos WHERE id IN ({marks})", ids).fetchall()
    return {r["id"]: dict(r) for r in rows}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]
    embedded = conn.execute("SELECT COUNT(*) c FROM repos WHERE embedded = 1").fetchone()["c"]
    by_lang = conn.execute(
        "SELECT language, COUNT(*) c FROM repos GROUP BY language ORDER BY c DESC LIMIT 10"
    ).fetchall()
    max_star = conn.execute("SELECT MAX(stargazers_count) m FROM repos").fetchone()["m"]
    return {
        "total": total,
        "embedded": embedded,
        "max_stars": max_star,
        "top_languages": [(r["language"], r["c"]) for r in by_lang],
    }
