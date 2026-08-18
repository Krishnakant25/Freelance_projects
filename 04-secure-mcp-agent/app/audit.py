"""
Tamper-evident audit log.

Architecture doc §6.6: "If that process is compromised or the agent has
filesystem write access, the log is editable by the thing it's supposed to be
auditing."

Two mechanisms, because the realistic threat here isn't a sophisticated
attacker — it's the agent itself having filesystem access, or a bug:

1. HASH CHAIN. Each record includes the hash of the previous record. Editing or
   deleting any entry breaks the chain from that point onward, and `verify()`
   reports exactly where. This doesn't PREVENT tampering — nothing file-based
   can — but it makes tampering *detectable*, which is the achievable property
   and the one that matters. A log you can silently rewrite provides no
   assurance while looking like it does.

2. OUT-OF-PROCESS WRITER. The log lives outside every granted filesystem root,
   so an agent with fs_write cannot reach it through the capability system at
   all. In production this becomes a separate service or write-only endpoint;
   the interface here is deliberately the same shape so that swap is a config
   change, not a rewrite.

DENIALS ARE LOGGED, NOT JUST SUCCESSES. Per the doc, they're the more
interesting half of the data: a spike in denied actions is the signal that
something upstream is wrong.
"""
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

GENESIS = "0" * 64


@dataclass
class AuditRecord:
    seq: int
    timestamp: str
    event: str
    actor: str
    decision: str          # allowed | denied | pending_approval | executed | failed
    detail: dict
    prev_hash: str
    record_hash: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "timestamp": self.timestamp,
                "event": self.event,
                "actor": self.actor,
                "decision": self.decision,
                "detail": self.detail,
                "prev_hash": self.prev_hash,
                "record_hash": self.record_hash,
            },
            sort_keys=True,
        )


def _compute_hash(seq: int, timestamp: str, event: str, actor: str,
                  decision: str, detail: dict, prev_hash: str) -> str:
    payload = json.dumps(
        {
            "seq": seq, "timestamp": timestamp, "event": event, "actor": actor,
            "decision": decision, "detail": detail, "prev_hash": prev_hash,
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log_path() -> Path:
    path = config.AUDIT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _last_record() -> Optional[dict]:
    path = _log_path()
    if not path.exists():
        return None
    last = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    return json.loads(last) if last else None


def record(event: str, actor: str, decision: str, detail: dict = None) -> AuditRecord:
    """Appends a record and returns it. Never raises into the caller's path —
    an audit failure must not become a reason to skip the security check that
    was about to be logged."""
    detail = detail or {}
    with _lock:
        try:
            prev = _last_record()
            seq = (prev["seq"] + 1) if prev else 1
            prev_hash = prev["record_hash"] if prev else GENESIS
            timestamp = datetime.now(timezone.utc).isoformat()

            record_hash = _compute_hash(seq, timestamp, event, actor, decision, detail, prev_hash)
            rec = AuditRecord(
                seq=seq, timestamp=timestamp, event=event, actor=actor,
                decision=decision, detail=detail, prev_hash=prev_hash,
                record_hash=record_hash,
            )
            path = _log_path()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(rec.to_json() + "\n")
                fh.flush()
                # fsync so a crash can't lose the record of an action that DID
                # happen — an audit gap around a crash is exactly when you most
                # need the record.
                os.fsync(fh.fileno())
            return rec
        except Exception:  # noqa: BLE001
            logger.exception("AUDIT WRITE FAILED for event=%s actor=%s", event, actor)
            raise


def read_all() -> list[dict]:
    path = _log_path()
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


@dataclass
class VerificationResult:
    valid: bool
    records_checked: int
    first_bad_seq: Optional[int] = None
    reason: str = ""

    def describe(self) -> str:
        if self.valid:
            return f"chain intact across {self.records_checked} record(s)"
        return f"CHAIN BROKEN at seq={self.first_bad_seq}: {self.reason}"


def verify() -> VerificationResult:
    """Walks the chain and reports the first inconsistency.

    Catches: edited fields (hash won't match), deleted records (seq gap and
    broken prev_hash link), and reordering.
    """
    records = read_all()
    if not records:
        return VerificationResult(valid=True, records_checked=0)

    expected_prev = GENESIS
    for i, rec in enumerate(records):
        expected_seq = i + 1
        if rec.get("seq") != expected_seq:
            return VerificationResult(
                valid=False, records_checked=i, first_bad_seq=rec.get("seq"),
                reason=f"sequence gap — expected {expected_seq}, found {rec.get('seq')} "
                       "(a record was deleted or reordered)",
            )
        if rec.get("prev_hash") != expected_prev:
            return VerificationResult(
                valid=False, records_checked=i, first_bad_seq=rec.get("seq"),
                reason="prev_hash does not match the previous record's hash "
                       "(a record was altered or removed)",
            )
        recomputed = _compute_hash(
            rec["seq"], rec["timestamp"], rec["event"], rec["actor"],
            rec["decision"], rec["detail"], rec["prev_hash"],
        )
        if recomputed != rec.get("record_hash"):
            return VerificationResult(
                valid=False, records_checked=i, first_bad_seq=rec.get("seq"),
                reason="record hash mismatch (this record's contents were edited)",
            )
        expected_prev = rec["record_hash"]

    return VerificationResult(valid=True, records_checked=len(records))


def stats() -> dict:
    records = read_all()
    decisions: dict[str, int] = {}
    events: dict[str, int] = {}
    for r in records:
        decisions[r["decision"]] = decisions.get(r["decision"], 0) + 1
        events[r["event"]] = events.get(r["event"], 0) + 1
    chain = verify()
    return {
        "total_records": len(records),
        "by_decision": decisions,
        "by_event": events,
        "chain_valid": chain.valid,
        "chain_detail": chain.describe(),
        # Denials are surfaced prominently: a spike is the signal that
        # something upstream is wrong.
        "denials": decisions.get("denied", 0),
    }


def reset_for_tests() -> None:
    """Test-only. Guarded so it can't run against a configured production path."""
    if not config.ALLOW_AUDIT_RESET:
        raise RuntimeError("audit reset is disabled (ALLOW_AUDIT_RESET=false)")
    path = _log_path()
    if path.exists():
        path.unlink()
