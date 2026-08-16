"""
API-key authentication mapping callers to ACL groups.

THE POINT OF THIS MODULE: before it existed, /query accepted `user_groups`
in the request body. The SQL-level ACL filtering was correct, but the
identity it filtered on was whatever the caller typed — so anyone could send
{"user_groups": ["management"]} and read restricted documents. Correct
filtering on an unverified identity is not access control.

Groups are now resolved SERVER-SIDE from the API key and are never accepted
from the client. If you add another entry point (a new endpoint, a worker, a
CLI-over-network path), it must resolve groups through this module too —
never from user input.

Keys live in a JSON file outside the code (default: keys.json, gitignored),
hashed at rest so the file is not a plaintext credential dump.
"""
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, status

from . import config

logger = logging.getLogger(__name__)


@dataclass
class Principal:
    """An authenticated caller. `groups` is authoritative and server-derived."""
    name: str
    groups: list[str] = field(default_factory=list)
    can_ingest: bool = False


def hash_key(raw_key: str) -> str:
    """SHA-256 of the raw key. Stored instead of the key itself so a leaked
    keys.json doesn't hand over working credentials."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return "rag_" + secrets.token_urlsafe(32)


class KeyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._principals: dict[str, Principal] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.warning(
                "No API key file at %s — all authenticated endpoints will reject "
                "every request until you create one (see scripts/manage_keys.py).",
                self.path,
            )
            self._principals = {}
            return

        data = json.loads(self.path.read_text(encoding="utf-8"))
        principals = {}
        for entry in data.get("keys", []):
            key_hash = entry["key_hash"]
            principals[key_hash] = Principal(
                name=entry.get("name", "unnamed"),
                groups=entry.get("groups", []),
                can_ingest=entry.get("can_ingest", False),
            )
        self._principals = principals
        logger.info("Loaded %d API key(s) from %s", len(principals), self.path)

    def resolve(self, raw_key: str) -> Optional[Principal]:
        candidate = hash_key(raw_key)
        # compare_digest against each stored hash to avoid leaking which
        # prefix matched via timing. The dict lookup alone would be faster
        # but is timing-variable on the key material.
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


def require_principal(x_api_key: Optional[str] = Header(default=None)) -> Principal:
    """FastAPI dependency. Returns the authenticated Principal or raises 401.

    AUTH_ENABLED=false bypasses this for local development only. It logs loudly
    because shipping with it disabled would silently restore the original hole.
    """
    if not config.AUTH_ENABLED:
        logger.warning(
            "AUTH_ENABLED=false — request served without authentication. "
            "This must never be the case in a deployed environment."
        )
        return Principal(name="dev-bypass", groups=config.DEV_BYPASS_GROUPS, can_ingest=True)

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    principal = get_keystore().resolve(x_api_key)
    if principal is None:
        # Deliberately does not distinguish "unknown key" from "revoked key".
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return principal


def require_ingest_permission(principal: Principal) -> Principal:
    """Ingestion writes to the shared index and sets ACLs on documents —
    a strictly higher privilege than querying."""
    if not principal.can_ingest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key does not have ingest permission.",
        )
    return principal
