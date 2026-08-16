# Manual Steps

**For a demo video or local testing: nothing required.** Everything below works out of the box with `LLM_PROVIDER=none` (rule-based extraction, no key, no cost).

---

## Optional: an LLM key for better extraction

Rule-based extraction is legible and free but is keyword matching, not real understanding. For noticeably better handling of ambiguous free text:

1. Get a free key from https://console.groq.com
2. Add to `.env`:
   ```
   LLM_PROVIDER=groq
   GROQ_API_KEY=gsk_...
   ```

**I have not tested this against a live provider** — same caveat as the RAG project. The logic is tested via a deterministic mock (`eval/test_extraction.py`), including the case where the model returns an invalid enum value; the actual HTTP call is unverified. If it errors, extraction falls back to rule-based automatically — that's tested too.

---

## Optional: Slack alerts for P1 tickets

Without this, P1 alerts go to structured logs instead (still visible, just not a real page):

1. Create a Slack incoming webhook: https://api.slack.com/messaging/webhooks
2. Add to `.env`: `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...`

---

## Demo script (CLI, no server needed)

```bash
python -m app.cli ingest-kb data/kb_articles

# 1. Self-service deflection — no ticket, a KB article is offered instead
python -m app.cli report "my VPN keeps dropping every few minutes"

# 2. A routine, low-priority ticket — correctly de-prioritized
python -m app.cli report "just my monitor has a slight flicker, not urgent"

# 3. THE PRIORITY-ENGINE DEMO — same category of issue, different scope/urgency
python -m app.cli report "my laptop screen went black, I need it for a client call in 20 minutes"
python -m app.cli report "our whole team lost access to the shared drive this morning"
python -m app.cli report "the entire company can't access email, this is critical"

# 4. THE RED-FLAG DEMO — casually phrased, still forces P1
python -m app.cli report "hey no rush, but I think I might have clicked a phishing link earlier"

# 5. Show the queue, sorted by priority
python -m app.cli queue

# 6. The test suite, for credibility
python run_all_tests.py
```

**Steps 3 and 4 are the ones worth narrating.** Step 3 shows the *same* rules engine producing four different priorities from four different combinations of scope and urgency — a decision a person could check by hand, not a black box. Step 4 shows the red-flag override catching a security report even when the user themselves downplays it ("no rush") — the system doesn't trust the user's own framing of severity, on purpose, because the cost of missing a real security incident is much higher than the cost of a false alarm.

---

## Push to GitHub (into the shared `Freelance_projects` repo)

Same pattern as the RAG project — this folder goes in as its own top-level directory, not merged into another project's files.

```bash
# From wherever your clone of Freelance_projects lives:
cp -r /path/to/02-helpdesk-intake-triage ./02-helpdesk-intake-triage
cd Freelance_projects
git add 02-helpdesk-intake-triage/ README.md
git commit -m "Add helpdesk-intake-triage project"
git push
```

Verify nothing sensitive is staged before committing — `.env` and `data/db/*.sqlite3` are gitignored, but double-check with `git status` regardless (same discipline as the RAG project's push).

---

## Before showing this to a real IT team

Not required for a demo, but worth knowing before a pilot:

- **Feed it real historical tickets.** Run 50–100 of a client's actual past tickets through `python -m app.cli report "<text>"` and compare the computed priority to what a human actually assigned. Disagreements tell you whether the priority matrix or the red-flag list needs adjusting for their specific environment — do this before trusting the numbers on anything but the demo KB.
- **Extend the red-flag list with their terminology.** Internal system names, specific compliance triggers, anything their security team already treats as "page immediately" — `app/redflag.py: add_pattern()` is the extension point.
- **No auth is implemented.** If this goes anywhere reachable by more than you, that's the first gap to close — see README's Known Limitations.
