"""Command-line interface for local operation and testing.

NOTE ON --groups: the CLI runs with direct local database access, so it is
inherently a trusted/admin tool — `--groups` here is "act as a caller in
these groups", equivalent to root. This is fine for a local operator but is
NOT the model the HTTP API uses: over the API, groups come from the API key
server-side and can never be supplied by the caller (see app/auth.py).
Do not expose this CLI over a network boundary.

Usage:
    python -m app.cli ingest data/sample_docs --groups public
    python -m app.cli ingest handbook.pdf --groups hr,management
    python -m app.cli query "How many vacation days do employees get?" --groups public
    python -m app.cli stats
"""
import argparse
import json
import sys
from pathlib import Path

from . import db
from .ingest import ingest_directory, ingest_file
from .query import answer_question

# Windows consoles default to cp1252, which raises UnicodeEncodeError on
# characters that appear routinely in PDF-extracted text (bullets, dashes,
# smart quotes). Printing a retrieved chunk would crash the CLI outright.
# Reconfigure to UTF-8 with replacement so display can never kill the process.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable stream (piped/redirected)
        pass


def _parse_groups(value: str) -> list[str]:
    if not value:
        return []
    return [g.strip() for g in value.split(",") if g.strip()]


def main():
    parser = argparse.ArgumentParser(description="Hybrid RAG Search Engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest a file or directory")
    p_ingest.add_argument("path", help="File or directory path")
    p_ingest.add_argument("--groups", default="", help="Comma-separated ACL groups (empty = public)")

    p_query = sub.add_parser("query", help="Ask a question")
    p_query.add_argument("question")
    p_query.add_argument("--groups", default="", help="Comma-separated groups the asking user belongs to")
    p_query.add_argument("--json", action="store_true", help="Print raw JSON instead of formatted output")

    sub.add_parser("stats", help="Show index statistics")

    args = parser.parse_args()
    db.init_db()

    if args.command == "stats":
        with db.session() as conn:
            docs = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
            chunks = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
            rows = conn.execute(
                """SELECT documents.title, documents.acl_groups, COUNT(chunks.id) AS n
                   FROM documents LEFT JOIN chunks ON chunks.document_id = documents.id
                   GROUP BY documents.id ORDER BY documents.title"""
            ).fetchall()
        print(f"\nDocuments: {docs}   Chunks: {chunks}")
        print(f"\n{'TITLE':40s} {'ACL':25s} {'CHUNKS':>7s}")
        print("-" * 76)
        for r in rows:
            acl = r["acl_groups"].strip(",") or "(public)"
            print(f"{r['title'][:40]:40s} {acl[:25]:25s} {r['n']:>7d}")
        print()
        return

    if args.command == "ingest":
        target = Path(args.path)
        groups = _parse_groups(args.groups)
        if target.is_dir():
            results = ingest_directory(target, acl_groups=groups)
        else:
            results = [ingest_file(target, acl_groups=groups)]
        print(json.dumps(results, indent=2))

    elif args.command == "query":
        groups = _parse_groups(args.groups)
        result = answer_question(args.question, user_groups=groups)
        if args.json:
            print(json.dumps(result, indent=2))
            return

        print(f"\nQ: {result['query']}\n")
        print(f"A: {result['answer']}\n")
        if result["insufficient_evidence"]:
            print("[insufficient evidence in indexed documents]\n")
        print(f"Provider: {result['provider']}")
        print(f"\nCitations ({len(result['citations'])}):")
        for c in result["citations"]:
            flag = "OK" if c["verified"] else "UNVERIFIED"
            print(
                f"  [{c['chunk_id']}] {flag:10s} overlap={c['overlap_score']:.2f}  "
                f"{c['document_title']} > {c['section'] or '(no section)'}  ({c['source_path']})"
            )
        print(f"\nRetrieved chunks ({len(result['retrieved_chunks'])}):")
        for rc in result["retrieved_chunks"]:
            print(
                f"  [{rc['chunk_id']}] fused={rc['fused_score']:.4f} "
                f"vec={rc['vector_score']:.3f} kw={rc['keyword_score']:.3f} "
                f"rerank={rc['rerank_score']}"
            )
            print(f"      {rc['document_title']} > {rc['section'] or '(no section)'}")


if __name__ == "__main__":
    main()
