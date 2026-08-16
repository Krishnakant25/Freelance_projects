"""
SQLite storage layer.

Design notes (see ../../05_Hybrid_RAG_Search_Engine.md §6 for the rationale):
- One database for both dense (vector) and lexical (FTS5/BM25) search, instead
  of running two separate services (Qdrant + Meilisearch) for a demo-scale corpus.
- ACL is enforced as a SQL WHERE-clause pre-filter, never as a post-filter on
  already-retrieved results.
- Documents are content-hashed so re-ingesting an unchanged file is a no-op,
  and chunks belonging to a deleted/changed document are hard-deleted rather
  than left orphaned.
"""
import json
import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    acl_groups TEXT NOT NULL DEFAULT '',  -- delimited ',group1,group2,' ; '' = public
    version INTEGER NOT NULL DEFAULT 1,
    effective_date TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    section TEXT,
    text TEXT NOT NULL,              -- includes the prepended contextual header
    raw_text TEXT NOT NULL,          -- without the header, for citation display
    acl_groups TEXT NOT NULL DEFAULT '',  -- denormalized from documents for fast filtering
    embedding BLOB,                  -- float32 vector, packed with struct
    embedding_dim INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);

-- FTS5 external-content table for BM25 keyword search over chunk text.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep FTS in sync with the chunks table.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def pack_embedding(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def unpack_embedding(blob: bytes, dim: int):
    return struct.unpack(f"{dim}f", blob)


def acl_token(groups: Optional[Iterable[str]]) -> str:
    """',group1,group2,' — empty string means public (visible to everyone)."""
    groups = sorted({g.strip() for g in (groups or []) if g and g.strip()})
    if not groups:
        return ""
    return "," + ",".join(groups) + ","


def acl_where_clause(user_groups: Optional[Iterable[str]], column: str = "acl_groups"):
    """
    Returns (sql_fragment, params) that restricts rows to ones the caller may see:
    a chunk is visible if it's public (acl_groups = '') OR its acl token contains
    at least one of the user's groups.

    This is evaluated as a WHERE-clause pre-filter inside the retrieval query
    itself, not applied to results after they've already been ranked/returned.
    """
    user_groups = sorted({g.strip() for g in (user_groups or []) if g and g.strip()})
    if not user_groups:
        return f"{column} = ''", []
    parts = [f"{column} = ''"]
    params = []
    for g in user_groups:
        parts.append(f"{column} LIKE ?")
        params.append(f"%,{g},%")
    return "(" + " OR ".join(parts) + ")", params


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_document_by_path(conn: sqlite3.Connection, source_path: str):
    return conn.execute(
        "SELECT * FROM documents WHERE source_path = ?", (source_path,)
    ).fetchone()


def upsert_document(
    conn: sqlite3.Connection,
    source_path: str,
    title: str,
    content_hash: str,
    acl_groups: Iterable[str],
    effective_date: Optional[str] = None,
) -> tuple[int, bool]:
    """
    Returns (document_id, changed). changed=True means the content was
    (re)chunked and (re)embedded — the caller must run the full pipeline.

    ACL changes are handled independently of content changes, and this is
    security-critical, not a nicety: the original version of this function
    only compared content_hash, and if a document's text was unchanged but
    its ACL groups changed (e.g. a document re-ingested as `management`
    after previously being ingested as `public`), the update was silently
    dropped — the document stayed visible to everyone under the old, wrong
    ACL. Found during manual testing: an executive-compensation document
    intended to be management-only was left world-readable because it was
    ingested twice with different groups and the second call was a content-
    hash no-op. ACL is now always synced, on both `documents` and the
    denormalized `chunks.acl_groups` column, regardless of whether the
    content itself changed.
    """
    existing = get_document_by_path(conn, source_path)
    acl = acl_token(acl_groups)

    if existing is not None:
        content_changed = existing["content_hash"] != content_hash
        acl_changed = existing["acl_groups"] != acl

        if not content_changed and not acl_changed:
            return existing["id"], False

        if content_changed:
            conn.execute(
                """UPDATE documents
                   SET title = ?, content_hash = ?, acl_groups = ?, version = version + 1,
                       effective_date = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (title, content_hash, acl, effective_date, existing["id"]),
            )
            # Source changed -> drop its old chunks so nothing orphaned survives.
            # The caller rebuilds them from scratch with the current ACL.
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (existing["id"],))
            return existing["id"], True

        # ACL-only change: no re-chunk/re-embed needed, but the ACL must be
        # propagated to every existing chunk, not just the document row —
        # retrieval filters on chunks.acl_groups, not documents.acl_groups.
        conn.execute(
            "UPDATE documents SET acl_groups = ?, updated_at = datetime('now') WHERE id = ?",
            (acl, existing["id"]),
        )
        conn.execute(
            "UPDATE chunks SET acl_groups = ? WHERE document_id = ?",
            (acl, existing["id"]),
        )
        return existing["id"], False

    cur = conn.execute(
        """INSERT INTO documents (source_path, title, content_hash, acl_groups, effective_date)
           VALUES (?, ?, ?, ?, ?)""",
        (source_path, title, content_hash, acl, effective_date),
    )
    return cur.lastrowid, True


def delete_document(conn: sqlite3.Connection, source_path: str) -> bool:
    row = get_document_by_path(conn, source_path)
    if row is None:
        return False
    conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))  # cascades to chunks
    return True


def insert_chunk(
    conn: sqlite3.Connection,
    document_id: int,
    chunk_index: int,
    section: Optional[str],
    text: str,
    raw_text: str,
    acl_groups: str,
    embedding,
) -> int:
    blob = pack_embedding(embedding) if embedding is not None else None
    dim = len(embedding) if embedding is not None else None
    cur = conn.execute(
        """INSERT INTO chunks
           (document_id, chunk_index, section, text, raw_text, acl_groups, embedding, embedding_dim)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, chunk_index, section, text, raw_text, acl_groups, blob, dim),
    )
    return cur.lastrowid


def prune_orphan_chunks(conn: sqlite3.Connection) -> int:
    """Defensive cleanup: chunks whose document no longer exists."""
    cur = conn.execute(
        "DELETE FROM chunks WHERE document_id NOT IN (SELECT id FROM documents)"
    )
    return cur.rowcount
