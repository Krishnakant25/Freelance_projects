"""
Create/list/revoke API keys.

The raw key is shown ONCE at creation and never stored — only its SHA-256
hash goes into keys.json. If it's lost, revoke and issue a new one.

Usage:
    python scripts/manage_keys.py create --name "acme-support" --groups support,public
    python scripts/manage_keys.py create --name "ingest-bot" --groups public --can-ingest
    python scripts/manage_keys.py list
    python scripts/manage_keys.py revoke --name "acme-support"
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.auth import generate_key, hash_key  # noqa: E402

KEYS_PATH = config.API_KEYS_PATH


def _load() -> dict:
    if not KEYS_PATH.exists():
        return {"keys": []}
    return json.loads(KEYS_PATH.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def cmd_create(args):
    data = _load()
    if any(k.get("name") == args.name for k in data["keys"]):
        print(f"ERROR: a key named {args.name!r} already exists. Revoke it first.")
        sys.exit(1)

    raw = generate_key()
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    data["keys"].append(
        {
            "name": args.name,
            "key_hash": hash_key(raw),
            "groups": groups,
            "can_ingest": args.can_ingest,
        }
    )
    _save(data)

    print(f"\nKey created for {args.name!r}")
    print(f"  groups:     {groups or '(public only)'}")
    print(f"  can_ingest: {args.can_ingest}")
    print(f"\n  API KEY (shown once, store it now):\n\n    {raw}\n")
    print(f"Saved hash to {KEYS_PATH}")


def cmd_list(args):
    data = _load()
    if not data["keys"]:
        print(f"No keys defined in {KEYS_PATH}")
        return
    print(f"\n{'NAME':25s} {'GROUPS':35s} {'INGEST':7s}")
    print("-" * 70)
    for k in data["keys"]:
        groups = ",".join(k.get("groups", [])) or "(public only)"
        print(f"{k.get('name', '?'):25s} {groups:35s} {str(k.get('can_ingest', False)):7s}")
    print(f"\n{len(data['keys'])} key(s) in {KEYS_PATH}")


def cmd_revoke(args):
    data = _load()
    before = len(data["keys"])
    data["keys"] = [k for k in data["keys"] if k.get("name") != args.name]
    if len(data["keys"]) == before:
        print(f"No key named {args.name!r} found.")
        sys.exit(1)
    _save(data)
    print(f"Revoked key {args.name!r}. Restart the API (or call /admin/reload-keys) to apply.")


def main():
    parser = argparse.ArgumentParser(description="Manage RAG API keys")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--groups", default="", help="Comma-separated ACL groups")
    p_create.add_argument("--can-ingest", action="store_true")
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
