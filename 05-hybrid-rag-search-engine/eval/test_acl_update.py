"""
Proves re-ingesting an unchanged document with DIFFERENT ACL groups actually
updates access control, instead of silently keeping the old (wrong) one.

WHY THIS TEST EXISTS: found by hand, not by a test — db.upsert_document only
compared content_hash, so re-ingesting the same file under a different ACL
(e.g. public -> management) was treated as a no-op and the ACL update was
dropped. The exec-compensation sample document was actually caught sitting
world-readable in this project's own demo database because of it. This is
the highest-severity class of bug in the whole project: silent, and a data
breach rather than a quality regression.

Run:  python eval/test_acl_update.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402

config.LLM_PROVIDER = "none"

from app.cache import get_cache  # noqa: E402
from app.ingest import ingest_file  # noqa: E402
from app.query import answer_question  # noqa: E402
from app.vector_index import get_index  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def main():
    print("=" * 74)
    print("ACL update on re-ingest (unchanged content, different groups)")
    print("=" * 74)

    db.init_db()
    tmp_dir = Path(tempfile.mkdtemp())
    doc = tmp_dir / "secret_memo.md"
    doc.write_text(
        "# Confidential Memo\n\n"
        "The quarterly layoff shortlist includes candidates from the finance "
        "and operations divisions.\n",
        encoding="utf-8",
    )

    print("\n[1] Ingest as PUBLIC first (simulates an operator mistake)")
    r1 = ingest_file(doc, acl_groups=["public"])
    check("first ingest succeeded", r1["status"] == "ingested", str(r1))

    result = answer_question("Which divisions are on the layoff shortlist?", user_groups=["public"])
    check(
        "public can see it (confirms the mistake happened)",
        not result["insufficient_evidence"] and "finance" in result["answer"].lower(),
        f"got: {result['answer'][:100]!r}",
    )

    print("\n[2] Re-ingest the SAME unchanged file as MANAGEMENT-only (the fix)")
    r2 = ingest_file(doc, acl_groups=["management"])
    check(
        "re-ingest reports unchanged (content is identical, correctly not re-chunked)",
        r2["status"] == "unchanged",
        str(r2),
    )

    with db.session() as conn:
        row = conn.execute(
            "SELECT acl_groups FROM documents WHERE source_path = ?", (str(doc),)
        ).fetchone()
    check(
        "documents.acl_groups actually updated to management",
        row["acl_groups"] == ",management,",
        f"got: {row['acl_groups']!r}",
    )

    with db.session() as conn:
        chunk_acls = [
            r["acl_groups"]
            for r in conn.execute(
                """SELECT chunks.acl_groups FROM chunks
                   JOIN documents ON documents.id = chunks.document_id
                   WHERE documents.source_path = ?""",
                (str(doc),),
            )
        ]
    check(
        "every existing chunk's ACL was updated too (not just the document row)",
        chunk_acls and all(a == ",management," for a in chunk_acls),
        f"got: {chunk_acls}",
    )

    print("\n[3] THE ACTUAL BUG: query again as public — must now be blocked")
    result_after = answer_question(
        "Which divisions are on the layoff shortlist?", user_groups=["public"]
    )
    check(
        "public is now correctly BLOCKED",
        result_after["insufficient_evidence"] and not result_after["citations"],
        f"LEAK: {result_after['answer'][:150]!r}",
    )

    print("\n[4] Management can still see it")
    result_mgmt = answer_question(
        "Which divisions are on the layoff shortlist?", user_groups=["management"]
    )
    check(
        "management can see it",
        not result_mgmt["insufficient_evidence"] and "finance" in result_mgmt["answer"].lower(),
        f"got: {result_mgmt['answer'][:100]!r}",
    )

    from app.ingest import remove_file

    remove_file(doc)
    doc.unlink(missing_ok=True)

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("ACL updates on re-ingest are applied and enforced correctly.")


if __name__ == "__main__":
    main()
