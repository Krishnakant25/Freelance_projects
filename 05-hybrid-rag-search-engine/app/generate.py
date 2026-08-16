"""
Answer generation with programmatic citation verification (§6.8 of the
architecture doc): the LLM is asked to cite chunk IDs, and every citation is
then checked against the actual retrieved set and against lexical overlap
with the cited chunk before being shown to the user. An unverifiable citation
is dropped rather than trusted, because citations are the whole trust
mechanism of this product.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import config
from .retrieval import RetrievedChunk

SYSTEM_INSTRUCTIONS = """You are a domain search assistant. Answer the user's question using ONLY the \
numbered context passages below. Rules:
1. Every factual claim must be traceable to a passage. Cite passages by their [id] number.
2. If the passages don't contain enough information to answer, set "insufficient_evidence": true \
and explain what's missing — do not guess or use outside knowledge.
3. Respond with ONLY a JSON object, no markdown fences, matching exactly:
{"answer": "<answer text with inline [id] citations>", "citations": [<id>, <id>, ...], \
"insufficient_evidence": <true|false>}
"""


@dataclass
class Citation:
    chunk_id: int
    document_title: str
    source_path: str
    section: str
    verified: bool
    overlap_score: float = 0.0


@dataclass
class GenerationResult:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    insufficient_evidence: bool = False
    provider: str = "none"


def _build_prompt(query: str, chunks: list[RetrievedChunk]) -> str:
    passages = "\n\n".join(f"[{c.chunk_id}] {c.text}" for c in chunks)
    return f"{SYSTEM_INSTRUCTIONS}\n\nContext passages:\n{passages}\n\nQuestion: {query}\n\nJSON:"


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def _call_groq(prompt: str) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": config.GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.0}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(prompt: str) -> str:
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


_MOCK_BEHAVIOUR = "normal"


def set_mock_behaviour(behaviour: str) -> None:
    """Test hook. See _call_mock for the supported behaviours."""
    global _MOCK_BEHAVIOUR
    _MOCK_BEHAVIOUR = behaviour


def _call_mock(prompt: str, chunks: list[RetrievedChunk]) -> str:
    """
    Deterministic stand-in for a real LLM, so the generation + citation-
    verification path can be tested without an API key, network, or cost.

    This exists because that path shipped untested: every earlier run used
    LLM_PROVIDER=none (extractive mode), which never exercises JSON parsing,
    citation extraction, or verification. The adversarial behaviours below
    are the point — they assert that a model returning bad citations gets
    caught rather than trusted.

    Behaviours:
      normal            — cites the real top chunk
      hallucinate_cite  — cites a chunk ID that was never retrieved
      unsupported_claim — answers with text unrelated to the cited chunk
      insufficient      — correctly declines to answer
      malformed_json    — returns prose instead of JSON
    """
    top_id = chunks[0].chunk_id if chunks else 0

    if _MOCK_BEHAVIOUR == "hallucinate_cite":
        return json.dumps(
            {
                "answer": f"According to the documents, the answer is described in detail. [{999999}]",
                "citations": [999999],
                "insufficient_evidence": False,
            }
        )
    if _MOCK_BEHAVIOUR == "unsupported_claim":
        return json.dumps(
            {
                "answer": "Quarterly zeppelin maintenance schedules require tungsten recalibration.",
                "citations": [top_id],
                "insufficient_evidence": False,
            }
        )
    if _MOCK_BEHAVIOUR == "insufficient":
        return json.dumps(
            {
                "answer": "The provided passages don't contain this information.",
                "citations": [],
                "insufficient_evidence": True,
            }
        )
    if _MOCK_BEHAVIOUR == "malformed_json":
        return "I'm afraid I can't format that as JSON today."

    snippet = chunks[0].raw_text[:200] if chunks else ""
    return json.dumps(
        {
            "answer": f"{snippet} [{top_id}]",
            "citations": [top_id],
            "insufficient_evidence": False,
        }
    )


def _extractive_fallback(query: str, chunks: list[RetrievedChunk]) -> dict:
    """LLM_PROVIDER=none: no API call, no cost, no key required. Returns the
    top passages verbatim instead of a synthesized answer — always correct
    by construction, since nothing is generated."""
    if not chunks:
        return {"answer": "", "citations": [], "insufficient_evidence": True}
    answer = "No LLM configured (LLM_PROVIDER=none) — showing the top matching passages:\n\n"
    answer += "\n\n".join(f"[{c.chunk_id}] {c.raw_text}" for c in chunks[:3])
    return {"answer": answer, "citations": [c.chunk_id for c in chunks[:3]], "insufficient_evidence": False}


def _word_overlap(a: str, b: str) -> float:
    """Cheap lexical-overlap heuristic used to flag citations where the
    answer's claim doesn't actually seem to be supported by the cited chunk.
    Not a substitute for a real entailment model, but catches the obvious
    mismatches for free."""
    words_a = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", a) if len(w) > 3}
    words_b = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", b) if len(w) > 3}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a)


def _verify_citations(
    answer: str, cited_ids: list, chunks_by_id: dict[int, RetrievedChunk]
) -> list[Citation]:
    verified = []
    for raw_id in cited_ids:
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        chunk = chunks_by_id.get(cid)
        if chunk is None:
            # Cited an ID that was never in the retrieved set — never trust this.
            continue
        overlap = _word_overlap(answer, chunk.raw_text)
        verified.append(
            Citation(
                chunk_id=cid,
                document_title=chunk.document_title,
                source_path=chunk.source_path,
                section=chunk.section,
                verified=overlap >= 0.15,
                overlap_score=overlap,
            )
        )
    return verified


def generate_answer(query: str, chunks: list[RetrievedChunk]) -> GenerationResult:
    chunks_by_id = {c.chunk_id: c for c in chunks}
    provider = config.LLM_PROVIDER

    if not chunks:
        return GenerationResult(
            answer="I couldn't find anything relevant in the indexed documents.",
            citations=[],
            insufficient_evidence=True,
            provider=provider,
        )

    if provider == "none":
        raw = _extractive_fallback(query, chunks)
    else:
        prompt = _build_prompt(query, chunks)
        try:
            if provider == "groq":
                text = _call_groq(prompt)
            elif provider == "gemini":
                text = _call_gemini(prompt)
            elif provider == "openai":
                text = _call_openai(prompt)
            elif provider == "mock":
                text = _call_mock(prompt, chunks)
            else:
                raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
            raw = _extract_json(text)
        except Exception as e:  # noqa: BLE001 - deliberately broad: any provider/parse failure falls back safely
            fallback = _extractive_fallback(query, chunks)
            fallback["answer"] = f"(LLM call failed: {e}. Showing extractive fallback.)\n\n" + fallback["answer"]
            raw = fallback

    citations = _verify_citations(raw.get("answer", ""), raw.get("citations", []), chunks_by_id)

    return GenerationResult(
        answer=raw.get("answer", ""),
        citations=citations,
        insufficient_evidence=bool(raw.get("insufficient_evidence", False)),
        provider=provider,
    )
