"""
Regression tests for the production-hardening pass.

Every test here corresponds to a real defect found by auditing the code, not
a hypothetical. Each one would have passed silently before the fix, which is
why they're worth keeping: they cover the class of bug that doesn't announce
itself.

Run:  python eval/test_production_hardening.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

from app import config, db, kb  # noqa: E402
from app.rate_limit import RateLimiter  # noqa: E402

ROOT_DIR = _harness.ROOT

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


# --- 1. Test isolation ------------------------------------------------------


def test_tests_never_touch_real_db():
    """The original failure: eval/run_eval.py called config.DB_PATH.unlink(),
    which deleted the operator's real database on every test run."""
    print("\n[isolation] the test suite must not point at the real database")
    real = _harness.real_db_path()
    active = config.DB_PATH
    check("active DB path differs from the real one", active != real, f"both are {active}")
    check(
        "active DB path is in a temp directory",
        "helpdesk_test_" in str(active),
        str(active),
    )


# --- 2. KB cache invalidation ----------------------------------------------


def test_kb_cache_invalidates_on_ingest():
    """The original failure: ingest_kb_article() didn't invalidate the search
    cache, so a newly-added article was invisible to deflection search until
    the process restarted — the KB "had" the answer and users were still told
    there wasn't one."""
    print("\n[kb-cache] a newly ingested article is immediately searchable")
    _harness.reset_db()

    kb.ingest_kb_article(
        title="Coffee machine on floor 2 not working",
        body="The floor 2 coffee machine needs its water tank refilled from the tap beside it, then hold the power button for 5 seconds to reset.",
    )
    # Warm the cache with a first search.
    first = kb.search("coffee machine broken", top_k=1)
    check("first article is searchable", len(first) == 1 and "coffee" in first[0].title.lower())

    # Now ingest a SECOND article while the cache is warm. Without
    # invalidation this one is invisible.
    kb.ingest_kb_article(
        title="Standing desk controller unresponsive",
        body="If your standing desk controller does nothing, unplug it for 20 seconds then hold the down arrow to recalibrate.",
    )
    results = kb.search("my standing desk controller does nothing", top_k=3)
    titles = [r.title.lower() for r in results]
    check(
        "the second article is visible without a restart",
        any("standing desk" in t for t in titles),
        f"cache went stale; got {titles}",
    )
    check("article_count reflects both articles", kb.article_count() == 2, str(kb.article_count()))


# --- 3. Audit log immutability ---------------------------------------------


def test_audit_log_rejects_update():
    print("\n[audit] audit_log must reject UPDATE at the database level")
    _harness.reset_db()
    with db.session() as conn:
        db.log_event(conn, "test_event", {"a": 1}, ticket_id=None)

    raised = False
    message = ""
    try:
        with db.session() as conn:
            conn.execute("UPDATE audit_log SET event_type = 'tampered' WHERE id = 1")
    except Exception as e:  # noqa: BLE001 - we're asserting it raises
        raised = True
        message = str(e)
    check("UPDATE is rejected", raised, "audit log was silently mutable")
    check("error explains why", "append-only" in message.lower(), message)


def test_audit_log_rejects_delete():
    print("\n[audit] audit_log must reject DELETE at the database level")
    _harness.reset_db()
    with db.session() as conn:
        db.log_event(conn, "test_event", {"a": 1}, ticket_id=None)

    raised = False
    message = ""
    try:
        with db.session() as conn:
            conn.execute("DELETE FROM audit_log WHERE id = 1")
    except Exception as e:  # noqa: BLE001
        raised = True
        message = str(e)
    check("DELETE is rejected", raised, "audit log entries were silently deletable")
    check("error explains why", "append-only" in message.lower(), message)

    with db.session() as conn:
        remaining = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
    check("the entry still exists", remaining == 1, f"count={remaining}")


# --- 4. SQLite concurrency -------------------------------------------------


def test_concurrent_writes_do_not_fail():
    """The original risk: no WAL mode and no busy_timeout meant a second
    concurrent writer got 'database is locked' immediately rather than
    waiting, surfacing as a 500 to a user submitting a ticket."""
    print("\n[concurrency] simultaneous writers must not hit 'database is locked'")
    _harness.reset_db()

    errors: list[str] = []
    write_count = {"n": 0}
    lock = threading.Lock()

    def writer(worker_id: int):
        try:
            for i in range(10):
                with db.session() as conn:
                    db.insert_ticket(
                        conn,
                        requester=f"worker-{worker_id}",
                        category="software",
                        affected_system="test",
                        impact="single_user",
                        urgency="low",
                        priority="P4",
                        description=f"concurrent write {worker_id}-{i}",
                        reasoning="test",
                        red_flag_matched=False,
                        red_flag_category="",
                        red_flag_phrase="",
                        extraction_provider="none",
                    )
                with lock:
                    write_count["n"] += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"worker-{worker_id}: {e}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    check("no write errors under concurrency", not errors, "; ".join(errors[:3]))
    check("all 80 writes landed", write_count["n"] == 80, f"got {write_count['n']}")

    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
    check("all rows persisted", total == 80, f"db has {total}")


def test_wal_mode_is_active():
    print("\n[concurrency] WAL journal mode is actually enabled")
    _harness.reset_db()
    with db.session() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("journal_mode is wal", str(mode).lower() == "wal", f"got {mode!r}")


def test_foreign_keys_enforced():
    print("\n[integrity] foreign keys are enforced (off by default in SQLite)")
    _harness.reset_db()
    with db.session() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    check("foreign_keys pragma is on", fk == 1, f"got {fk}")


# --- 5. Network I/O outside transactions ----------------------------------


def test_extraction_happens_outside_db_transaction():
    """The original failure: extract_incident() (an LLM call, up to 30s) and
    send_p1_alert() (10s) ran inside `with db.session()`. With SQLite's
    single-writer model that serialized every concurrent submission behind an
    external API call.

    Verified structurally: a slow extractor must not prevent a concurrent
    writer from completing quickly.
    """
    print("\n[transactions] a slow extractor must not block other DB writers")
    _harness.reset_db()

    import app.extraction as extraction_module
    import app.intake as intake_module

    original = intake_module.extract_incident
    slow_call_started = threading.Event()

    def slow_extract(text):
        slow_call_started.set()
        time.sleep(2.0)  # stand-in for a slow LLM provider
        return original(text)

    intake_module.extract_incident = slow_extract
    try:
        submitter = threading.Thread(
            target=lambda: intake_module.file_ticket("slow extraction test", requester="slow")
        )
        submitter.start()

        # Wait until we're inside the slow "network call".
        assert slow_call_started.wait(timeout=5), "slow extract never ran"

        # A concurrent write should complete promptly. If extraction were
        # inside the transaction, this would block for the full 2s.
        started = time.perf_counter()
        with db.session() as conn:
            db.insert_ticket(
                conn, requester="fast", category="software", affected_system="test",
                impact="single_user", urgency="low", priority="P4",
                description="concurrent write during slow extraction",
                reasoning="test", red_flag_matched=False, red_flag_category="",
                red_flag_phrase="", extraction_provider="none",
            )
        elapsed = time.perf_counter() - started
        submitter.join(timeout=30)

        check(
            "concurrent write completed while extraction was in flight",
            elapsed < 1.0,
            f"took {elapsed:.2f}s — extraction is likely inside the transaction",
        )
    finally:
        intake_module.extract_incident = original


# --- 6. Rate limiter ------------------------------------------------------


def test_rate_limiter_blocks_and_recovers():
    print("\n[rate-limit] limiter blocks past the threshold and reports retry-after")
    limiter = RateLimiter(max_requests=3, window_seconds=2)
    results = [limiter.check("client-a")[0] for _ in range(5)]
    check("first 3 allowed", results[:3] == [True, True, True], str(results))
    check("4th and 5th blocked", results[3:] == [False, False], str(results))

    allowed, retry_after = limiter.check("client-a")
    check("blocked response includes a positive retry-after", not allowed and retry_after > 0, str(retry_after))

    check("a different client is unaffected", limiter.check("client-b")[0] is True)

    time.sleep(2.1)
    check("allowed again after the window expires", limiter.check("client-a")[0] is True)


def test_rate_limiter_prunes_expired_keys():
    """Without pruning, the key map grows one entry per distinct client
    forever — a slow memory leak on a public endpoint keyed by client IP."""
    print("\n[rate-limit] expired client entries are pruned (memory leak guard)")
    limiter = RateLimiter(max_requests=5, window_seconds=1)
    for i in range(50):
        limiter.check(f"client-{i}")
    check("all keys tracked initially", limiter.tracked_keys() == 50, str(limiter.tracked_keys()))
    time.sleep(1.1)
    removed = limiter.prune()
    check("expired keys were pruned", removed == 50, f"pruned {removed}")
    check("tracking map is empty", limiter.tracked_keys() == 0, str(limiter.tracked_keys()))


# --- 7. Config robustness -------------------------------------------------


def test_malformed_config_does_not_crash():
    print("\n[config] a malformed numeric env var falls back instead of crashing on import")
    import warnings

    from app.config import _get_float, _get_int

    import os

    os.environ["TEST_BAD_INT"] = "not-a-number"
    os.environ["TEST_BAD_FLOAT"] = "abc"
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            i = _get_int("TEST_BAD_INT", 42)
            f = _get_float("TEST_BAD_FLOAT", 1.5)
        check("bad int falls back to default", i == 42, str(i))
        check("bad float falls back to default", f == 1.5, str(f))
        check("a warning was emitted rather than failing silently", len(caught) >= 2, str(len(caught)))
    finally:
        os.environ.pop("TEST_BAD_INT", None)
        os.environ.pop("TEST_BAD_FLOAT", None)


# --- 8. Pagination --------------------------------------------------------


def test_voice_ui_has_no_interpolated_innerhtml():
    """Static guard against reintroducing the XSS.

    The original bug: voice.html interpolated server values into innerHTML.
    `reasoning` embeds the red-flag `matched_phrase`, which is a regex match
    against user-supplied text, and patterns like
    \\bvirus\\b.{0,15}\\b(detected)\\b contain wildcards — so a crafted
    description got attacker-controlled markup reflected into the page.
    Verified live against a real payload ("virus <img src=x> detected"):
    the server does reflect it, and the fixed UI renders it as inert text.

    A static check is the right tool here because the failure mode is a
    developer reaching for innerHTML again out of convenience, which no
    runtime test would catch until it shipped.
    """
    print("\n[xss] voice.html must not interpolate values into innerHTML")
    html = (ROOT_DIR / "app" / "static" / "voice.html").read_text(encoding="utf-8")

    import re as _re

    # Flag `innerHTML = ...` or `innerHTML += ...` where the right-hand side
    # contains a ${...} template substitution.
    offenders = _re.findall(r"innerHTML\s*\+?=\s*[^;\n]*\$\{[^}]+\}", html)
    check(
        "no innerHTML assignment contains ${...} interpolation",
        not offenders,
        f"found {len(offenders)}: {offenders[:2]}",
    )

    check(
        "textContent is used for rendering server values",
        "textContent" in html,
        "expected the DOM-building helper to assign textContent",
    )


def test_ticket_listing_pagination():
    print("\n[pagination] list_tickets honours limit/offset")
    _harness.reset_db()
    with db.session() as conn:
        for i in range(25):
            db.insert_ticket(
                conn, requester="test", category="software", affected_system="test",
                impact="single_user", urgency="low", priority="P4",
                description=f"ticket {i}", reasoning="test", red_flag_matched=False,
                red_flag_category="", red_flag_phrase="", extraction_provider="none",
            )
    with db.session() as conn:
        page1 = db.list_tickets(conn, limit=10, offset=0)
        page2 = db.list_tickets(conn, limit=10, offset=10)
        unbounded = db.list_tickets(conn)
    check("page 1 has 10 rows", len(page1) == 10, str(len(page1)))
    check("page 2 has 10 rows", len(page2) == 10, str(len(page2)))
    check("pages don't overlap", {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2}))
    check("unbounded call still returns everything (CLI use)", len(unbounded) == 25, str(len(unbounded)))


def main():
    print("=" * 78)
    print("Production hardening regression tests")
    print("=" * 78)

    test_tests_never_touch_real_db()
    test_kb_cache_invalidates_on_ingest()
    test_audit_log_rejects_update()
    test_audit_log_rejects_delete()
    test_wal_mode_is_active()
    test_foreign_keys_enforced()
    test_concurrent_writes_do_not_fail()
    test_extraction_happens_outside_db_transaction()
    test_rate_limiter_blocks_and_recovers()
    test_rate_limiter_prunes_expired_keys()
    test_malformed_config_does_not_crash()
    test_voice_ui_has_no_interpolated_innerhtml()
    test_ticket_listing_pagination()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("All production-hardening regressions covered.")


if __name__ == "__main__":
    main()
