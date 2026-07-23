"""
memory/qdrant_client.py — Qdrant vector memory for similar-incident RAG retrieval.

Uses FastEmbed's default model: BAAI/bge-small-en-v1.5
Output dimension: 384

CRITICAL: The collection vector size MUST match the embedding model's output dimension.
If you change `embedder = TextEmbedding(model_name="...")`, you must delete the
existing collection and recreate it with the new model's dimension, or all
store_incident() calls will fail with a dimension-mismatch error.

Exit check:
    python -c "
    from memory.qdrant_client import init_collection, store_incident, retrieve_similar
    init_collection()
    store_incident('test-1', 'OOMKill in payment-service', 'memory leak in heap allocator', 'resolved')
    results = retrieve_similar('OOMKill in checkout-service')
    print(results)
    # expect: [{'id': 'test-1', 'score': ~0.95, 'rca': 'memory leak in heap allocator'}]
    "
"""

from __future__ import annotations

import os
import logging
import uuid
from typing import Optional

from qdrant_client import QdrantClient, models
from fastembed import TextEmbedding

log = logging.getLogger("qdrant-memory")

# ── Configuration ─────────────────────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION  = "incidents"
VECTOR_DIM  = 384   # BAAI/bge-small-en-v1.5 output dimension — DO NOT change without recreating collection

# ── Lazy singletons ────────────────────────────────────────────────────────────
_client: Optional[QdrantClient] = None
_embedder: Optional[TextEmbedding] = None


def get_client() -> QdrantClient:
    """Return a cached QdrantClient, creating one on first call."""
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL)
        log.info("Qdrant client connected to %s", QDRANT_URL)
    return _client


def get_embedder() -> TextEmbedding:
    """Return a cached FastEmbed TextEmbedding instance."""
    global _embedder
    if _embedder is None:
        # Downloads model on first call (~25MB); cached in ~/.cache/fastembed
        _embedder = TextEmbedding()  # model: BAAI/bge-small-en-v1.5, dim=384
        log.info("FastEmbed embedder loaded (BAAI/bge-small-en-v1.5, dim=%d)", VECTOR_DIM)
    return _embedder


# ── Collection management ──────────────────────────────────────────────────────

def init_collection() -> None:
    """
    Create the 'incidents' collection if it does not already exist.
    Safe to call on every startup — idempotent.
    """
    client = get_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=models.VectorParams(
                size=VECTOR_DIM,
                distance=models.Distance.COSINE
            )
        )
        log.info("Created Qdrant collection '%s' (dim=%d, distance=COSINE)", COLLECTION, VECTOR_DIM)
    else:
        log.info("Qdrant collection '%s' already exists — skipping creation", COLLECTION)


def recreate_collection() -> None:
    """
    Delete and recreate the collection. Use ONLY when changing embedding models.
    WARNING: This permanently deletes all stored incident embeddings.
    """
    client = get_client()
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
        log.warning("Deleted collection '%s' for recreation", COLLECTION)
    init_collection()


# ── Write: store a resolved incident ──────────────────────────────────────────

def store_incident(
    incident_id: str,
    text: str,
    rca: str,
    outcome: str,
    metadata: Optional[dict] = None
) -> None:
    """
    Embed and store an incident in Qdrant for future RAG retrieval.

    Args:
        incident_id: Unique string ID for this incident. Used as the Qdrant point ID.
                     Must be a valid UUID string or an integer.
        text:        The text to embed — combine workload name + alert labels + log excerpt.
        rca:         Root cause analysis text produced by the diagnosis agent.
        outcome:     Resolution outcome: "resolved" | "escalated" | "false-positive".
        metadata:    Optional extra fields stored in the payload (e.g., namespace, severity).
    """
    init_collection()
    embedder = get_embedder()
    client   = get_client()

    vector = list(embedder.embed([text]))[0].tolist()

    payload = {
        "text": text,
        "rca": rca,
        "outcome": outcome,
        **(metadata or {})
    }

    # Qdrant point IDs must be UUID or unsigned integer
    try:
        point_id = str(uuid.UUID(incident_id))  # validate UUID format
    except ValueError:
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, incident_id))

    client.upsert(
        collection_name=COLLECTION,
        points=[
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload
            )
        ]
    )
    log.info("Stored incident id=%s outcome=%s", point_id, outcome)


# ── Read: retrieve similar past incidents ─────────────────────────────────────

def retrieve_similar(query_text: str, k: int = 3) -> list[dict]:
    """
    Find the k most similar past incidents using cosine similarity.

    Args:
        query_text: Natural language description of the current incident.
        k:          Number of similar incidents to return.

    Returns:
        List of dicts: [{"id": str, "score": float, "rca": str, "outcome": str}]
        Returns empty list if collection is empty or Qdrant is unreachable.
    """
    init_collection()
    embedder = get_embedder()
    client   = get_client()

    try:
        vector  = list(embedder.embed([query_text]))[0].tolist()
        results = client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=k
        ).points

        similar = [
            {
                "id":      str(r.id),
                "score":   round(r.score, 4),
                "rca":     r.payload.get("rca", ""),
                "outcome": r.payload.get("outcome", ""),
                "text":    r.payload.get("text", ""),
            }
            for r in results
        ]
        log.info("Retrieved %d similar incidents for query (top score=%.4f)",
                 len(similar), similar[0]["score"] if similar else 0.0)
        return similar

    except Exception as exc:
        log.error("Qdrant retrieval failed: %s", exc)
        return []
