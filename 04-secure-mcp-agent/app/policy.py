"""
Risk-tiered policy engine and approval flow.

Architecture doc §6.5: "Approvals must be RARE enough to stay meaningful."
Gating everything destructive means users click approve reflexively within a
day, and the gate becomes theatre while still being slow enough that people
ask you to turn it off.

So actions fall into four tiers:

  AUTO_ALLOW    read-only / reversible — no prompt, just audited
  UNDOABLE      writes with a recorded undo — no prompt, because a cheap undo
                beats an expensive approval (doc §6.5)
  NEEDS_APPROVAL the narrow irreversible middle: deletes, sends, payments,
                credential access, prod writes
  FORBIDDEN     never permitted regardless of who asks

The prompt for NEEDS_APPROVAL shows the CONCRETE consequence — the real path,
the actual recipient, the specific diff — not "Agent wants to run a tool."
An approval you can't evaluate is a rubber stamp with extra steps.
"""
import fnmatch
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from . import audit, config

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    AUTO_ALLOW = "auto_allow"
    UNDOABLE = "undoable"
    NEEDS_APPROVAL = "needs_approval"
    FORBIDDEN = "forbidden"


class Decision(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"


@dataclass
class PolicyResult:
    decision: Decision
    tier: Tier
    reason: str
    # Shown verbatim in the approval prompt. Must describe the ACTUAL effect.
    consequence: str = ""
    approval_id: str = ""
    undo_token: str = ""


# Default tiering. Tools declare their own tier; this maps the ones that vary
# by argument (a delete is worse than a read even on the same tool).
_DEFAULT_TIERS: dict[str, Tier] = {
    "fs.read": Tier.AUTO_ALLOW,
    "fs.list": Tier.AUTO_ALLOW,
    "fs.write": Tier.UNDOABLE,
    "fs.delete": Tier.NEEDS_APPROVAL,
    "net.fetch": Tier.AUTO_ALLOW,      # already constrained by the egress allow-list
    "exec.run": Tier.NEEDS_APPROVAL,
    "secret.read": Tier.FORBIDDEN,     # no legitimate agent path reads raw secrets
}

# Patterns that are forbidden regardless of tool tier. Kept small on purpose:
# this is a backstop for catastrophic targets, NOT the primary control (the
# capability system is). A long blocklist here would be a sign the capability
# scoping is too loose.
_FORBIDDEN_PATH_PATTERNS = [
    "*/.env", "*/.env.*", "*/.git/config", "*/.ssh/*", "*/id_rsa*",
    "*/.aws/*", "*/.kube/*", "*credentials*", "*/keys.json",
]


@dataclass
class SessionGrant:
    """A time-boxed approval covering repeated instances of the same action.

    Doc §6.5: session-scoped grants ("allow writes to this directory for this
    task") instead of per-call prompts. This is what keeps approvals rare
    without making them meaningless.
    """
    tool: str
    scope: str
    granted_by: str
    granted_at: float
    ttl_seconds: int

    def covers(self, tool: str, scope: str, now: float) -> bool:
        if now - self.granted_at > self.ttl_seconds:
            return False
        if self.tool != tool:
            return False
        return self.scope == "*" or fnmatch.fnmatch(scope, self.scope)


class PolicyEngine:
    def __init__(self, actor: str = "agent", attended: bool = False):
        self.actor = actor
        # `attended` = a human is present and able to answer a prompt. When
        # False (batch, CI, scheduled run) an irreversible action has nobody to
        # approve it, so it fails CLOSED.
        self.attended = attended
        self._grants: list[SessionGrant] = []
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._call_count = 0
        self.last_used = time.time()

    def begin_task(self) -> None:
        """Starts a new task, resetting the tool-call budget.

        MAX_TOOL_CALLS_PER_TASK bounds a runaway agent LOOP inside a single
        task. It is not a quota on how much work an actor may ever do — that
        is the rate limiter's job, at a different layer. Without this reset an
        engine reused across tasks (as the API does, one per actor) silently
        becomes a lifetime quota: the actor is denied everything after 50
        calls, with a message blaming a runaway loop, until the process
        restarts.
        """
        with self._lock:
            self._call_count = 0
            self.last_used = time.time()

    # --- tiering ---------------------------------------------------------

    def tier_for(self, tool: str, args: dict) -> tuple[Tier, str]:
        tier = _DEFAULT_TIERS.get(tool, Tier.NEEDS_APPROVAL)
        reason = f"default tier for {tool}"

        target = str(args.get("path") or args.get("url") or args.get("command") or "")
        if target:
            for pattern in _FORBIDDEN_PATH_PATTERNS:
                if fnmatch.fnmatch(target.replace("\\", "/"), pattern):
                    return Tier.FORBIDDEN, f"target matches forbidden pattern {pattern!r}"

        # Unknown tools default to NEEDS_APPROVAL rather than allow. Deny-by-
        # default matters most for the case nobody anticipated.
        if tool not in _DEFAULT_TIERS:
            reason = f"{tool!r} is not a known tool — defaulting to approval"
        return tier, reason

    # --- session grants --------------------------------------------------

    def grant_session(self, tool: str, scope: str, granted_by: str,
                      ttl_seconds: Optional[int] = None) -> SessionGrant:
        grant = SessionGrant(
            tool=tool, scope=scope, granted_by=granted_by,
            granted_at=time.monotonic(),
            ttl_seconds=ttl_seconds or config.SESSION_GRANT_TTL_SECONDS,
        )
        with self._lock:
            self._grants.append(grant)
        audit.record(
            event="session_grant", actor=granted_by, decision="allowed",
            detail={"tool": tool, "scope": scope, "ttl_seconds": grant.ttl_seconds},
        )
        return grant

    def _covered_by_grant(self, tool: str, scope: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(g.covers(tool, scope, now) for g in self._grants)

    # --- the main check --------------------------------------------------

    def check(self, tool: str, args: dict, consequence: str = "") -> PolicyResult:
        """Evaluates an intended action. ALWAYS audited — including denials,
        which per doc §6.6 are the more interesting half of the data."""
        self._call_count += 1
        self.last_used = time.time()
        if self._call_count > config.MAX_TOOL_CALLS_PER_TASK:
            result = PolicyResult(
                decision=Decision.DENIED, tier=Tier.FORBIDDEN,
                reason=(f"tool-call budget for this task exceeded "
                        f"({config.MAX_TOOL_CALLS_PER_TASK}) - possible runaway loop"),
            )
            self._audit(tool, args, result)
            return result

        tier, reason = self.tier_for(tool, args)
        scope = str(args.get("path") or args.get("url") or args.get("command") or "*")

        if tier == Tier.FORBIDDEN:
            result = PolicyResult(decision=Decision.DENIED, tier=tier, reason=reason)
        elif tier in (Tier.AUTO_ALLOW, Tier.UNDOABLE):
            result = PolicyResult(decision=Decision.ALLOWED, tier=tier, reason=reason)
        elif not config.APPROVAL_REQUIRED:
            result = PolicyResult(
                decision=Decision.ALLOWED, tier=tier,
                reason="approval disabled by configuration",
            )
        elif self._covered_by_grant(tool, scope):
            result = PolicyResult(
                decision=Decision.ALLOWED, tier=tier,
                reason=f"covered by an active session grant for {tool}",
            )
        elif not self.attended and config.AUTO_DENY_WHEN_UNATTENDED:
            # Fail closed. An irreversible action with nobody to approve it must
            # not proceed just because no one was there to say no.
            result = PolicyResult(
                decision=Decision.DENIED, tier=tier,
                reason="irreversible action requested with no human attached — failing closed",
            )
        else:
            approval_id = f"apr_{int(time.time() * 1000)}_{self._call_count}"
            with self._lock:
                self._pending[approval_id] = {"tool": tool, "args": args, "scope": scope,
                                              "requested_by": self.actor}
            result = PolicyResult(
                decision=Decision.PENDING_APPROVAL, tier=tier, reason=reason,
                consequence=consequence or _describe_consequence(tool, args),
                approval_id=approval_id,
            )

        self._audit(tool, args, result)
        return result

    def resolve_approval(self, approval_id: str, approved: bool, approver: str,
                         grant_session: bool = False) -> PolicyResult:
        """Resolves a pending approval.

        SEPARATION OF DUTIES: the actor that requested the action cannot
        approve it. An agent (or the operator driving it) rubber-stamping its
        own destructive action is the human gate in name only — the same
        approval-fatigue failure doc §6.5 describes, reached by a shorter
        route. This is enforced here rather than only in the API so the CLI
        cannot route around it.
        """
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is not None and approver == pending.get("requested_by"):
                # Peeked, not popped: a refused self-approval must leave the
                # request pending so a second party can still act on it.
                # Popping here would let an attacker burn approvals by
                # self-approving them into oblivion.
                self_approval = True
            else:
                self_approval = False
                pending = self._pending.pop(approval_id, None)

        if self_approval:
            result = PolicyResult(
                decision=Decision.DENIED, tier=Tier.NEEDS_APPROVAL,
                reason=(f"separation of duties: {approver!r} requested this action and "
                        f"cannot approve it — a second party must"),
                approval_id=approval_id,
            )
            audit.record(
                event="self_approval_refused", actor=approver, decision="denied",
                detail={"approval_id": approval_id, "tool": pending["tool"]},
            )
            return result

        if pending is None:
            return PolicyResult(
                decision=Decision.DENIED, tier=Tier.FORBIDDEN,
                reason=f"unknown or already-resolved approval {approval_id!r}",
            )

        if approved and grant_session:
            self.grant_session(pending["tool"], pending["scope"], approver)

        result = PolicyResult(
            decision=Decision.ALLOWED if approved else Decision.DENIED,
            tier=Tier.NEEDS_APPROVAL,
            reason=f"{'approved' if approved else 'rejected'} by {approver}",
        )
        audit.record(
            event="approval_resolved", actor=approver,
            decision=result.decision.value,
            detail={"approval_id": approval_id, "tool": pending["tool"],
                    "args": pending["args"], "session_grant": grant_session},
        )
        return result

    def pending_approvals(self) -> dict:
        with self._lock:
            return dict(self._pending)

    def _audit(self, tool: str, args: dict, result: PolicyResult) -> None:
        audit.record(
            event="policy_check",
            actor=self.actor,
            decision=result.decision.value,
            detail={
                "tool": tool,
                "args": _redact(args),
                "tier": result.tier.value,
                "reason": result.reason,
                "attended": self.attended,
            },
        )


def _describe_consequence(tool: str, args: dict) -> str:
    """The concrete effect, for the approval prompt.

    Doc §6.5: show "the concrete diff, the actual recipient, the real file
    path — not 'Agent wants to run a tool.'"
    """
    if tool == "fs.delete":
        return f"PERMANENTLY DELETE the file: {args.get('path')}"
    if tool == "exec.run":
        return f"EXECUTE this command: {args.get('command')!r}"
    if tool == "fs.write":
        content = str(args.get("content", ""))
        preview = content[:120] + ("…" if len(content) > 120 else "")
        return f"WRITE {len(content)} bytes to {args.get('path')}\n  Preview: {preview!r}"
    if tool == "net.fetch":
        return f"SEND a request to {args.get('url')}"
    return f"{tool} with {args}"


_SECRET_KEYS = {"password", "token", "secret", "api_key", "apikey", "authorization", "key"}


def _redact(args: dict) -> dict:
    """Audit records must not themselves become a place secrets leak."""
    out = {}
    for k, v in (args or {}).items():
        if k.lower() in _SECRET_KEYS:
            out[k] = "***REDACTED***"
        elif isinstance(v, str) and len(v) > 500:
            out[k] = v[:500] + f"… ({len(v)} bytes total)"
        else:
            out[k] = v
    return out
