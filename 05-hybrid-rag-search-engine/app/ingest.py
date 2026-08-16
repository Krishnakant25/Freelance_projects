"""
Ingestion pipeline: parse -> chunk -> embed -> upsert.

Idempotent by content hash (architecture doc §6.6): re-ingesting an unchanged
file is a no-op. A changed file drops its old chunks and rebuilds them.
Deleting a document cascades to its chunks, so nothing is orphaned.

Parse-quality warnings are propagated into the result rather than swallowed —
a silently badly-parsed PDF is the single most common way a RAG system ends
up confidently retrieving garbage.
"""
import hashlib
import logging
from pathlib import Path
from typing import Iterable, Optional

from . import config, db
from .chunking import chunk_document
from .embeddings import embed_texts
from .cache import get_cache
from .parsing import SUPPORTED_EXTENSIONS, ParsedDocument, parse_file
from .vector_index import get_index

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Hash covers the document text AND the processing pipeline.

    Hashing text alone means a change to parsing/chunking/embedding leaves
    every existing document reported as "unchanged", so the index silently
    keeps chunks built by superseded logic. Including the pipeline version
    and embedding model makes such a change invalidate affected documents
    automatically on the next ingest.
    """
    fingerprint = f"{config.PIPELINE_VERSION}|{config.EMBEDDING_MODEL}|{text}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def ingest_file(
    path: Path,
    acl_groups: Optional[Iterable[str]] = None,
    title: Optional[str] = None,
    effective_date: Optional[str] = None,
) -> dict:
    path = Path(path)
    parsed: ParsedDocument = parse_file(path)

    if parsed.warnings:
        for w in parsed.warnings:
            logger.warning("Parse warning for %s: %s", path.name, w)

    if not parsed.text.strip():
        return {
            "path": str(path),
            "status": "skipped_empty",
            "warnings": parsed.warnings,
        }

    content_hash = _content_hash(parsed.text)
    title = (
        title
        or (parsed.metadata.get("title") or "").strip()
        or path.stem.replace("_", " ").replace("-", " ").title()
    )
    acl_token = db.acl_token(acl_groups)

    with db.session() as conn:
        doc_id, changed = db.upsert_document(
            conn,
            source_path=str(path),
            title=title,
            content_hash=content_hash,
            acl_groups=acl_groups or [],
            effective_date=effective_date,
        )
        if not changed:
            # "changed=False" can still mean the ACL was updated in place
            # (db.upsert_document syncs ACL independently of content — see
            # the comment there). Invalidate unconditionally rather than
            # trying to distinguish "truly nothing happened" from "ACL
            # changed but chunks didn't" — a stale in-memory index/cache
            # serving an old ACL is a security bug, and invalidation is
            # cheap (lazy rebuild on next query), so there's no reason to
            # skip it on this path.
            get_index().invalidate()
            get_cache().clear()
            return {
                "path": str(path),
                "status": "unchanged",
                "document_id": doc_id,
                "warnings": parsed.warnings,
            }

        chunks = chunk_document(title, parsed.text)
        if not chunks:
            return {
                "path": str(path),
                "status": "skipped_no_chunks",
                "document_id": doc_id,
                "warnings": parsed.warnings,
            }

        # Batch embedding — one model call per batch instead of one per chunk.
        texts = [c.text for c in chunks]
        vectors = []
        for i in range(0, len(texts), config.EMBED_BATCH_SIZE):
            vectors.extend(embed_texts(texts[i : i + config.EMBED_BATCH_SIZE]))

        for chunk, vec in zip(chunks, vectors):
            db.insert_chunk(
                conn,
                document_id=doc_id,
                chunk_index=chunk.index,
                section=chunk.section,
                text=chunk.text,
                raw_text=chunk.raw_text,
                acl_groups=acl_token,
                embedding=vec,
            )

    # New vectors exist — drop the cached matrix/index so queries see them.
    # The query cache must go too: a previously-cached "not found" would
    # otherwise keep being served for a question this document now answers.
    get_index().invalidate()
    get_cache().clear()

    return {
        "path": str(path),
        "status": "ingested",
        "document_id": doc_id,
        "chunks": len(chunks),
        "pages": parsed.page_count or None,
        "warnings": parsed.warnings,
    }


def ingest_directory(
    directory: Path,
    acl_groups: Optional[Iterable[str]] = None,
) -> list[dict]:
    directory = Path(directory)
    results = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            try:
                results.append(ingest_file(path, acl_groups=acl_groups))
            except Exception as e:  # noqa: BLE001 - one bad file shouldn't abort the batch
                logger.exception("Failed to ingest %s", path)
                results.append({"path": str(path), "status": "error", "error": str(e)})
    return results


def remove_file(path: Path) -> bool:
    with db.session() as conn:
        removed = db.delete_document(conn, str(Path(path)))
    if removed:
        get_index().invalidate()
        # Critical on delete: a cached answer could still be serving content
        # from a document that has just been removed.
        get_cache().clear()
    return removed
