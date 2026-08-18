# Production Gaps

What this project does **not** do, why, and what it would take. Written so a
client can tell the difference between "not built" and "not needed yet".

Everything here was found by auditing the running system, not by guessing.

---

## 1. The audit log cannot detect tail truncation

**Status:** deferred — needs infrastructure, not code.

The hash chain detects any record being **edited** or **deleted from the middle**,
and names the first bad sequence number. It cannot detect an attacker lopping off
the *end* of the log, because nothing references records that no longer exist.
This is a property of chains generally, not a bug in this one.

`eval/test_audit_and_registry.py` tests this honestly: it asserts the truncated
chain still verifies, so the limitation is pinned rather than papered over.

**To close it:** anchor externally — periodically publish the head hash somewhere
the agent cannot write (a separate append-only service, an object store with
retention lock, or another org's system). Then a missing tail is provable. Needs
infrastructure this demo does not have.

## 2. The audit log is single-process

**Status:** deferred.

Writes are lock-protected and **verified under 8 concurrent writers x 40 writes —
320/320 records, chain intact**. But the lock is in-process. Two workers
(`uvicorn --workers 2`) appending to the same file would interleave and break the
chain, which `/ready` would then correctly report as unhealthy, taking the service
down.

**Today:** run one worker. **To close it:** move to an append-only store with
server-side sequencing (Postgres with a sequence, or a dedicated audit service).

## 3. `exec.run` has no sandbox

**Status:** disabled by default (`EXEC_ENABLED=false`).

The tool exists, is tiered `NEEDS_APPROVAL`, and is capability-gated — but if
enabled it runs commands with the host process's privileges. Approval is not a
substitute for isolation.

**To close it:** container or microVM per execution, with its own filesystem view
and no network by default. Do not enable this without one.

## 4. Approvals are in-memory

**Status:** acceptable for a demo, not for production.

A pending approval lives in the `PolicyEngine` for its actor. A process restart
loses it. Nothing unsafe happens (the action simply never runs — it fails closed),
but a human waiting on a prompt sees it vanish.

**To close it:** persist pending approvals with the same durable-outbox pattern
used in the helpdesk project (`02-helpdesk-intake-triage/app/alerting.py`).

## 5. No per-tenant capability isolation

**Status:** out of scope as specified.

All operators share one workspace root. Multi-tenant use needs a capability set
derived per principal, and a workspace root per tenant.

**To close it:** key the workspace off `Principal`, and add a test that tenant A
cannot resolve a path into tenant B's root. The containment machinery already
supports this — it is a wiring change, not a design change.

## 6. Session eviction is per-process

Idle sessions are evicted after a TTL, with pending-approval sessions always
kept. Behind multiple workers, a request could land on a worker without the
session that holds the approval. Same fix as gap 4: persist approvals.

---

# Fixed during the adversarial audit

These were live defects found by probing the running system. Each has a
regression test.

### A. The per-task budget had become a lifetime lockout (**high**)

`MAX_TOOL_CALLS_PER_TASK` bounds a runaway agent loop *within one task*. The API
holds one `PolicyEngine` per actor for the life of the process, so the counter
never reset: after 50 actions **ever**, that operator was denied everything until
a restart — reported as "tool-call budget exceeded", which reads as a runaway
loop and would have sent an operator hunting in entirely the wrong place.

Right mechanism, wrong lifecycle. Bounding an actor's total request volume is the
rate limiter's job, at a different layer; conflating the two broke both.

**Fixed:** `PolicyEngine.begin_task()` resets the budget per request. The budget
still bites within a task — pinned by test, so the fix cannot silently disable it.

### B. Sessions grew without bound (**medium**)

One `PolicyEngine` retained per distinct actor, forever. 500 actors, 500 engines.

**Fixed:** idle-TTL eviction with an `_MAX_SESSIONS` backstop. Sessions holding a
**pending approval are never evicted** — discarding one would silently drop a
decision a human still owed an answer to, so the request would vanish rather than
be rejected.

### C. Refused traversals left no trace (**medium**)

The reader returns refusals as error *findings* rather than exceptions, so that
an attacker-influenced message can never re-enter as an instruction. Correct — but
it meant a path-traversal probe was recorded nowhere, and the API answered
**200 OK**. In every dashboard and access log, someone probing for `../../.env`
looked identical to a successful read.

Doc §6.6 makes denials the more interesting half of the audit data; this path
dropped them entirely.

**Fixed:** refusals are audited as `reader_refused`, and the API translates an
all-error finding set back into 403/404 at the boundary.

### D. Credential redaction was tangled up with injection detection (**medium**)

Found by ablation (see below). `AKIA[0-9A-Z]{16}` lived in the *instruction*
pattern list, so a credential in a file's content was (a) mislabelled as
"instruction-shaped text" and (b) handled all-or-nothing — the entire summary was
discarded instead of the secret being removed.

**Fixed:** `redact_secrets()` is now a separate, unconditional, surgical control
covering AWS keys, GitHub/OpenAI/Slack tokens, private keys, and JWTs. It runs
whether or not the injection check fires, so secret hygiene never depends on
injection heuristics.

### E. The central claim was asserted, not measured

The README said injections fail because the capability was never granted, "not
because the attack text was recognised". That is easy to assert and easy to break
silently — one refactor leaning on the sanitizer and the project becomes the
pattern-matching defence it was built to avoid, with a green suite either way.

**Fixed:** `eval/run_ablation.py` is now a permanent suite. It disables the
instruction matcher entirely and re-runs the full red-team corpus:

```
BASELINE - everything enabled
  full defence            PASS   20/20 payloads, 100 executions
ABLATED - instruction matcher never matches
  capability model alone  PASS   20/20 payloads, 100 executions
```

**Every invariant holds with the sanitizer disabled.** The claim is now a
measurement. If a future change makes the sanitizer load-bearing, this suite goes
red and says so.

---

# Verified sound during the audit

Probed and found correct — recorded so nobody re-litigates them:

| Probe | Result |
|---|---|
| Concurrent audit writes (8 threads x 40) | 320/320 records, chain intact |
| Session grant past its TTL | correctly reverts to requiring approval |
| Resolving another session's approval id | refused; the original stays pending |
| Refused self-approval | does **not** consume the request — a second party can still act, so approvals cannot be burned by self-approving them into oblivion |
