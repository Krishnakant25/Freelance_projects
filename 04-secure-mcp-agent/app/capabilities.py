"""
Capability grants — the security primitive this whole project is built on.

Architecture doc §6.3: string-matching allow-lists are famously porous.
`bash -c "..."` wraps anything; path allow-lists fall to `../`, symlinks, and
absolute-vs-relative confusion. And because a model chooses the arguments, it
will eventually find a phrasing that passes your regex — not maliciously, just
by exploring.

So policy here is enforced by POSSESSION, not inspection. A tool receives a
`Capabilities` object. If it doesn't hold `fs_write` for a path, there is no
code path that writes — nothing to bypass, no string to outsmart. The check
isn't "is this argument allowed?" but "do I have this capability at all?".

Path handling is the one place a string check remains unavoidable (the caller
supplies a path), so it is done the safe way: canonicalize FIRST — resolve
symlinks, make absolute — then verify containment. Never compare raw strings.
"""
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class CapabilityError(PermissionError):
    """Raised when an operation is attempted without the capability for it.

    A PermissionError subclass so it can't be accidentally swallowed by a
    generic `except Exception` that was meant for I/O errors.
    """


class Capability(str, Enum):
    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    FS_DELETE = "fs_delete"
    NET_EGRESS = "net_egress"
    EXEC_PROCESS = "exec_process"
    SECRET_READ = "secret_read"


@dataclass(frozen=True)
class Capabilities:
    """An immutable grant. Frozen deliberately: a capability set must not be
    widened after it's handed to a tool, or the guarantee evaporates. Widening
    requires constructing a NEW object, which is visible in code review.
    """
    granted: frozenset[Capability] = field(default_factory=frozenset)
    # Filesystem roots this grant may touch. Canonicalized at construction.
    fs_roots: tuple[Path, ...] = ()
    # Hosts egress is permitted to. Default-deny: an empty tuple means no
    # network at all, which is the correct default (doc §6.1: cutting egress is
    # "the single highest-value control and it's free").
    egress_hosts: tuple[str, ...] = ()
    # Human-readable label used in audit records and approval prompts.
    label: str = "unnamed"

    @staticmethod
    def none(label: str = "no-capabilities") -> "Capabilities":
        return Capabilities(label=label)

    def has(self, cap: Capability) -> bool:
        return cap in self.granted

    def require(self, cap: Capability, detail: str = "") -> None:
        if cap not in self.granted:
            raise CapabilityError(
                f"capability {cap.value!r} not granted to {self.label!r}"
                + (f" ({detail})" if detail else "")
            )

    # --- filesystem ------------------------------------------------------

    def resolve_path(self, requested: str | Path, *, for_write: bool = False) -> Path:
        """Canonicalizes a path and verifies it is inside a granted root.

        Order matters and is the whole point:
          1. ANCHOR a relative path to a granted root — never to the process's
             current working directory.
          2. Make absolute and resolve symlinks, so `../../etc` and a symlink
             pointing outside both become their real target.
          3. THEN check containment against the canonicalized roots.

        Checking before resolving is the classic mistake — `workspace/../../etc`
        starts with `workspace/` as a string while pointing somewhere else.

        Step 1 is a fix for a subtler problem found in testing: resolving a
        relative path against `os.getcwd()` makes the security boundary depend
        on WHERE THE PROCESS WAS STARTED. A relative path would resolve inside
        the sandbox when launched from one directory and outside it from
        another — legitimate paths failing in some deployments, and the
        containment check exercising a different branch than intended. A
        capability boundary must not be position-dependent, so relative paths
        are always interpreted against the grant, never the ambient cwd.
        """
        if not self.fs_roots:
            raise CapabilityError(f"{self.label!r} holds no filesystem roots")

        requested_path = Path(requested)
        # Anchor relative paths to each granted root in turn. Absolute paths are
        # used as-is (and then containment-checked like everything else).
        candidates = (
            [requested_path]
            if requested_path.is_absolute()
            else [root / requested_path for root in self.fs_roots]
        )

        attempted: list[str] = []
        for candidate in candidates:
            # For writes the file may not exist yet; canonicalize the parent so a
            # non-existent target is still resolved rather than skipped.
            if for_write and not candidate.exists():
                parent = candidate.parent.resolve(strict=False)
                resolved = parent / candidate.name
            else:
                resolved = candidate.resolve(strict=False)
            attempted.append(str(resolved))

            for root in self.fs_roots:
                try:
                    resolved.relative_to(root)
                    return resolved
                except ValueError:
                    continue

        raise CapabilityError(
            f"path {str(requested)!r} (resolved to {attempted}) is outside the granted roots "
            f"{[str(r) for r in self.fs_roots]} for {self.label!r}"
        )

    # --- network ---------------------------------------------------------

    def check_egress(self, url: str) -> str:
        """Verifies a URL's host is allow-listed, and refuses SSRF targets.

        Two separate concerns:
          - The host must be explicitly allowed (default-deny).
          - Even an allowed hostname must not resolve to a private/loopback/
            link-local address. Otherwise an attacker who controls DNS for an
            allow-listed domain can point it at 169.254.169.254 (cloud metadata)
            or 127.0.0.1 and reach services the sandbox was never meant to see.
        """
        self.require(Capability.NET_EGRESS, f"attempted request to {url}")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise CapabilityError(f"only http/https egress is permitted, got {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise CapabilityError(f"could not determine host from {url!r}")

        allowed = any(
            host == permitted or host.endswith("." + permitted)
            for permitted in self.egress_hosts
        )
        if not allowed:
            raise CapabilityError(
                f"egress to {host!r} is not allow-listed for {self.label!r} "
                f"(allowed: {list(self.egress_hosts) or 'none'})"
            )

        _assert_public_address(host)
        return host

    def with_label(self, label: str) -> "Capabilities":
        return Capabilities(
            granted=self.granted,
            fs_roots=self.fs_roots,
            egress_hosts=self.egress_hosts,
            label=label,
        )

    def describe(self) -> dict:
        return {
            "label": self.label,
            "granted": sorted(c.value for c in self.granted),
            "fs_roots": [str(p) for p in self.fs_roots],
            "egress_hosts": list(self.egress_hosts),
        }


def _assert_public_address(host: str) -> None:
    """Refuses hosts that resolve to private, loopback, or link-local ranges.

    169.254.169.254 is the cloud metadata endpoint — reaching it from a
    sandbox usually means instance credentials. This check is why an
    allow-listed hostname isn't sufficient on its own.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable host: let the request itself fail rather than guessing.
        return
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise CapabilityError(
                f"host {host!r} resolves to non-public address {addr} — refused "
                "(protects cloud metadata and internal services from SSRF)"
            )


def build(
    label: str,
    caps: Iterable[Capability] = (),
    fs_roots: Iterable[str | Path] = (),
    egress_hosts: Iterable[str] = (),
) -> Capabilities:
    """Constructs a grant, canonicalizing roots up front.

    Roots are resolved here — once — so every later containment check compares
    canonical paths to canonical paths.
    """
    resolved_roots = []
    for root in fs_roots:
        p = Path(root).resolve(strict=False)
        p.mkdir(parents=True, exist_ok=True)
        resolved_roots.append(p)

    granted = frozenset(caps)
    if Capability.NET_EGRESS in granted and not tuple(egress_hosts):
        # A net capability with no allow-list is almost certainly a mistake, and
        # a silent one — it would look permissive while permitting nothing.
        logger.warning(
            "capability set %r grants NET_EGRESS with an empty host allow-list; "
            "all requests will be refused", label,
        )

    return Capabilities(
        granted=granted,
        fs_roots=tuple(resolved_roots),
        egress_hosts=tuple(egress_hosts),
        label=label,
    )
