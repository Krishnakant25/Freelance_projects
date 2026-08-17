"""
API-level tests using FastAPI's TestClient — input validation, rate limiting,
and the readiness probe.

These run in-process (no real server, no port binding) so they're fast and
safe in CI.

Run:  python eval/test_api.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

from app import config  # noqa: E402

_harness.quiet_logs()

# Keep the limiter tight so the rate-limit test doesn't need 30 requests.
config.RATE_LIMIT_REQUESTS = 5
config.RATE_LIMIT_WINDOW_SECONDS = 60

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


STAFF_KEY = None
ADMIN_KEY = None


def make_client():
    from fastapi.testclient import TestClient

    from app import api

    global STAFF_KEY, ADMIN_KEY
    _harness.reset_db()
    _harness.clear_test_keys()
    STAFF_KEY = _harness.create_test_key("test-staff", ["staff"])
    ADMIN_KEY = _harness.create_test_key("test-admin", ["admin"])
    # Reset limiter state between tests so ordering doesn't matter.
    api._rate_limiter.reset()
    return TestClient(api.app)


def staff_headers():
    return {"X-API-Key": STAFF_KEY}


def admin_headers():
    return {"X-API-Key": ADMIN_KEY}


def test_health_and_ready():
    print("\n[probes] /health is cheap, /ready reports real state")
    client = make_client()
    r = client.get("/health")
    check("/health returns 200", r.status_code == 200, str(r.status_code))
    check("/health says ok", r.json().get("status") == "ok", r.text)

    r = client.get("/ready")
    check("/ready returns 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("/ready reports ticket count", "tickets" in body, r.text)
    check("/ready reports kb_articles", "kb_articles" in body, r.text)
    check("/ready reports extraction provider", body.get("extraction_provider") == "none", r.text)
    check(
        "/ready warns about an empty KB",
        any("KB is empty" in w for w in body.get("warnings", [])),
        str(body.get("warnings")),
    )


def test_oversized_description_rejected():
    """Unbounded text on /report is an amplification vector: the endpoint runs
    an embedding model over the description, so multi-MB input is cheap to
    send and expensive to process."""
    print("\n[validation] oversized description is rejected before any processing")
    client = make_client()
    huge = "a" * (config.MAX_DESCRIPTION_CHARS + 1)
    r = client.post("/report", json={"description": huge})
    check("returns 422 not 200", r.status_code == 422, f"{r.status_code}: {r.text[:120]}")

    at_limit = "my laptop is broken " * 10
    r2 = client.post("/report", json={"description": at_limit})
    check("a normal-length description is accepted", r2.status_code == 200, f"{r2.status_code}: {r2.text[:120]}")


def test_blank_and_missing_description_rejected():
    print("\n[validation] blank/whitespace/missing description is rejected")
    client = make_client()
    check("empty string rejected", client.post("/report", json={"description": ""}).status_code == 422)
    check("whitespace-only rejected", client.post("/report", json={"description": "     "}).status_code == 422)
    check("missing field rejected", client.post("/report", json={}).status_code == 422)


def test_oversized_requester_rejected():
    print("\n[validation] oversized requester name is rejected")
    client = make_client()
    r = client.post(
        "/report",
        json={"description": "my mouse stopped working", "requester": "x" * (config.MAX_REQUESTER_CHARS + 1)},
    )
    check("returns 422", r.status_code == 422, f"{r.status_code}: {r.text[:120]}")


def test_rate_limiting_enforced():
    print("\n[rate-limit] the public intake endpoint is rate limited")
    client = make_client()
    statuses = []
    for i in range(config.RATE_LIMIT_REQUESTS + 3):
        r = client.post("/report", json={"description": f"unique issue number {i} with my keyboard"})
        statuses.append(r.status_code)

    check(
        f"first {config.RATE_LIMIT_REQUESTS} requests allowed",
        all(s == 200 for s in statuses[: config.RATE_LIMIT_REQUESTS]),
        str(statuses),
    )
    check("subsequent requests get 429", 429 in statuses, str(statuses))

    blocked = next(
        (i for i, s in enumerate(statuses) if s == 429), None
    )
    check("429 appears only after the limit", blocked is not None and blocked >= config.RATE_LIMIT_REQUESTS, str(statuses))


def test_request_id_header_returned():
    print("\n[observability] every response carries an X-Request-ID")
    client = make_client()
    r = client.get("/health")
    check("X-Request-ID present", "X-Request-ID" in r.headers, str(dict(r.headers)))

    r2 = client.get("/health", headers={"X-Request-ID": "my-trace-id"})
    check("a caller-supplied request id is echoed", r2.headers.get("X-Request-ID") == "my-trace-id", r2.headers.get("X-Request-ID"))


def test_tickets_pagination_capped():
    print("\n[pagination] /tickets caps limit and returns a total")
    client = make_client()
    for i in range(5):
        client.post("/report", json={"description": f"distinct problem {i} with the office printer"})

    r = client.get("/tickets?limit=99999", headers=staff_headers())
    check("returns 200", r.status_code == 200, r.text[:120])
    body = r.json()
    check("limit is capped at 500", body["limit"] <= 500, str(body.get("limit")))
    check("total is reported", "total" in body, r.text[:200])


# --- Authentication --------------------------------------------------------


def test_intake_stays_anonymous():
    """Intake being open is a design choice, not an oversight — it's the public
    submission surface. This test pins that so a future auth change doesn't
    accidentally break the widget."""
    print("\n[auth] intake endpoints must remain anonymous")
    client = make_client()
    r = client.post("/report", json={"description": "my keyboard has stopped responding entirely"})
    check("/report works with no key", r.status_code == 200, f"{r.status_code}: {r.text[:120]}")

    r2 = client.get("/health")
    check("/health works with no key", r2.status_code == 200)
    r3 = client.get("/ready")
    check("/ready works with no key", r3.status_code == 200)


def test_staff_endpoints_reject_anonymous():
    """THE GAP THIS CLOSES: these endpoints expose every ticket's contents —
    including whatever users typed — and mutate state. They were previously
    open to anyone who could reach the port."""
    print("\n[auth] staff endpoints must reject unauthenticated requests")
    client = make_client()
    client.post("/report", json={"description": "we found ransomware on the finance server"})

    checks = [
        ("GET /tickets", client.get("/tickets")),
        ("GET /tickets/1", client.get("/tickets/1")),
        ("POST /tickets/1/acknowledge", client.post("/tickets/1/acknowledge")),
        ("POST /tickets/1/resolve", client.post("/tickets/1/resolve")),
        ("GET /stats", client.get("/stats")),
        ("POST /admin/check-escalations", client.post("/admin/check-escalations")),
        ("POST /admin/flush-alerts", client.post("/admin/flush-alerts")),
        ("POST /admin/reload-keys", client.post("/admin/reload-keys")),
    ]
    for label, resp in checks:
        check(f"{label} -> 401 without a key", resp.status_code == 401, f"got {resp.status_code}")


def test_invalid_key_rejected():
    print("\n[auth] an invalid key is rejected")
    client = make_client()
    r = client.get("/tickets", headers={"X-API-Key": "hd_completely_made_up_key"})
    check("returns 401", r.status_code == 401, f"got {r.status_code}")


def test_staff_key_works_but_cannot_do_admin():
    print("\n[auth] a staff key reads tickets but cannot trigger admin actions")
    client = make_client()
    client.post("/report", json={"description": "we found ransomware on the finance server"})

    r = client.get("/tickets", headers=staff_headers())
    check("staff key can list tickets", r.status_code == 200, f"{r.status_code}: {r.text[:120]}")

    r2 = client.post("/tickets/1/acknowledge", headers=staff_headers())
    check("staff key can acknowledge", r2.status_code == 200, f"{r2.status_code}: {r2.text[:120]}")

    r3 = client.post("/admin/check-escalations", headers=staff_headers())
    check("staff key is FORBIDDEN from admin actions (403)", r3.status_code == 403, f"got {r3.status_code}")


def test_admin_key_can_do_both():
    print("\n[auth] an admin key implies staff access too")
    client = make_client()
    client.post("/report", json={"description": "the entire company cannot access email right now"})

    r = client.get("/tickets", headers=admin_headers())
    check("admin key can list tickets (admin implies staff)", r.status_code == 200, f"{r.status_code}")

    r2 = client.post("/admin/check-escalations", headers=admin_headers())
    check("admin key can trigger escalation sweep", r2.status_code == 200, f"{r2.status_code}: {r2.text[:120]}")

    r3 = client.post("/admin/flush-alerts", headers=admin_headers())
    check("admin key can flush the alert outbox", r3.status_code == 200, f"{r3.status_code}")


def test_acknowledge_records_who():
    """An audit log that says 'via api' for every action can't answer 'who
    acknowledged this' — which is the main question after an incident."""
    print("\n[audit] state changes record which principal made them")
    client = make_client()
    client.post("/report", json={"description": "we found ransomware on the finance server"})
    client.post("/tickets/1/acknowledge", headers=staff_headers())

    r = client.get("/tickets/1", headers=staff_headers())
    check("ticket detail returns 200", r.status_code == 200, r.text[:120])
    events = r.json().get("audit_log", [])
    ack_events = [e for e in events if e["event_type"] == "acknowledged"]
    check("an acknowledged event exists", len(ack_events) == 1, str(len(ack_events)))
    check(
        "the acting principal is recorded",
        ack_events and "test-staff" in ack_events[0]["details"],
        str(ack_events[0]["details"]) if ack_events else "no event",
    )


def test_ready_reports_outbox_and_scheduler():
    print("\n[observability] /ready surfaces outbox and scheduler state")
    client = make_client()
    r = client.get("/ready")
    body = r.json()
    check("outbox counts reported", "outbox" in body and "pending" in body["outbox"], str(body.get("outbox")))
    check("scheduler state reported", "scheduler" in body, str(body.get("scheduler")))
    check("auth_enabled reported", "auth_enabled" in body, str(body.get("auth_enabled")))
    # Scheduler is disabled in tests, so /ready should warn about it — proving
    # the warning fires rather than being decorative.
    check(
        "warns when the escalation scheduler isn't running",
        any("scheduler" in w.lower() for w in body.get("warnings", [])),
        str(body.get("warnings")),
    )


def test_red_flag_path_through_api():
    print("\n[end-to-end] a red-flag report becomes P1 via the API")
    client = make_client()
    r = client.post(
        "/report",
        json={"description": "no rush but I think I clicked a phishing link this morning"},
    )
    check("returns 200", r.status_code == 200, r.text[:200])
    body = r.json()
    check("a ticket was created (not deflected)", body.get("outcome") == "ticket_created", r.text[:200])
    check("priority is P1", body.get("priority") == "P1", str(body.get("priority")))
    check("red_flag is reported", body.get("red_flag") is True, str(body.get("red_flag")))


def main():
    print("=" * 78)
    print("API tests (validation, rate limiting, probes)")
    print("=" * 78)

    test_health_and_ready()
    test_oversized_description_rejected()
    test_blank_and_missing_description_rejected()
    test_oversized_requester_rejected()
    test_rate_limiting_enforced()
    test_request_id_header_returned()
    test_tickets_pagination_capped()
    test_red_flag_path_through_api()
    test_intake_stays_anonymous()
    test_staff_endpoints_reject_anonymous()
    test_invalid_key_rejected()
    test_staff_key_works_but_cannot_do_admin()
    test_admin_key_can_do_both()
    test_acknowledge_records_who()
    test_ready_reports_outbox_and_scheduler()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("API enforces input limits, rate limits, and reports real readiness.")


if __name__ == "__main__":
    main()
