# Manual Steps

**For GitHub + a demo video: there is nothing you need to do.** The CLI works out of the box with the sample corpus already ingested and calibrated. Skip straight to the bottom of this file for a demo script.

Everything involving a live server, API keys, or a client's real documents is deployment work, not demo prep — that's all in [`PRODUCT_SCALING_TASKS.md`](PRODUCT_SCALING_TASKS.md), to come back to only once a client actually signs.

---

## The one genuinely optional thing: an LLM key

Right now the system runs in **extractive mode** — it shows you the actual retrieved passages instead of a generated sentence. No API key, no cost, and it can't hallucinate because nothing is generated. This is what's currently running and what the demo below uses.

If you want a more natural-sounding generated answer for the video (optional, takes 2 minutes, free tier):

1. Get a free key from https://console.groq.com
2. Add to `.env`:
   ```
   LLM_PROVIDER=groq
   GROQ_API_KEY=gsk_...
   ```
3. Re-run any query — you'll see a written answer instead of raw passages.

**I have not tested this against a live provider** (no key was available while building) — the logic is tested via a mock, but the real API call is unverified. If it errors, extractive mode is the safe fallback and is what I'd actually recommend showing in a client video anyway: it's the version that provably cannot make something up.

---

## Push to GitHub

```bash
cd 05-hybrid-rag-search-engine
git init
git add .
git commit -m "Hybrid RAG search engine: hybrid retrieval, ACL enforcement, verified citations"
```

Then create an empty repo on github.com (no README/gitignore — you already have both) and:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

`.env` and `keys.json` are already gitignored — verify nothing sensitive got staged with `git status` before the first commit.

---

## Demo video script (CLI, no server needed)

Everything below runs standalone — no auth, no deployment, just `python -m app.cli`.

```bash
# 1. Show the corpus
python -m app.cli stats

# 2. A normal answerable question — cited, verified
python -m app.cli query "How many vacation days do employees get?" --groups public

# 3. THE ACCESS-CONTROL DEMO — same question, two identities
python -m app.cli query "How are executive bonuses calculated?" --groups public
python -m app.cli query "How are executive bonuses calculated?" --groups management

# 4. THE HONESTY DEMO — a question with no answer in the corpus
python -m app.cli query "What is the company's policy on sabbatical leave?" --groups public

# 5. Ingest a real PDF live
python -m app.cli ingest "../Freelance_Team_Portfolio_Guide.pdf" --groups public
python -m app.cli query "What should the hero section include?" --groups public

# 6. The test suite, for credibility
python run_all_tests.py
```

**Steps 3 and 4 are the ones worth narrating slowly.** #3 shows the system correctly withholding a restricted document from one identity and correctly surfacing it for another — the same question, different access. #4 shows it saying "I don't know" instead of inventing an answer, which is the thing most RAG demos get wrong. Those two moments are the actual pitch: this isn't just search, it's search that knows what it's allowed to tell you and knows when it doesn't know.
