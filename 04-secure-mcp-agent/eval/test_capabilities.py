"""
Capability enforcement tests — the primary security boundary.

Architecture doc §6.3: string-matching allow-lists are porous. These tests
attack the capability system the way the doc says the naive version fails:
`../` traversal, symlinks, absolute paths, and capabilities that simply
weren't granted.

Run:  python eval/test_capabilities.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import config  # noqa: E402
from app.capabilities import Capabilities, Capability, CapabilityError, build  # noqa: E402
from app.tools import filesystem  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def read_only_caps() -> Capabilities:
    return build("test-reader", [Capability.FS_READ], [config.WORKSPACE_DIR])


def write_caps() -> Capabilities:
    return build("test-writer", [Capability.FS_READ, Capability.FS_WRITE], [config.WORKSPACE_DIR])


# --- 1. Ungranted capabilities have no code path ------------------------


def test_ungranted_capability_is_refused():
    print("\n[possession] a capability that wasn't granted cannot be exercised")
    _harness.reset_workspace()
    _harness.write_workspace_file("note.txt", "hello")
    caps = read_only_caps()

    check("read works (capability held)", filesystem.read_file(caps, "note.txt")["content"] == "hello")

    for label, fn in [
        ("write refused", lambda: filesystem.write_file(caps, "new.txt", "x")),
        ("delete refused", lambda: filesystem.delete_file(caps, "note.txt")),
    ]:
        raised = False
        try:
            fn()
        except CapabilityError:
            raised = True
        check(label, raised, "an ungranted capability was exercised")


def test_no_capabilities_means_nothing_works():
    print("\n[possession] an empty grant permits nothing at all")
    caps = Capabilities.none("empty")
    for label, cap in [("FS_READ", Capability.FS_READ), ("NET_EGRESS", Capability.NET_EGRESS)]:
        raised = False
        try:
            caps.require(cap)
        except CapabilityError:
            raised = True
        check(f"{label} refused", raised)


# --- 2. Path traversal cannot escape the granted root -------------------


def test_dotdot_traversal_blocked():
    """The classic. `workspace/../../etc/passwd` starts with 'workspace/' as a
    STRING while pointing somewhere else — which is why containment is checked
    after canonicalization, not before."""
    print("\n[traversal] ../ cannot escape the granted root")
    caps = read_only_caps()
    for attempt in [
        "../private/.env",
        "../../private/.env",
        "subdir/../../private/.env",
        "./../../private/.env",
        "..\\..\\private\\.env",
    ]:
        raised = False
        try:
            caps.resolve_path(attempt)
        except CapabilityError:
            raised = True
        check(f"blocked: {attempt!r}", raised, "traversal escaped the root")


def test_absolute_path_outside_root_blocked():
    print("\n[traversal] an absolute path outside the root is refused")
    caps = read_only_caps()
    raised = False
    try:
        caps.resolve_path(str(_harness.SECRET_FILE))
    except CapabilityError:
        raised = True
    check("absolute path to the secret refused", raised)

    # And the agent-level read must fail too, not just the resolver.
    raised2 = False
    try:
        filesystem.read_file(caps, str(_harness.SECRET_FILE))
    except CapabilityError:
        raised2 = True
    check("fs.read of the secret refused", raised2, "the agent read a file outside its root")


def test_symlink_escape_blocked():
    """A symlink inside the workspace pointing outside it. `.resolve()` follows
    it, so containment is checked against the real target."""
    print("\n[traversal] a symlink pointing outside the root is refused")
    _harness.reset_workspace()
    link = config.WORKSPACE_DIR / "escape_link"
    made = False
    try:
        link.symlink_to(_harness.SECRET_DIR, target_is_directory=True)
        made = True
    except (OSError, NotImplementedError):
        # Windows needs privileges for symlinks; skip cleanly rather than
        # reporting a pass we didn't actually verify.
        print("  SKIP  symlink creation unavailable on this platform/privileges")

    if made:
        caps = read_only_caps()
        raised = False
        try:
            caps.resolve_path("escape_link/.env")
        except CapabilityError:
            raised = True
        check("symlink escape refused", raised, "a symlink reached outside the granted root")


def test_paths_inside_root_still_work():
    """The containment check must not be so strict that legitimate paths fail —
    otherwise it's broken rather than secure."""
    print("\n[traversal] legitimate paths inside the root still work")
    _harness.reset_workspace()
    _harness.write_workspace_file("sub/deep/file.txt", "nested")
    caps = read_only_caps()
    check("nested read works", filesystem.read_file(caps, "sub/deep/file.txt")["content"] == "nested")
    check("normalised inner traversal works",
          filesystem.read_file(caps, "sub/../sub/deep/file.txt")["content"] == "nested")


def test_write_to_nonexistent_path_is_still_contained():
    """Writes target files that don't exist yet, so the resolver takes a
    different branch — it must still enforce containment."""
    print("\n[traversal] writes to new files are contained too")
    _harness.reset_workspace()
    caps = write_caps()

    result = filesystem.write_file(caps, "brand_new.txt", "ok")
    check("write inside root succeeds", Path(result["path"]).exists())

    raised = False
    try:
        filesystem.write_file(caps, "../private/evil.txt", "pwned")
    except CapabilityError:
        raised = True
    check("write outside root refused", raised, "a write escaped the granted root")
    check("no file was created outside the root",
          not (_harness.SECRET_DIR / "evil.txt").exists())


# --- 3. Egress is default-deny ------------------------------------------


def test_egress_denied_without_capability():
    print("\n[egress] no NET_EGRESS capability means no requests at all")
    caps = read_only_caps()
    raised = False
    try:
        caps.check_egress("https://example.com/data")
    except CapabilityError:
        raised = True
    check("request refused without the capability", raised)


def test_egress_allowlist_is_default_deny():
    print("\n[egress] only allow-listed hosts are permitted")
    caps = build("net", [Capability.FS_READ, Capability.NET_EGRESS],
                 [config.WORKSPACE_DIR], egress_hosts=["api.example.com"])

    check("allow-listed host permitted",
          caps.check_egress("https://api.example.com/v1/data") == "api.example.com")
    check("subdomain of an allow-listed host permitted",
          caps.check_egress("https://eu.api.example.com/x") == "eu.api.example.com")

    for url in [
        "https://evil.com/steal",
        "https://api.example.com.evil.com/steal",   # suffix-confusion attempt
        "http://attacker.io/collect",
    ]:
        raised = False
        try:
            caps.check_egress(url)
        except CapabilityError:
            raised = True
        check(f"refused: {url}", raised, "a non-allow-listed host was permitted")


def test_ssrf_private_addresses_refused():
    """An allow-listed hostname isn't enough: if it resolves to a private or
    link-local address, an attacker controlling DNS could reach cloud metadata
    (169.254.169.254) or internal services."""
    print("\n[egress] allow-listed hosts resolving to private ranges are refused")
    caps = build("net", [Capability.NET_EGRESS], [config.WORKSPACE_DIR],
                 egress_hosts=["localhost", "127.0.0.1", "169.254.169.254"])
    for url in ["http://localhost:8000/x", "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/"]:
        raised = False
        message = ""
        try:
            caps.check_egress(url)
        except CapabilityError as e:
            raised = True
            message = str(e)
        check(f"refused: {url}", raised, "an internal address was reachable")
        if raised:
            check("  ...explains it's a non-public address", "non-public" in message.lower(), message)


def test_non_http_schemes_refused():
    print("\n[egress] non-http(s) schemes are refused")
    caps = build("net", [Capability.NET_EGRESS], [config.WORKSPACE_DIR],
                 egress_hosts=["example.com"])
    for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"]:
        raised = False
        try:
            caps.check_egress(url)
        except CapabilityError:
            raised = True
        check(f"refused: {url}", raised)


# --- 4. Grants are immutable -------------------------------------------


def test_capabilities_are_immutable():
    """A grant that could be widened after being handed to a tool would make
    the guarantee meaningless."""
    print("\n[immutability] a grant cannot be widened in place")
    caps = read_only_caps()
    raised = False
    try:
        caps.granted = frozenset([Capability.FS_WRITE])  # type: ignore[misc]
    except Exception:
        raised = True
    check("mutating the grant raises", raised, "capabilities were mutable")
    check("still read-only", not caps.has(Capability.FS_WRITE))


def main():
    print("=" * 78)
    print("Capability enforcement: possession, traversal, egress")
    print("=" * 78)

    test_ungranted_capability_is_refused()
    test_no_capabilities_means_nothing_works()
    test_dotdot_traversal_blocked()
    test_absolute_path_outside_root_blocked()
    test_symlink_escape_blocked()
    test_paths_inside_root_still_work()
    test_write_to_nonexistent_path_is_still_contained()
    test_egress_denied_without_capability()
    test_egress_allowlist_is_default_deny()
    test_ssrf_private_addresses_refused()
    test_non_http_schemes_refused()
    test_capabilities_are_immutable()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Capabilities cannot be exceeded, and paths cannot escape their granted roots.")


if __name__ == "__main__":
    main()
