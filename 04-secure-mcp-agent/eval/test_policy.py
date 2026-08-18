"""
Policy, approval-tiering, and undo tests.

Architecture doc §6.5: "Approvals must be RARE enough to stay meaningful."
Gating everything destructive means users click approve reflexively within a
day, and the gate becomes theatre. These tests assert the tiering actually
keeps prompts rare, that the forbidden set is genuinely unreachable, and that
undo works — because a cheap undo is what lets writes avoid prompting at all.

Run:  python eval/test_policy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import audit, config  # noqa: E402
from app.agent import ExecutorAgent, executor_capabilities  # noqa: E402
from app.policy import Decision, PolicyEngine, Tier  # noqa: E402
from app.tools import filesystem  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


# --- Tiering keeps approvals rare --------------------------------------


def test_reversible_actions_do_not_prompt():
    """If reads and writes prompted, users would approve reflexively within a
    day and the gate would become theatre."""
    print("\n[tiering] reversible/read-only actions are auto-allowed")
    engine = PolicyEngine(actor="test", attended=True)
    for tool, args in [
        ("fs.read", {"path": "a.txt"}),
        ("fs.list", {"path": "."}),
        ("fs.write", {"path": "a.txt", "content": "x"}),
        ("net.fetch", {"url": "https://api.example.com/x"}),
    ]:
        result = engine.check(tool, args)
        check(f"{tool} auto-allowed", result.decision is Decision.ALLOWED,
              f"{result.decision} ({result.reason})")


def test_irreversible_actions_require_approval():
    print("\n[tiering] the irreversible middle requires approval")
    engine = PolicyEngine(actor="test", attended=True)
    for tool, args in [
        ("fs.delete", {"path": "important.txt"}),
        ("exec.run", {"command": "rm -rf ."}),
    ]:
        result = engine.check(tool, args)
        check(f"{tool} needs approval", result.decision is Decision.PENDING_APPROVAL,
              f"{result.decision} ({result.reason})")
        check(f"  ...has an approval id", bool(result.approval_id))


def test_approval_prompt_shows_concrete_consequence():
    """Doc §6.5: show 'the concrete diff, the actual recipient, the real file
    path — not "Agent wants to run a tool"'. An approval you can't evaluate is
    a rubber stamp with extra steps."""
    print("\n[tiering] the approval prompt states the ACTUAL effect")
    engine = PolicyEngine(actor="test", attended=True)

    result = engine.check("fs.delete", {"path": "/workspace/quarterly_report.xlsx"})
    check("names the real file", "quarterly_report.xlsx" in result.consequence, result.consequence)
    check("says what will happen", "DELETE" in result.consequence.upper(), result.consequence)

    result2 = engine.check("exec.run", {"command": "rm -rf /data"})
    check("shows the actual command", "rm -rf /data" in result2.consequence, result2.consequence)


def test_forbidden_actions_are_never_approvable():
    print("\n[tiering] the forbidden set cannot be approved by anyone")
    engine = PolicyEngine(actor="test", attended=True)
    result = engine.check("secret.read", {"path": "creds"})
    check("secret.read denied outright", result.decision is Decision.DENIED, str(result.decision))
    check("tier is FORBIDDEN", result.tier is Tier.FORBIDDEN, result.tier.value)
    check("no approval id offered", not result.approval_id)


def test_forbidden_paths_override_tool_tier():
    """A read is normally auto-allowed, but not of a credential file."""
    print("\n[tiering] catastrophic targets are forbidden regardless of tool tier")
    engine = PolicyEngine(actor="test", attended=True)
    for path in ["/home/u/.ssh/id_rsa", "/app/.env", "/root/.aws/credentials", "/srv/keys.json"]:
        result = engine.check("fs.read", {"path": path})
        check(f"denied: {path}", result.decision is Decision.DENIED, f"{result.decision}")


def test_unknown_tools_default_to_approval():
    """Deny-by-default matters most for the case nobody anticipated."""
    print("\n[tiering] an unknown tool requires approval rather than being allowed")
    engine = PolicyEngine(actor="test", attended=True)
    result = engine.check("some.new.tool", {"arg": 1})
    check("not auto-allowed", result.decision is not Decision.ALLOWED, str(result.decision))
    check("reason names the tool as unknown", "not a known tool" in result.reason, result.reason)


# --- Fail closed when unattended ---------------------------------------


def test_unattended_irreversible_action_fails_closed():
    """A batch/CI run has nobody to answer a prompt. The action must not
    proceed just because no one was there to say no."""
    print("\n[fail-closed] with no human attached, irreversible actions are denied")
    engine = PolicyEngine(actor="batch", attended=False)
    result = engine.check("fs.delete", {"path": "data.csv"})
    check("denied", result.decision is Decision.DENIED, str(result.decision))
    check("reason explains failing closed", "failing closed" in result.reason, result.reason)

    # Reversible work still proceeds unattended — otherwise batch use is dead.
    allowed = engine.check("fs.write", {"path": "out.txt", "content": "x"})
    check("reversible work still allowed unattended", allowed.decision is Decision.ALLOWED)


# --- Session grants keep approvals rare --------------------------------


def test_session_grant_prevents_repeat_prompting():
    """Doc §6.5: session-scoped grants instead of per-call prompts."""
    print("\n[session grants] one approval covers repeated matching actions")
    engine = PolicyEngine(actor="test", attended=True)

    first = engine.check("fs.delete", {"path": "tmp/a.log"})
    check("first delete prompts", first.decision is Decision.PENDING_APPROVAL)

    engine.resolve_approval(first.approval_id, approved=True, approver="alice", grant_session=True)

    second = engine.check("fs.delete", {"path": "tmp/a.log"})
    check("second identical delete is covered by the grant",
          second.decision is Decision.ALLOWED, f"{second.decision} ({second.reason})")
    check("reason cites the grant", "session grant" in second.reason, second.reason)


def test_session_grant_does_not_cover_unrelated_actions():
    """A grant that covered everything would be indistinguishable from turning
    approvals off."""
    print("\n[session grants] a grant does not widen to other targets or tools")
    engine = PolicyEngine(actor="test", attended=True)
    first = engine.check("fs.delete", {"path": "tmp/a.log"})
    engine.resolve_approval(first.approval_id, approved=True, approver="alice", grant_session=True)

    other_path = engine.check("fs.delete", {"path": "important/customers.db"})
    check("a different path still prompts",
          other_path.decision is Decision.PENDING_APPROVAL, str(other_path.decision))

    other_tool = engine.check("exec.run", {"command": "ls"})
    check("a different tool still prompts",
          other_tool.decision is Decision.PENDING_APPROVAL, str(other_tool.decision))


def test_rejected_approval_denies():
    print("\n[approvals] rejecting an approval denies the action")
    engine = PolicyEngine(actor="test", attended=True)
    pending = engine.check("fs.delete", {"path": "x.txt"})
    resolved = engine.resolve_approval(pending.approval_id, approved=False, approver="bob")
    check("denied", resolved.decision is Decision.DENIED, str(resolved.decision))
    check("names the rejector", "bob" in resolved.reason, resolved.reason)


def test_approval_cannot_be_replayed():
    print("\n[approvals] an approval id cannot be reused")
    engine = PolicyEngine(actor="test", attended=True)
    pending = engine.check("fs.delete", {"path": "x.txt"})
    engine.resolve_approval(pending.approval_id, approved=True, approver="alice")
    replay = engine.resolve_approval(pending.approval_id, approved=True, approver="alice")
    check("replay denied", replay.decision is Decision.DENIED, str(replay.decision))


# --- Undo is the primary safety mechanism ------------------------------


def test_write_is_undoable():
    """Doc §6.5: 'a cheap undo beats an expensive approval'. This is what lets
    fs.write sit in the UNDOABLE tier and not prompt."""
    print("\n[undo] a write can be reversed, which is why it needn't prompt")
    _harness.reset_workspace()
    caps = executor_capabilities()
    _harness.write_workspace_file("doc.txt", "ORIGINAL")

    result = filesystem.write_file(caps, "doc.txt", "OVERWRITTEN")
    check("write applied", filesystem.read_file(caps, "doc.txt")["content"] == "OVERWRITTEN")
    check("an undo token was issued", bool(result["undo_token"]))

    filesystem.undo(result["undo_token"])
    check("original content restored",
          filesystem.read_file(caps, "doc.txt")["content"] == "ORIGINAL")


def test_undo_removes_a_created_file():
    print("\n[undo] undoing a create removes the file")
    _harness.reset_workspace()
    caps = executor_capabilities()
    result = filesystem.write_file(caps, "new.txt", "content")
    path = Path(result["path"])
    check("file created", path.exists())
    check("flagged as a creation", result["created"])

    filesystem.undo(result["undo_token"])
    check("file removed by undo", not path.exists())


def test_delete_is_undoable():
    print("\n[undo] a delete can be reversed")
    _harness.reset_workspace()
    caps = executor_capabilities(allow_delete=True)
    _harness.write_workspace_file("gone.txt", "IMPORTANT DATA")

    result = filesystem.delete_file(caps, "gone.txt")
    check("file deleted", not (config.WORKSPACE_DIR / "gone.txt").exists())

    filesystem.undo(result["undo_token"])
    restored = config.WORKSPACE_DIR / "gone.txt"
    check("file restored", restored.exists())
    check("content intact", restored.read_text(encoding="utf-8") == "IMPORTANT DATA")


# --- Budget ------------------------------------------------------------


def test_tool_call_budget_is_enforced():
    """A runaway agent loop is bounded, not merely hoped against."""
    print("\n[budget] the per-task tool-call budget is enforced")
    engine = PolicyEngine(actor="test", attended=True)
    decisions = [engine.check("fs.read", {"path": "a.txt"}).decision
                 for _ in range(config.MAX_TOOL_CALLS_PER_TASK + 5)]
    check("early calls allowed", decisions[0] is Decision.ALLOWED)
    check("calls past the budget are denied", decisions[-1] is Decision.DENIED, str(decisions[-1]))


# --- Everything is audited ---------------------------------------------


def test_every_decision_is_audited_including_denials():
    print("\n[audit] policy decisions are recorded, denials included")
    audit.reset_for_tests()
    engine = PolicyEngine(actor="test", attended=True)
    engine.check("fs.read", {"path": "a.txt"})
    engine.check("secret.read", {"path": "creds"})
    engine.check("fs.delete", {"path": "x.txt"})

    records = audit.read_all()
    decisions = [r["decision"] for r in records if r["event"] == "policy_check"]
    check("allowed recorded", "allowed" in decisions, str(decisions))
    check("denied recorded", "denied" in decisions, str(decisions))
    check("pending recorded", "pending_approval" in decisions, str(decisions))
    check("chain intact", audit.verify().valid)


def test_secrets_are_redacted_in_audit_records():
    """The audit log must not itself become a place secrets leak."""
    print("\n[audit] secret-shaped arguments are redacted")
    audit.reset_for_tests()
    engine = PolicyEngine(actor="test", attended=True)
    engine.check("fs.write", {"path": "cfg.txt", "content": "x", "api_key": "sk-SUPERSECRET123"})

    raw = config.AUDIT_LOG_PATH.read_text(encoding="utf-8")
    check("the secret value is absent from the log", "sk-SUPERSECRET123" not in raw)
    check("redaction marker present", "REDACTED" in raw)


def main():
    print("=" * 78)
    print("Policy tiering, approvals, session grants, and undo")
    print("=" * 78)

    test_reversible_actions_do_not_prompt()
    test_irreversible_actions_require_approval()
    test_approval_prompt_shows_concrete_consequence()
    test_forbidden_actions_are_never_approvable()
    test_forbidden_paths_override_tool_tier()
    test_unknown_tools_default_to_approval()
    test_unattended_irreversible_action_fails_closed()
    test_session_grant_prevents_repeat_prompting()
    test_session_grant_does_not_cover_unrelated_actions()
    test_rejected_approval_denies()
    test_approval_cannot_be_replayed()
    test_write_is_undoable()
    test_undo_removes_a_created_file()
    test_delete_is_undoable()
    test_tool_call_budget_is_enforced()
    test_every_decision_is_audited_including_denials()
    test_secrets_are_redacted_in_audit_records()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Approvals stay rare and meaningful; reversible work proceeds; nothing goes unaudited.")


if __name__ == "__main__":
    main()
