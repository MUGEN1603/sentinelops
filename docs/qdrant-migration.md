# Qdrant Collection Migration Guide

## Background
The Qdrant vector database stores incident embeddings for RAG-based incident analysis.
The current configuration uses the `BAAI/bge-small-en-v1.5` model which produces 384-dimensional vectors.

## Critical Constraint
**The collection vector dimension MUST match the embedding model's output dimension.**
If you change the embedding model (e.g., to a larger model like `BAAI/bge-base-en-v1.5` with 768 dimensions),
you MUST delete and recreate the collection, or all insert/search operations will fail with a dimension mismatch error.

## Migration Procedure

### 1. Backup Existing Data (if needed)
```bash
# Export existing incidents to JSON
curl -X POST http://localhost:6333/collections/incidents/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"limit": 10000, "with_payload": true, "with_vector": false}' \
  > qdrant_backup.json
```

### 2. Delete and Recreate Collection
```bash
# Delete old collection
curl -X DELETE http://localhost:6333/collections/incidents

# Recreate with new dimension (e.g., 768 for bge-base)
curl -X PUT http://localhost:6333/collections/incidents \
  -H "Content-Type: application/json" \
  -d '{"vectors": {"size": 768, "distance": "Cosine"}}'
```

### 3. Update Embedding Model
Update the model in `memory/qdrant_client.py`:
```python
# Change from:
# _embedder = TextEmbedding()  # BAAI/bge-small-en-v1.5, dim=384
# To:
_embedder = TextEmbedding(model_name="BAAI/bge-base-en-v1.5")  # dim=768
```

### 4. Update Collection Dimension Constant
Update `VECTOR_DIM` in `memory/qdrant_client.py`:
```python
VECTOR_DIM = 768  # Updated for bge-base-en-v1.5
```

### 5. Re-index Historical Incidents
```bash
# Re-embed and re-insert from backup
python -c "
import json
from memory.qdrant_client import get_client, get_embedder, store_incident

with open('qdrant_backup.json') as f:
    data = json.load(f)

for point in data['result']['points']:
    store_incident(
        incident_id=point['id'],
        text=point['payload']['text'],
        rca=point['payload']['rca'],
        outcome=point['payload']['outcome'],
        metadata=point['payload'].get('metadata', {})
    )
"
```

## Embedding Model Options

| Model | Dimensions | Speed | Quality | Use Case |
|-------|------------|-------|---------|----------|
| BAAI/bge-small-en-v1.5 | 384 | Fast | Good | Default (current) |
| BAAI/bge-base-en-v1.5 | 768 | Medium | Better | Higher accuracy |
| BAAI/bge-large-en-v1.5 | 1024 | Slow | Best | Maximum accuracy |
| sentence-transformers/all-MiniLM-L6-v2 | 384 | Fast | Good | Alternative |

## Automated Migration Script

Create a migration script for future use:

```python
# scripts/migrate_qdrant.py
import os
import json
import sys
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance

def migrate_collection(new_dim: int, new_model: str):
    """Migrate Qdrant collection to new embedding dimension."""
    client = QdrantClient(url="http://localhost:6333")
    collection = "incidents"
    
    # 1. Backup
    print("Backing up existing data...")
    points = []
    offset = None
    while True:
        result = client.scroll(collection_name="incidents", limit=1000, offset=offset, with_payload=True, with_vectors=False)
        points.extend(result[0])
        if result[1] is None:
            break
        offset = result[1]
    
    with open('qdrant_backup.json', 'w') as f:
        json.dump([{
            'id': p.id,
            'payload': p.payload
        } for p in points], f)
    print(f"Backed up {len(points)} points")
    
    # 2. Recreate collection
    print(f"Recreating collection with dim={new_dim}...")
    client.delete_collection(collection)
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=new_dim, distance=Distance.COSINE)
    )
    
    # 3. Update config
    print("Update memory/qdrant_client.py:")
    print(f"  VECTOR_DIM = {new_dim}")
    print(f"  _embedder = TextEmbedding(model_name='{new_model}')")
    
    print("\nAfter updating the code, re-run this script with --reindex to re-embed data")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dim", type=int, default=768, help="New vector dimension")
    parser.add_argument("--model", type=str, default="BAAI/bge-base-en-v1.5", help="New embedding model")
    args = parser.parse_args()
    migrate_collection(args.dim, args.model)
```

Run with:
```bash
python scripts/migrate_qdrant.py --dim 768 --model BAAI/bge-base-en-v1.5
```

## Rollback Procedure

If migration fails:
1. Restore from backup: `curl -X DELETE .../collections/incidents` then recreate with old dimension and re-insert backup data
2. Revert code changes in `memory/qdrant_client.py`
3. Restart services

## Testing After Migration

```bash
# Verify collection dimension
curl http://localhost:6333/collections/incidents

# Test store/retrieve
python -c "
from memory.qdrant_client import init_collection, store_incident, retrieve_similar
init_collection()
store_incident('test-1', 'test incident', 'test cause', 'resolved')
results = retrieve_similar('test query', k=1)
print(f'Retrieved: {results}')
assert len(results) == 1
print('Migration successful!')
"
```

## Version Compatibility Matrix

| Qdrant Version | Python Client | Embedding Models |
|----------------|---------------|------------------|
| 1.8.x          | 1.8.x         | All BGE models   |
| 1.9.x          | 1.9.x         | All BGE models   |
| 1.10.x         | 1.10.x        | All BGE models   |

Always pin Qdrant version in `requirements.txt` to avoid unexpected breaking changes.
