"""
Filesystem tools. Every operation goes through the capability object.

The security property: there is no code path here that touches a file without
first calling `caps.resolve_path()`, which canonicalizes and verifies
containment. A path that escapes the granted roots raises before any I/O — so
`../../etc/passwd`, a symlink pointing outside, and an absolute path are all
handled by the same single mechanism rather than three separate string checks.

Writes and deletes record an UNDO snapshot. Doc §6.5: "make undo the primary
safety mechanism where possible — a cheap undo beats an expensive approval."
That's what lets fs.write sit in the UNDOABLE tier and not prompt.
"""
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import config
from ..capabilities import Capabilities, Capability, CapabilityError

logger = logging.getLogger(__name__)


@dataclass
class UndoEntry:
    token: str
    operation: str            # write | delete
    path: str
    backup_path: Optional[str]
    existed_before: bool
    created_at: float = field(default_factory=time.time)


class UndoLog:
    """In-memory undo for reversible operations within a task.

    Backups live outside the granted workspace roots so an agent with fs_write
    cannot reach and corrupt its own undo history — the same reasoning that
    puts the audit log outside the workspace.
    """

    def __init__(self, backup_dir: Optional[Path] = None):
        self.backup_dir = Path(backup_dir or (config.ROOT_DIR / "data" / "undo"))
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, UndoEntry] = {}

    def snapshot(self, operation: str, path: Path) -> UndoEntry:
        token = f"undo_{uuid.uuid4().hex[:12]}"
        existed = path.exists()
        backup_path = None
        if existed:
            backup_path = self.backup_dir / f"{token}{path.suffix}"
            shutil.copy2(path, backup_path)
        entry = UndoEntry(
            token=token, operation=operation, path=str(path),
            backup_path=str(backup_path) if backup_path else None,
            existed_before=existed,
        )
        self._entries[token] = entry
        return entry

    def undo(self, token: str) -> bool:
        entry = self._entries.get(token)
        if entry is None:
            return False
        target = Path(entry.path)
        if entry.existed_before and entry.backup_path:
            shutil.copy2(entry.backup_path, target)
        elif not entry.existed_before and target.exists():
            # The operation created the file; undo removes it.
            target.unlink()
        del self._entries[token]
        return True

    def pending(self) -> list[UndoEntry]:
        return list(self._entries.values())


_undo_log: Optional[UndoLog] = None


def undo_log() -> UndoLog:
    global _undo_log
    if _undo_log is None:
        _undo_log = UndoLog()
    return _undo_log


# --- tools ----------------------------------------------------------------


def read_file(caps: Capabilities, path: str) -> dict:
    caps.require(Capability.FS_READ, f"read {path}")
    resolved = caps.resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if resolved.is_dir():
        raise IsADirectoryError(f"{path} is a directory; use fs.list")

    size = resolved.stat().st_size
    if size > config.MAX_FILE_READ_BYTES:
        raise ValueError(
            f"file is {size:,} bytes, exceeding the {config.MAX_FILE_READ_BYTES:,} byte read cap"
        )
    content = resolved.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(resolved),
        "bytes": size,
        "content": content,
        # Flagged so downstream code cannot forget where this came from. File
        # contents are UNTRUSTED — see agent.py and doc §6.1.
        "trust": "untrusted",
    }


def list_dir(caps: Capabilities, path: str = ".") -> dict:
    caps.require(Capability.FS_READ, f"list {path}")
    resolved = caps.resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"no such directory: {path}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")

    entries = []
    for child in sorted(resolved.iterdir()):
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "bytes": child.stat().st_size if child.is_file() else None,
        })
    # Filenames are attacker-controllable too — a file called
    # "IGNORE PREVIOUS INSTRUCTIONS.txt" is a real injection vector.
    return {"path": str(resolved), "entries": entries, "trust": "untrusted"}


def write_file(caps: Capabilities, path: str, content: str) -> dict:
    caps.require(Capability.FS_WRITE, f"write {path}")
    resolved = caps.resolve_path(path, for_write=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    entry = undo_log().snapshot("write", resolved)
    resolved.write_text(content, encoding="utf-8")
    return {
        "path": str(resolved),
        "bytes_written": len(content.encode("utf-8")),
        "undo_token": entry.token,
        "created": not entry.existed_before,
    }


def delete_file(caps: Capabilities, path: str) -> dict:
    caps.require(Capability.FS_DELETE, f"delete {path}")
    resolved = caps.resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if resolved.is_dir():
        raise IsADirectoryError("directory deletion is not supported")

    entry = undo_log().snapshot("delete", resolved)
    resolved.unlink()
    return {"path": str(resolved), "deleted": True, "undo_token": entry.token}


def undo(token: str) -> dict:
    ok = undo_log().undo(token)
    return {"token": token, "undone": ok}
