# Freelance Projects

Each subfolder is a standalone, self-contained project — its own code, dependencies, tests, and documentation. Nothing is shared between folders.

| Folder | Project | Tests |
|---|---|---|
| [`02-helpdesk-intake-triage`](02-helpdesk-intake-triage/) | IT helpdesk intake with self-service deflection, a deterministic priority rules engine, security/outage red-flag override, durable alerting, and a browser voice UI | ~208 |
| [`05-hybrid-rag-search-engine`](05-hybrid-rag-search-engine/) | Domain-specific hybrid search (dense + lexical) with reranking, ACL-filtered retrieval, and verified citations | 39 |

Each project runs its full suite with `python run_all_tests.py` — deterministic, no API keys or network required.

More projects are added as their own top-level folders as they're built.
