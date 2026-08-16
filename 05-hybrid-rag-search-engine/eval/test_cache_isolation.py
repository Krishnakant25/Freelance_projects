"""
Proves the query cache cannot leak restricted content across ACL boundaries.

WHY THIS TEST EXISTS: adding a cache to an access-controlled system is a
classic way to reintroduce a data leak that the access-control code itself
is powerless to prevent. If the cache key omitted the caller's groups, a
privileged user's answer would be served verbatim to an unprivileged one —
and the SQL ACL filter would never even run, so every existing ACL test
would still pass while the system leaked.

Run:  python eval/test_cache_isolation.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402

config.LLM_PROVIDER = "none"
config.QUERY_CACHE_ENABLED = True

from app.cache import get_cache, make_cache_key  # noqa: E402
from app.ingest import ingest_file  # noqa: E402
from app.query import answer_question  # noqa: E402

SAMPLE_DOCS = ROOT / "data" / "sample_docs"
RESTRICTED_Q = "How are executive bonuses calculated?"
SECRET_MARKER = "ebitda"

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def setup():
    db.init_db()
    ingest_file(SAMPLE_DOCS / "employee_handbook.md", acl_groups=[])
    ingest_file(SAMPLE_DOCS / "product_faq.md", acl_groups=[])
    ingest_file(SAMPLE_DOCS / "exec_compensation_policy.md", acl_groups=["management"])


def test_keys_differ_by_group():
    print("\n[keys] identical question, different groups")
    k_pub = make_cache_key(RESTRICTED_Q, ["public"])
    k_mgmt = make_cache_key(RESTRICTED_Q, ["management"])
    k_none = make_cache_key(RESTRICTED_Q, [])
    check("public != management", k_pub != k_mgmt)
    check("public != no-groups", k_pub != k_none)
    check("same groups produce same key", k_mgmt == make_cache_key(RESTRICTED_Q, ["management"]))
    check(
        "group order/duplication doesn't matter",
        make_cache_key(RESTRICTED_Q, ["b", "a", "a"]) == make_cache_key(RESTRICTED_Q, ["a", "b"]),
    )


def test_no_leak_after_privileged_query():
    print("\n[leak] privileged query first, then unprivileged — the dangerous order")
    get_cache().clear()

    mgmt = answer_question(RESTRICTED_Q, user_groups=["management"])
    check(
        "management genuinely sees restricted content",
        SECRET_MARKER in mgmt["answer"].lower(),
        "precondition failed — test proves nothing if management can't see it",
    )
    check("management result was a cache miss", mgmt.get("cached") is False)

    pub = answer_question(RESTRICTED_Q, user_groups=["public"])
    check(
        "public did NOT receive the cached privileged answer",
        SECRET_MARKER not in pub["answer"].lower(),
        f"LEAK: {pub['answer'][:120]!r}",
    )
    check("public got no citations", len(pub["citations"]) == 0)
    check("public correctly refused", pub["insufficient_evidence"] is True)


def test_cache_actually_hits():
    print("\n[hit] repeat query for the same principal is served from cache")
    get_cache().clear()
    first = answer_question("How many vacation days do employees get?", user_groups=["public"])
    second = answer_question("How many vacation days do employees get?", user_groups=["public"])
    check("first call is a miss", first.get("cached") is False)
    check("second call is a hit", second.get("cached") is True)
    check("answers are identical", first["answer"] == second["answer"])
    check(
        "cache hit is faster",
        second["elapsed_ms"] <= first["elapsed_ms"],
        f"{first['elapsed_ms']}ms -> {second['elapsed_ms']}ms",
    )
    print(f"        {first['elapsed_ms']}ms (miss) -> {second['elapsed_ms']}ms (hit)")


def test_ingest_clears_cache():
    """A cached 'not found' must not survive ingestion of a document that
    answers it — otherwise newly-added content is invisible until the TTL
    expires.

    UPDATED, not the original assumption: an earlier version of this test
    asserted that re-ingesting an UNCHANGED file preserves the cache, since
    nothing in the index changed. That was true for chunk content but wrong
    for ACL: db.upsert_document can update a document's ACL groups even when
    its content hash is unchanged (re-ingesting the same file under a
    different --groups value), and that IS a case where cached answers
    become wrong — a cached "not found" for a newly-restricted document, or
    a cached answer for a document that's now MORE restricted, would leak.
    Found for real: the exec-compensation sample document ended up
    world-readable in this project's own demo DB because an ACL update on
    unchanged content wasn't being propagated (see eval/test_acl_update.py).
    The fix was to invalidate on every ingest call unconditionally, trading
    a small amount of cache warmth for not having this class of bug at all.
    """
    import tempfile

    print("\n[invalidate] ingest clears the cache unconditionally (see docstring)")
    get_cache().clear()

    novel_q = "What is the company policy on office plant maintenance?"
    before = answer_question(novel_q, user_groups=["public"])
    check("novel question initially refused", before["insufficient_evidence"] is True)
    check("cache has entries", get_cache().stats()["entries"] > 0)

    # Ingest, unchanged content AND unchanged ACL: still clears, by design —
    # see the docstring above for why this trades cache warmth for correctness.
    ingest_file(SAMPLE_DOCS / "employee_handbook.md", acl_groups=[])
    check(
        "cache cleared even on a true no-op ingest (unconditional invalidation)",
        get_cache().stats()["entries"] == 0,
    )
    answer_question(novel_q, user_groups=["public"])  # repopulate for the next check

    # Real ingest: new content must invalidate.
    tmp = Path(tempfile.mkdtemp()) / "office_policy.md"
    tmp.write_text(
        "# Office Policy\n\n## Plant Maintenance\n\n"
        "Office plants are watered every Tuesday by the facilities team. "
        "Employees should not water plants themselves.\n",
        encoding="utf-8",
    )
    try:
        ingest_file(tmp, acl_groups=[])
        check("cache cleared after new document ingested", get_cache().stats()["entries"] == 0)

        after = answer_question(novel_q, user_groups=["public"])
        check(
            "previously-refused question now answered",
            after["insufficient_evidence"] is False,
            f"still refusing: {after['answer'][:80]!r}",
        )
        check("answer comes from the new document", "tuesday" in after["answer"].lower())
    finally:
        from app.ingest import remove_file

        remove_file(tmp)
        tmp.unlink(missing_ok=True)


def main():
    print("=" * 70)
    print("Query cache ACL-isolation tests")
    print("=" * 70)
    setup()
    test_keys_differ_by_group()
    test_no_leak_after_privileged_query()
    test_cache_actually_hits()
    test_ingest_clears_cache()

    print("\n" + "=" * 70)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Cache cannot leak across ACL boundaries.")


if __name__ == "__main__":
    main()
