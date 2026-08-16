"""
Structure-aware fixed-size chunking with overlap (see architecture doc §6.4:
semantic chunking is skipped deliberately — heading-aware fixed-size chunking
with overlap is the better time/quality tradeoff at this scale).

Every chunk gets a contextual header prepended before embedding/storage
(§6.2), e.g. "[Source: Employee Handbook > Leave Policy]", so the chunk is
interpretable standalone instead of relying on surrounding chunks for meaning.
"""
import re
from dataclasses import dataclass

from . import config

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

# Heading detection for documents that have no markdown structure — chiefly
# PDFs. Found by testing against a real PDF: without this, a whole document
# collapses into a single section, the sliding window produces enormous
# chunks spanning unrelated topics, and retrieval returns "here is a third of
# the document" instead of the relevant part. Markdown-only heading detection
# silently degrades to no chunking structure at all on the format most client
# corpora actually arrive in.
_PLAIN_HEADING_PATTERNS = [
    # "01 Hero / Landing Section", "1. Introduction", "Section 3 - Pricing"
    re.compile(r"^\s*(?:\d{1,2}[.)]?\s+|\d{2}\s+)([A-Z][^\n]{2,70})$"),
    # "ARTICLE IV — TERMINATION", "TERMS AND CONDITIONS"
    re.compile(r"^\s*([A-Z][A-Z0-9 ,&'()/-]{4,70})$"),
    # Title Case line with no terminal punctuation, e.g. "Why a Team Portfolio Is Different"
    re.compile(r"^\s*((?:[A-Z][\w'’-]*)(?:\s+(?:[A-Z][\w'’-]*|of|the|a|an|and|or|for|to|in|on|with|is|are)){1,9})\s*$"),
]

# A "heading" that is really a list label or fragment — seen in practice
# ("Include" appearing 7x in one PDF). Splitting on these fragments the
# document worse than not splitting at all.
_HEADING_STOPWORDS = {"include", "includes", "note", "notes", "example", "examples", "tip", "tips"}


@dataclass
class Chunk:
    index: int
    section: str
    text: str       # with contextual header, embed/store this
    raw_text: str    # without header, show this to the user for citations


def _approx_tokens(text: str) -> int:
    # Cheap approximation (~0.75 tokens/word) — good enough for chunk sizing,
    # avoids pulling in a tokenizer dependency just for this.
    return max(1, int(len(text.split()) * 1.3))


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    if stripped.lower().rstrip(":") in _HEADING_STOPWORDS:
        return False
    # Body text ends in sentence punctuation; headings usually don't.
    if stripped[-1] in ".,;":
        return False
    # Bullets are list items, not headings.
    if stripped[0] in "•●○▪◦‣-*":
        return False
    words = stripped.split()
    if len(words) > 12:
        return False
    return any(p.match(stripped) for p in _PLAIN_HEADING_PATTERNS)


def _split_plain_text_by_headings(text: str) -> list[tuple[str, str]]:
    """Heading detection for documents with no markdown structure (PDFs)."""
    lines = text.split("\n")
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []

    for line in lines:
        if _looks_like_heading(line):
            if current_body:
                sections.append((current_title, current_body))
            current_title = line.strip()
            current_body = []
        else:
            current_body.append(line)
    if current_body:
        sections.append((current_title, current_body))

    out = [(title, "\n".join(body).strip()) for title, body in sections]
    out = [(t, b) for t, b in out if b]
    # If detection found almost nothing, it isn't working on this document —
    # fall back rather than emitting one giant mislabeled section.
    if len(out) < 2:
        return [("", text.strip())]
    return out


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Returns [(section_title, section_body), ...]. Uses markdown headings
    when present, otherwise falls back to structural heading detection for
    plain-text/PDF documents."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return _split_plain_text_by_headings(text)

    sections = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))

    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((title, body))
    return sections


def _sliding_window(words: list[str], target_tokens: int, overlap_tokens: int):
    # words-per-chunk approximated from the token target (see _approx_tokens).
    words_per_chunk = max(20, int(target_tokens / 1.3))
    overlap_words = max(0, int(overlap_tokens / 1.3))
    step = max(1, words_per_chunk - overlap_words)

    if len(words) <= words_per_chunk:
        yield words
        return

    i = 0
    while i < len(words):
        yield words[i : i + words_per_chunk]
        if i + words_per_chunk >= len(words):
            break
        i += step


def chunk_document(
    title: str,
    text: str,
    target_tokens: int = None,
    overlap_tokens: int = None,
) -> list[Chunk]:
    target_tokens = target_tokens or config.CHUNK_TARGET_TOKENS
    overlap_tokens = overlap_tokens or config.CHUNK_OVERLAP_TOKENS

    chunks: list[Chunk] = []
    idx = 0
    for section_title, body in _split_by_headings(text):
        if not body:
            continue
        words = body.split()
        for window in _sliding_window(words, target_tokens, overlap_tokens):
            raw = " ".join(window).strip()
            if not raw:
                continue
            header = f"[Source: {title}" + (f" > {section_title}]" if section_title else "]")
            chunks.append(
                Chunk(
                    index=idx,
                    section=section_title,
                    text=f"{header}\n{raw}",
                    raw_text=raw,
                )
            )
            idx += 1
    return chunks
