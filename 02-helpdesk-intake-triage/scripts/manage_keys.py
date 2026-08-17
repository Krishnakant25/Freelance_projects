"""
Create/list/revoke API keys for the staff and admin endpoints.

The raw key is shown ONCE at creation and never stored — only its SHA-256
hash goes into keys.json. If it's lost, revoke and issue a new one.

Note: intake endpoints (/report etc.) are anonymous by design and need no
key. These keys are only for reading/mutating ticket data and admin actions.

Usage:
    python scripts/manage_keys.py create --name "helpdesk-team" --roles staff
    python scripts/manage_keys.py create --name "ops-lead" --roles admin
    python scripts/manage_keys.py list
    python scripts/manage_keys.py revoke --name "helpdesk-team"
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.auth import Role, generate_key, hash_key  # noqa: E402

KEYS_PATH = config.API_KEYS_PATH
VALID_ROLES = {r.value for r in Role}


def _load() -> dict:
    if not KEYS_PATH.exists():
        return {"keys": []}
    return json.loads(KEYS_PATH.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def cmd_create(args):
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    invalid = [r for r in roles if r not in VALID_ROLES]
    if invalid:
        print(f"ERROR: invalid role(s) {invalid}. Valid roles: {sorted(VALID_ROLES)}")
        sys.exit(1)
    if not roles:
        print(f"ERROR: at least one role required. Valid roles: {sorted(VALID_ROLES)}")
        sys.exit(1)

    data = _load()
    if any(k.get("name") == args.name for k in data["keys"]):
        print(f"ERROR: a key named {args.name!r} already exists. Revoke it first.")
        sys.exit(1)

    raw = generate_key()
    data["keys"].append({"name": args.name, "key_hash": hash_key(raw), "roles": roles})
    _save(data)

    print(f"\nKey created for {args.name!r}")
    print(f"  roles: {roles}")
    print(f"\n  API KEY (shown once, store it now):\n\n    {raw}\n")
    print("  Use it as an  X-API-Key  header on staff/admin endpoints.")
    print(f"\nSaved hash to {KEYS_PATH}")


def cmd_list(args):
    data = _load()
    if not data["keys"]:
        print(f"No keys defined in {KEYS_PATH}")
        return
    print(f"\n{'NAME':28s} {'ROLES':24s}")
    print("-" * 54)
    for k in data["keys"]:
        print(f"{k.get('name', '?'):28s} {','.join(k.get('roles', [])):24s}")
    print(f"\n{len(data['keys'])} key(s) in {KEYS_PATH}")


def cmd_revoke(args):
    data = _load()
    before = len(data["keys"])
    data["keys"] = [k for k in data["keys"] if k.get("name") != args.name]
    if len(data["keys"]) == before:
        print(f"No key named {args.name!r} found.")
        sys.exit(1)
    _save(data)
    print(f"Revoked key {args.name!r}. Restart the API (or POST /admin/reload-keys) to apply.")


def main():
    parser = argparse.ArgumentParser(description="Manage helpdesk API keys")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--roles", default="staff", help=f"Comma-separated: {sorted(VALID_ROLES)}")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--name", required=True)
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
