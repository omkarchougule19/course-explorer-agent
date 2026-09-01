"""
embeddings.py

Generates and stores text embeddings for the RAG layer using a self-hosted,
open-source model - BAAI/bge-small-en-v1.5 (384-dim, ~130MB, MIT licensed) -
via fastembed (ONNX runtime, no torch/GPU needed). No API key, no external
service, no rate limit: the model runs identically wherever this code runs,
which matters because embedding vectors are only comparable to each other
when produced by the exact same model.

Groq was the original plan for embeddings (nomic-embed-text-v1_5), but
turned out to have no embeddings API at all - confirmed both by a live 404
and Groq's own docs. This replaces that entirely. See DECISIONS.md for the
full reasoning, including the measured memory footprint (~130MB for the
model on top of the rest of the app - well within Render's free 512MB tier).

Vector storage (course_embeddings table) is Postgres-only, via the pgvector
extension - SQLite has no vector type, so every function here that touches
the database is a no-op on the local SQLite fallback. Embedding *generation*
(embed_text/embed_texts) works regardless of backend; only persistence and
search are Postgres-gated.
"""

from pathlib import Path
from typing import List, Optional

from app import db

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# fastembed's own default cache directory is a temp folder, which isn't a safe
# place to rely on surviving between Render's build step and the running
# service (see DECISIONS.md - the cold-start fix bakes the model in at build
# time, which only works if build and runtime agree on where it lives). Pin
# it to a fixed path inside the project instead, used identically by every
# caller: render.yaml's build step, this module's own warmup(), and local dev.
MODEL_CACHE_DIR = Path(__file__).parent.parent / "model_cache"

_model = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))
    return _model


def warmup() -> None:
    """Force the model to load (and run one throwaway embed) right now,
    instead of lazily on first use. Call this at app startup so the local
    model load happens while the server is already booting, not stacked onto
    a user's first request."""
    _get_model()
    embed_text("warmup")


def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:
    """Embed a batch of texts in one call (fastembed batches internally, which
    is meaningfully faster than embedding one at a time). Empty/whitespace-only
    strings are skipped and come back as None in the same position - there's
    nothing meaningful to embed for them."""
    indices_to_embed = [i for i, t in enumerate(texts) if t and t.strip()]
    result: List[Optional[List[float]]] = [None] * len(texts)
    if not indices_to_embed:
        return result
    model = _get_model()
    to_embed = [texts[i] for i in indices_to_embed]
    vectors = list(model.embed(to_embed))
    for idx, vec in zip(indices_to_embed, vectors):
        result[idx] = vec.tolist()
    return result


def embed_text(text: str) -> Optional[List[float]]:
    """Embed a single piece of text (e.g. a user's question). None for
    empty/whitespace-only input."""
    return embed_texts([text])[0]


def init_course_embeddings_table(conn: db.Connection) -> None:
    """Create the course_embeddings table + pgvector extension + similarity
    index. No-op on SQLite (no vector type there) - vector search is
    Postgres-only by design, see DECISIONS.md."""
    if conn.backend != "postgres":
        return
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS course_embeddings (
            subject TEXT NOT NULL,
            course_number TEXT NOT NULL,
            description TEXT NOT NULL,
            embedding VECTOR({EMBEDDING_DIM}),
            updated_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (subject, course_number)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_course_embeddings_vec "
        "ON course_embeddings USING hnsw (embedding vector_cosine_ops)"
    )
    conn.commit()


def save_course_embedding(conn: db.Connection, subject: str, course_number: str,
                           description: str) -> bool:
    """Embed one course's description and upsert it. Returns False (no-op) on
    SQLite, or if there's no description to embed."""
    if conn.backend != "postgres" or not description or not description.strip():
        return False

    vector = embed_text(description)
    if vector is None:
        return False

    from pgvector import Vector
    from pgvector.psycopg2 import register_vector
    register_vector(conn._raw)

    conn.execute(
        """
        INSERT INTO course_embeddings (subject, course_number, description, embedding, updated_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (subject, course_number) DO UPDATE SET
            description = EXCLUDED.description,
            embedding = EXCLUDED.embedding,
            updated_at = EXCLUDED.updated_at
        """,
        (subject, course_number, description, Vector(vector)),
    )
    conn.commit()
    return True


def search_similar_by_vector(conn: db.Connection, vector: Optional[List[float]], k: int = 5) -> List[dict]:
    """Cosine-nearest course descriptions to an already-computed embedding.
    Lets a multi-query search embed all of its sub-queries in one batch
    (embed_texts) and reuse the vectors here instead of re-embedding per
    query. Empty list on SQLite or if `vector` is missing."""
    if conn.backend != "postgres" or not vector:
        return []

    from pgvector import Vector
    from pgvector.psycopg2 import register_vector
    register_vector(conn._raw)

    rows = conn.execute(
        """
        SELECT subject, course_number, description, embedding <=> ? AS distance
        FROM course_embeddings
        ORDER BY distance
        LIMIT ?
        """,
        (Vector(vector), k),
    ).fetchall()
    return [dict(r) for r in rows]


def search_similar_courses(conn: db.Connection, query: str, k: int = 5) -> List[dict]:
    """Cosine-nearest course descriptions to `query`. Empty list on SQLite (no
    course_embeddings table there) or if the query has nothing to embed."""
    if conn.backend != "postgres":
        return []
    return search_similar_by_vector(conn, embed_text(query), k)
