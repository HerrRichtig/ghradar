"""混合检索：语义向量 + 关键词 FTS，用 RRF 融合排序。"""
from __future__ import annotations

import json
from typing import Any

import numpy as np

from . import config, db
from .embed import embed_query, load_vectors

RRF_K = 60


def _semantic(ids_arr: np.ndarray, vecs: np.ndarray, qv: np.ndarray, limit: int):
    if vecs.shape[0] == 0:
        return [], []
    scores = vecs @ qv  # 已归一化 → 余弦
    k = min(limit, len(scores))
    idx = np.argpartition(scores, -k)[-k:]
    idx = idx[np.argsort(-scores[idx])]
    return ids_arr[idx].astype(int).tolist(), scores[idx].tolist()


def _fmt(r: dict[str, Any], fused: float, via_sem: bool, via_kw: bool) -> dict[str, Any]:
    topics = json.loads(r["topics"]) if r["topics"] else []
    matched = []
    if via_sem:
        matched.append("semantic")
    if via_kw:
        matched.append("keyword")
    return {
        "full_name": r["full_name"],
        "url": r["html_url"],
        "stars": r["stargazers_count"],
        "forks": r["forks_count"],
        "language": r["language"],
        "description": r["description"],
        "topics": topics,
        "license": r["license"],
        "archived": bool(r["archived"]),
        "pushed_at": r["pushed_at"],
        "matched_by": "+".join(matched),
        "score": round(fused, 4),
    }


def search(conn, query: str, top_k: int | None = None, min_stars: int = 0,
           language: str | None = None, topic: str | None = None) -> list[dict[str, Any]]:
    top_k = top_k or config.DEFAULT_TOP_K
    ids_arr, vecs = load_vectors()

    qv = embed_query(query)
    sem_ids, _ = _semantic(ids_arr, vecs, qv, config.SEMANTIC_CANDIDATES)
    sem_set = set(sem_ids)

    fts = db.fts_search(conn, query, limit=config.SEMANTIC_CANDIDATES)
    kw_set = {rid for rid, _ in fts}

    fused: dict[int, float] = {}
    for rank, rid in enumerate(sem_ids):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (rid, _) in enumerate(fts):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)

    order = sorted(fused.items(), key=lambda kv: -kv[1])
    repos = db.repos_by_ids(conn, [rid for rid, _ in order])

    results: list[dict[str, Any]] = []
    for rid, score in order:
        r = repos.get(rid)
        if not r:
            continue
        if r["stargazers_count"] < min_stars:
            continue
        if language and (r["language"] or "").lower() != language.lower():
            continue
        topics = json.loads(r["topics"]) if r["topics"] else []
        if topic and topic.lower() not in {t.lower() for t in topics}:
            continue
        results.append(_fmt(r, score, rid in sem_set, rid in kw_set))
        if len(results) >= top_k:
            break

    for i, item in enumerate(results):
        item["rank"] = i + 1
    return results


def get(full_name: str) -> dict[str, Any] | None:
    conn = db.get_conn()
    try:
        r = db.get_repo(conn, full_name)
        return r
    finally:
        conn.close()
