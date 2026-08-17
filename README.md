# Freelance Projects

Each subfolder is a standalone, self-contained project — its own code, dependencies, tests, and documentation. Nothing is shared between folders.

| Folder | Project | Tests |
|---|---|---|
| [`01-voice-receptionist`](01-voice-receptionist/) | Voice receptionist that books/cancels appointments. Cannot double-book (verified with concurrent callers racing one slot), cannot confirm a booking that doesn't exist, cannot strand a caller | 106 + 16 call replays |
| [`02-helpdesk-intake-triage`](02-helpdesk-intake-triage/) | IT helpdesk intake with self-service deflection, a deterministic priority rules engine, security/outage red-flag override, durable alerting, and a browser voice UI | ~208 |
| [`05-hybrid-rag-search-engine`](05-hybrid-rag-search-engine/) | Domain-specific hybrid search (dense + lexical) with reranking, ACL-filtered retrieval, and verified citations | 39 |
| [`08-prompt-to-bi`](08-prompt-to-bi/) | Plain-English analytics via a semantic layer — the LLM selects from defined metrics, SQL is generated deterministically, and undefined questions get refused rather than guessed | 64 + 28 golden |

Each project runs its full suite with `python run_all_tests.py` — deterministic, no API keys or network required.

More projects are added as their own top-level folders as they're built.
