# Product Scaling Tasks

**None of this is needed to use the product, push it to GitHub, or record a demo.** Everything here only matters once you stand this up as a live server that a client's team hits over the network. The CLI (what you'd actually record for a demo — `python -m app.cli query "..."`) talks to the local database directly and never touches any of this.

Come back to this file when you're moving from "demo on my laptop" to "a client's team uses this daily." Ordered by when each one bites you.

---

## Task 1 — API authentication (only if you run the HTTP server)

**Trigger:** you run `uvicorn app.api:app` and let someone other than you call it.

The API refuses every request until at least one key exists, on purpose — a RAG system with no auth on `/query` will happily hand a restricted document to anyone who asks.

```bash
python scripts/manage_keys.py create --name admin --groups management,public --can-ingest
```

That prints a key once. Save it. Give each client/consumer their own key with only the groups they should see — that's what makes the access control in this project actually mean something instead of being a demo feature.

**Not needed for:** the CLI, or a local demo video. Only for the HTTP API.

---

## Task 2 — LLM key for synthesized answers (optional, any time)

Already covered in `MANUAL_STEPS.md` §2 — this is the one task that's genuinely optional and low-effort regardless of deployment stage. Skip it for now; extractive mode (current default) works fine for a demo and costs nothing.

---

## Task 3 — TLS + reverse proxy (only if you deploy the server publicly)

**Trigger:** the API needs to be reachable from outside your own machine (a client's browser, another company's server).

Right now `uvicorn` would be talking plain HTTP if you ran it exposed — API keys traveling in plaintext over the network. Before that happens:

1. Get a domain + TLS certificate (Let's Encrypt is free via Certbot, or use whatever your hosting provider provides — Render/Railway/Fly.io issue one automatically).
2. Put nginx or Caddy in front of uvicorn; it terminates HTTPS and forwards to `127.0.0.1:8000`.
3. Bind uvicorn to `127.0.0.1` only, never `0.0.0.0` — the proxy is the only thing that should be internet-facing.

**Not needed for:** anything running only on your own machine, including the demo recording.

---

## Task 4 — Multi-worker / horizontal scale (only past real concurrent load)

**Trigger:** you have enough simultaneous users that one process is slow, or you need the server to survive a restart without dropping in-flight requests.

The cache, the vector index cache, and the rate limiter all live in one process's memory today — running multiple workers means each one enforces its own limits and has a cold cache, and SQLite only allows one writer at a time regardless. This means:
- Migrating SQLite to Postgres
- Moving the cache and rate limiter to Redis

This is real engineering work, not a config flag. Full technical detail is in `DEPLOYMENT.md` §4 when you get there. For a single client's internal tool, you likely never need this.

---

## Task 5 — Recalibrate for a new/changed corpus (only after real client documents are loaded)

**Trigger:** you swap in a client's actual documents instead of the 4-document sample corpus this was tuned on.

```bash
python eval/calibrate_threshold.py
```

The "is this answer actually in the corpus" cutoff was tuned on a tiny sample set with a fragile margin (0.43 points — see `app/config.py`). It will drift once the real document set is different. Re-run this, and replace `eval/eval_set.json` with real questions about the client's actual documents, once there's a real corpus to tune against.

**Not needed for:** the current demo corpus, which is already calibrated and passing 31/31.

---

## Priority order if a client actually signs

1. Task 1 (auth) — always first, it's the difference between a toy and a product
2. Task 5 (recalibrate) — as soon as their real documents are loaded
3. Task 3 (TLS) — before anything is reachable outside your machine
4. Task 4 (scale) — only if/when load actually requires it
