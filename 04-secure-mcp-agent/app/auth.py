"""
API-key authentication with roles.

TWO-TIER TRUST MODEL — the distinction is the point:

  INTAKE endpoints (/report, /report/file-anyway, /deflection/feedback) are
  ANONYMOUS BY DESIGN. They are the public submission surface, meant to be
  embedded in a chat widget, intranet page, or kiosk that anyone in the
  organisation can use. Requiring a per-user credential there would defeat
  the purpose, and they expose nothing: you can only submit your own text
  and read back a KB article or your own ticket id.

  STAFF endpoints (/tickets, acknowledge, resolve) expose EVERY ticket's
  contents — which in a helpdesk includes whatever users typed, up to and
  including credentials they shouldn't have pasted, security incident details,
  and personal information. They also mutate ticket state. These require a key.

  ADMIN endpoints (/admin/*) trigger alerting and inspect operational state.
  Separate role so a read-mostly staff key can't fire pages.

Keys are hashed (SHA-256) at rest, so keys.json is not a plaintext credential
dump. The raw key is shown once at creation and never stored.
"""
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, status

from . import config

logger = logging.getLogger(__name__)


class Role(str, Enum):
    OPERATOR = "operator"   # submit agent tasks, read audit
    APPROVER = "approver"   # approve pending irreversible actions
    ADMIN = "admin"         # tool approval/revocation, config

    # SEPARATION OF DUTIES: operator and approver are deliberately distinct.
    # The person who launched an agent task should not be the one who waves
    # through its irreversible actions — that collapses the human gate into a
    # formality, which is exactly the approval-fatigue failure doc 6.5 warns
    # about, just with extra steps. Enforced in require_approver().


@dataclass
class Principal:
    name: str
    roles: list[str] = field(default_factory=list)

    def has(self, role: Role) -> bool:
        # NOTE: admin does NOT imply approver here. In the other portfolio
        # projects admin implies everything, but separation of duties is the
        # point of this one — an admin key must be granted `approver`
        # explicitly if it is meant to approve actions.
        return role.value in self.roles


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return "hd_" + secrets.token_urlsafe(32)


class KeyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._principals: dict[str, Principal] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.warning(
                "No API key file at %s — staff/admin endpoints will reject every "
                "request until you create one: python scripts/manage_keys.py create "
                "--name <name> --roles operator",
                self.path,
            )
            self._principals = {}
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        principals = {}
        for entry in data.get("keys", []):
            principals[entry["key_hash"]] = Principal(
                name=entry.get("name", "unnamed"),
                roles=entry.get("roles", []),
            )
        self._principals = principals
        logger.info("Loaded %d API key(s) from %s", len(principals), self.path)

    def resolve(self, raw_key: str) -> Optional[Principal]:
        candidate = hash_key(raw_key)
        # compare_digest against each stored hash rather than a dict lookup, so
        # matching doesn't leak timing information about the key material.
        for stored_hash, principal in self._principals.items():
            if hmac.compare_digest(candidate, stored_hash):
                return principal
        return None

    def __len__(self) -> int:
        return len(self._principals)


_keystore: Optional[KeyStore] = None


def get_keystore() -> KeyStore:
    global _keystore
    if _keystore is None:
        _keystore = KeyStore(config.API_KEYS_PATH)
    return _keystore


def reload_keystore() -> None:
    global _keystore
    _keystore = None
    get_keystore()


def _authenticate(x_api_key: Optional[str]) -> Principal:
    if not config.AUTH_ENABLED:
        # Local development escape hatch. Logs loudly every request because
        # shipping with this off would silently restore the original gap:
        # staff endpoints readable by anyone who can reach the port.
        logger.warning(
            "AUTH_ENABLED=false — endpoint served WITHOUT authentication. "
            "This must never be the case in a deployed environment."
        )
        return Principal(name="dev-bypass",
                         roles=[Role.OPERATOR.value, Role.APPROVER.value, Role.ADMIN.value])

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    principal = get_keystore().resolve(x_api_key)
    if principal is None:
        # Deliberately does not distinguish unknown from revoked.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return principal


def require_operator(x_api_key: Optional[str] = Header(default=None)) -> Principal:
    """For endpoints that submit agent tasks or read the audit log."""
    principal = _authenticate(x_api_key)
    if not principal.has(Role.OPERATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This key does not have the 'operator' role.",
        )
    return principal


def require_approver(x_api_key: Optional[str] = Header(default=None)) -> Principal:
    """For approving pending irreversible actions.

    Deliberately a separate role from `operator`. See the note on Role: an
    operator approving their own agent's destructive action is the human gate
    in name only.
    """
    principal = _authenticate(x_api_key)
    if not principal.has(Role.APPROVER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This key does not have the 'approver' role. Approving an "
                   "irreversible action requires a key distinct from the one "
                   "that submitted the task (separation of duties).",
        )
    return principal


def require_admin(x_api_key: Optional[str] = Header(default=None)) -> Principal:
    """FastAPI dependency for endpoints that trigger alerts or change config."""
    principal = _authenticate(x_api_key)
    if not principal.has(Role.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This key does not have the 'admin' role.",
        )
    return principal
