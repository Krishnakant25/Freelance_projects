"""
Document parsing with extraction-quality checks.

The architecture doc's §6.2 point was that RAG projects fail at parsing more
often than at retrieval, and that you should inspect parser output rather
than assume it worked. Silent parse failures are the worst failure mode here:
a scanned PDF yields a handful of ligature artifacts, gets embedded, indexed,
and retrieved as gibberish — and nothing anywhere reports a problem.

So every parse returns quality warnings alongside the text, and ingestion
surfaces them instead of swallowing them.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}

# Heuristics for "this parsed badly". Deliberately conservative — these warn,
# they don't block ingestion, because a human should decide.
MIN_CHARS_PER_PAGE = 100          # below this, likely a scanned/image-only page
MIN_ALPHA_RATIO = 0.55            # below this, likely extraction garbage
MIN_AVG_WORD_LENGTH = 2.0
MAX_AVG_WORD_LENGTH = 15.0        # above this, words are probably run together


@dataclass
class ParsedDocument:
    text: str
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.warnings


def _assess_quality(text: str, page_count: int, source: str) -> list[str]:
    warnings: list[str] = []
    stripped = text.strip()

    if not stripped:
        warnings.append("Extracted no text at all — likely an image-only/scanned document needing OCR.")
        return warnings

    if page_count > 0:
        chars_per_page = len(stripped) / page_count
        if chars_per_page < MIN_CHARS_PER_PAGE:
            warnings.append(
                f"Only {chars_per_page:.0f} chars/page extracted ({len(stripped)} chars over "
                f"{page_count} pages) — likely scanned or image-heavy; consider OCR."
            )

    alpha = sum(c.isalpha() or c.isspace() for c in stripped)
    alpha_ratio = alpha / len(stripped)
    if alpha_ratio < MIN_ALPHA_RATIO:
        warnings.append(
            f"Only {alpha_ratio:.0%} of characters are letters/spaces — extraction may be "
            "garbled (encoding issue, or a table/figure-heavy layout)."
        )

    words = stripped.split()
    if words:
        avg_len = sum(len(w) for w in words) / len(words)
        if avg_len > MAX_AVG_WORD_LENGTH:
            warnings.append(
                f"Average 'word' length is {avg_len:.1f} chars — words are probably running "
                "together (missing spaces in the PDF text layer)."
            )
        elif avg_len < MIN_AVG_WORD_LENGTH:
            warnings.append(
                f"Average 'word' length is {avg_len:.1f} chars — text may be fragmented "
                "character-by-character."
            )

    # Repeated headers/footers across pages add noise to every chunk.
    if page_count > 2:
        lines = [ln.strip() for ln in stripped.split("\n") if ln.strip()]
        if lines:
            from collections import Counter

            common = Counter(lines).most_common(1)[0]
            if common[1] >= max(3, page_count * 0.6):
                warnings.append(
                    f"Line {common[0][:60]!r} repeats {common[1]}x — likely a running "
                    "header/footer that should be stripped before chunking."
                )
    return warnings


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("﻿", "").replace(" ", " ")
    # De-hyphenate words split across line breaks: "compre-\nhensive" -> "comprehensive"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # PDF extraction commonly emits list bullets as their own line, detached
    # from the item text. Measured on a real 5-page PDF: 61 standalone bullet
    # lines. Left alone they get embedded as content and become chunk noise.
    # Re-attach the marker to the following text, then drop any still orphaned.
    text = re.sub(r"^([•●○▪◦‣])[ \t]*\n[ \t]*(\S)", r"\1 \2", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*[•●○▪◦‣][ \t]*$\n?", "", text, flags=re.MULTILINE)
    # Collapse 3+ blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Trim trailing spaces per line
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _parse_pdf(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "PDF support requires pypdf. Install with: pip install pypdf"
        ) from e

    reader = PdfReader(str(path))
    metadata = {}
    try:
        info = reader.metadata or {}
        metadata = {
            "title": str(info.get("/Title", "") or ""),
            "author": str(info.get("/Author", "") or ""),
        }
    except Exception:  # noqa: BLE001 - metadata is best-effort, never fatal
        pass

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return ParsedDocument(
                text="",
                page_count=len(reader.pages),
                warnings=["PDF is encrypted and could not be decrypted with an empty password."],
                metadata=metadata,
            )

    pages = []
    failed_pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001 - one bad page shouldn't lose the document
            failed_pages.append(i)
            pages.append("")
            logger.warning("Failed to extract page %d of %s: %s", i, path.name, e)

    text = _clean_text("\n\n".join(pages))
    warnings = _assess_quality(text, len(reader.pages), str(path))
    if failed_pages:
        warnings.append(f"Text extraction raised on page(s): {failed_pages}")

    return ParsedDocument(
        text=text, page_count=len(reader.pages), warnings=warnings, metadata=metadata
    )


def _parse_text(path: Path) -> ParsedDocument:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _clean_text(raw)
    warnings = []
    if "�" in raw:
        warnings.append("File contained undecodable bytes (replaced) — check the source encoding.")
    warnings += _assess_quality(text, page_count=0, source=str(path))
    return ParsedDocument(text=text, page_count=0, warnings=warnings)


def parse_file(path: Path) -> ParsedDocument:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type {suffix!r}. Supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    if suffix == ".pdf":
        return _parse_pdf(path)
    return _parse_text(path)
