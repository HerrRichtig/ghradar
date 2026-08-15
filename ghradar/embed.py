"""本地多语言向量 embedding（fastembed + onnxruntime，无 API 成本）。

向量以 float32 归一化后存成两个对齐的 .npy：
  repo_ids.npy (N,) int64、vectors.npy (N, dim) float32，
归一化后点积即余弦相似度。embed_new 只处理 embedded=0 的仓库，
支持「新增追加、变更原地覆盖」的增量更新。
"""
from __future__ import annotations

import numpy as np
from fastembed import TextEmbedding

from . import config, db

_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=config.EMBED_MODEL, cache_dir=str(config.MODELS_DIR))
    return _model


def _encode(texts: list[str], batch_size: int) -> np.ndarray:
    model = get_model()
    out: list[np.ndarray] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        out.extend(np.asarray(v, dtype=np.float32) for v in model.embed(batch))
    return np.stack(out) if out else np.empty((0, config.EMBED_DIM), dtype=np.float32)


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n) if n > 0 else v


def embed_query(text: str) -> np.ndarray:
    v = _encode([text], 1)[0]
    return _normalize(v)


def load_vectors() -> tuple[np.ndarray, np.ndarray]:
    if config.IDS_PATH.exists() and config.VECTORS_PATH.exists():
        return np.load(config.IDS_PATH), np.load(config.VECTORS_PATH)
    return (np.array([], dtype=np.int64),
            np.empty((0, config.EMBED_DIM), dtype=np.float32))


def embed_new(conn, batch_size: int | None = None) -> int:
    """为所有 embedded=0 的仓库生成向量并写盘，返回处理条数。"""
    config.ensure_dirs()
    batch_size = batch_size or config.EMBED_BATCH
    pending = db.list_unembedded(conn)
    if not pending:
        return 0
    ids_arr, vecs = load_vectors()
    if ids_arr.shape[0] != vecs.shape[0]:
        raise RuntimeError("repo_ids.npy 与 vectors.npy 行数不一致，请检查数据目录")
    if vecs.shape[0] and vecs.shape[1] != config.EMBED_DIM:
        raise RuntimeError(
            f"已存向量维度 {vecs.shape[1]} 与配置 {config.EMBED_DIM} 不符，"
            "可能是换了模型，请删除 data/vectors.npy 与 data/repo_ids.npy 后重跑"
        )
    row_of = {int(i): r for r, i in enumerate(ids_arr)}
    ids = [i for i, _ in pending]
    texts = [t for _, t in pending]
    embs = _encode(texts, batch_size)
    new_ids: list[int] = []
    new_vecs: list[np.ndarray] = []
    for rid, v in zip(ids, embs):
        v = _normalize(v)
        if rid in row_of:
            vecs[row_of[rid]] = v
        else:
            new_ids.append(rid)
            new_vecs.append(v)
        db.set_embedded(conn, rid)
    if new_ids:
        ids_arr = np.concatenate([ids_arr, np.asarray(new_ids, dtype=np.int64)])
        vecs = np.concatenate([vecs, np.stack(new_vecs)])
    np.save(config.IDS_PATH, ids_arr)
    np.save(config.VECTORS_PATH, vecs)
    conn.commit()
    return len(pending)
