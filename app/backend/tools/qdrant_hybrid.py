"""
Qdrant hybrid retrieval：Dense (OpenAI cosine) + Sparse BM25，以 RRF 融合。
需 collection 具 named vector「dense」與 sparse「text」，且 sparse 使用 IDF modifier（對齊 FastEmbed BM25）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "text"

_sparse_model = None


def _bm25():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding

        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model


def embed_sparse_query(text: str) -> models.SparseVector:
    """查詢語句 → BM25 sparse vector（須與 index 時 passage 成對使用）。"""
    emb = next(_bm25().query_embed([text]))
    return models.SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist())


def _prefetch_limit(top_k: int, group_size: int) -> int:
    return max(48, top_k * max(group_size, 1) * 6)


async def hybrid_rrf_grouped(
    client: AsyncQdrantClient,
    collection_name: str,
    query_dense: List[float],
    query_sparse: models.SparseVector,
    query_filter: Optional[models.Filter],
    group_by_payload_key: str,
    group_size: int,
    top_k: int,
    score_threshold: Optional[float],
) -> List[Tuple[Any, List[Any]]]:
    """
    RRF 融合後依 payload 鍵分組；每组保留分數最高的 group_size 個點（dedupe by point id）。
    回傳 [(group_id, [ScoredPoint, ...]), ...]，已依組內最佳分數排序並截斷為 top_k 組。
    """
    plimit = _prefetch_limit(top_k, group_size)
    use_sparse = bool(query_sparse.indices)

    if use_sparse:
        res = await client.query_points(
            collection_name=collection_name,
            prefetch=[
                models.Prefetch(
                    query=query_dense,
                    using=DENSE_VECTOR_NAME,
                    limit=plimit,
                    filter=query_filter,
                ),
                models.Prefetch(
                    query=query_sparse,
                    using=SPARSE_VECTOR_NAME,
                    limit=plimit,
                    filter=query_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=plimit,
            with_payload=True,
        )
    else:
        res = await client.query_points(
            collection_name=collection_name,
            query=query_dense,
            using=DENSE_VECTOR_NAME,
            query_filter=query_filter,
            limit=plimit,
            with_payload=True,
        )

    points = res.points
    buckets: Dict[Any, List[Any]] = defaultdict(list)
    for p in points:
        pl = p.payload or {}
        gid = pl.get(group_by_payload_key)
        if gid is None:
            continue
        buckets[gid].append(p)

    ranked: List[Tuple[Any, float, List[Any]]] = []
    for gid, hits in buckets.items():
        by_score = sorted(hits, key=lambda x: x.score, reverse=True)
        seen: set = set()
        uniq = []
        for h in by_score:
            if h.id in seen:
                continue
            seen.add(h.id)
            uniq.append(h)
            if len(uniq) >= group_size:
                break
        if not uniq:
            continue
        best = uniq[0].score
        if score_threshold is not None and best < score_threshold:
            continue
        ranked.append((gid, best, uniq))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [(g, hits) for g, _, hits in ranked[:top_k]]


async def hybrid_rrf_flat(
    client: AsyncQdrantClient,
    collection_name: str,
    query_dense: List[float],
    query_sparse: models.SparseVector,
    query_filter: Optional[models.Filter],
    limit: int,
) -> List[Any]:
    """RRF 融合，回傳 ScoredPoint 列表（無 group_by）。"""
    plimit = max(limit * 4, 32)
    use_sparse = bool(query_sparse.indices)

    if use_sparse:
        res = await client.query_points(
            collection_name=collection_name,
            prefetch=[
                models.Prefetch(
                    query=query_dense,
                    using=DENSE_VECTOR_NAME,
                    limit=plimit,
                    filter=query_filter,
                ),
                models.Prefetch(
                    query=query_sparse,
                    using=SPARSE_VECTOR_NAME,
                    limit=plimit,
                    filter=query_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
    else:
        res = await client.query_points(
            collection_name=collection_name,
            query=query_dense,
            using=DENSE_VECTOR_NAME,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    return list(res.points)
