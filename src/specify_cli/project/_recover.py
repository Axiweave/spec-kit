"""Report and restore what a forcibly stopped rename, merge, move, or conversion left behind."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
from functools import partial
from pathlib import Path

from specify_cli.workspace import confined

from .naming import _rollback, _sibling_records, _write

_MOVE_STEPS = (
    "Compare the original files in this folder with the repository and the workspace. "
    "Move back any file that the repository lost, then delete the folder."
)
_CONVERT_STEPS = (
    "The workspace was not changed. If the repository lost its .specify folder, "
    "move this folder's .specify back into the repository. Then delete the folder."
)
_REPORT_ONLY = {"move-recovery": _MOVE_STEPS, "convert-recovery": _CONVERT_STEPS}


def _pid_alive(pid: int) -> bool:
    """Return True while process *pid* runs. Never send a signal: os.kill ends a Windows process."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: the process exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def _owner(record: Path, operation: str | None) -> int | None:
    """Return the owner pid that *record* names, or None when it cannot name one."""
    try:
        data = json.loads(record.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or (operation and data.get("operation") != operation):
        return None
    pid = data.get("pid")
    return pid if type(pid) is int and 0 < pid < 2**32 else None


def _manual(kind: str) -> str:
    return "Stop every writer, then " + (
        "delete the lock by hand." if kind.endswith("-lock") else "restore and delete the folder by hand."
    )


def scan(repository: Path, workspace: Path) -> list[dict]:
    """List leftover resources. Read only."""
    meta, specs = workspace / ".specify", workspace / "specs"
    found = [
        *(("naming-recovery", path, path / "manifest.json", None) for path in sorted(meta.glob("naming-recovery-*"))),
        ("naming-lock", meta / "naming-migration.lock", meta / "naming-migration.lock", "naming"),
        *(("merge-recovery", path, path / "journal.json", None) for path in sorted(specs.glob(".merge-specs-recovery-*"))),
        ("merge-lock", specs / ".merge-specs.lock", specs / ".merge-specs.lock", "merge-specs"),
        *(("move-recovery", path, None, None) for path in sorted(repository.glob(".specify-move-*"))),
        *(("convert-recovery", path, None, None) for path in sorted(repository.glob(".specify-convert-*"))),
    ]
    rows = []
    for kind, path, record, operation in found:
        if not os.path.lexists(path):
            continue
        pid = _owner(record, operation) if record else None
        owner = "unknown" if pid is None else "running" if _pid_alive(pid) else "stopped"
        if kind in _REPORT_ONLY:
            choices = [_REPORT_ONLY[kind]]
        elif owner == "stopped":
            choices = ["specify project recover --apply"]
        elif owner == "running":
            choices = [f"Wait until process {pid} ends, then run specify project recover --apply."]
        else:
            choices = [_manual(kind)]
        rows.append({"kind": kind, "path": str(path), "owner_pid": pid, "owner": owner, "choices": choices})
    return rows


def _refusal(row: dict) -> str | None:
    if row["owner"] == "running":
        return f"Owner process {row['owner_pid']} still runs: {row['path']}"
    if row["owner"] == "unknown":
        return f"Cannot confirm that the owner stopped: {row['path']}. {_manual(row['kind'])}"
    return None


def _content(path: Path) -> bytes | None:
    """Return the bytes of an ordinary file. None when the path is absent or is not an ordinary file."""
    if path.is_symlink() or not path.is_file():
        return None
    return path.read_bytes()


def _restore_naming(folder: Path, repository: Path, workspace: Path) -> list[str]:
    """Undo every marked step and the one step that may have started. Refuse a file changed after the stop.

    Every manifest path must stay in this project: renames in the workspace, files in the workspace,
    the repository, or the record of another local worktree, and saved bytes in this folder.
    """
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    root = workspace.resolve()
    if Path(manifest["workspace_root"]).resolve() != root:
        raise ValueError(f"The manifest names another workspace: {manifest['workspace_root']}")
    records = set(_sibling_records(repository.resolve()))

    def owned(value: str) -> Path:
        if Path(value) in records:
            return Path(value)
        try:
            return confined(root, value)
        except ValueError:
            return confined(repository, value)

    marked = {path.name for path in (folder / "done").iterdir()}
    if "applied" in marked:
        return []  # The rename finished. Only its backups remain.
    steps = [
        *((f"rename-{index}", entry) for index, entry in enumerate(manifest["renames"])),
        *((f"file-{index}", entry) for index, entry in enumerate(manifest["files"])),
    ]
    done: dict[str, list] = {"renames": [], "writes": [], "created": []}
    changed = []
    for name, entry in steps:
        if name.startswith("rename-"):
            source, target = confined(root, entry["from"]), confined(root, entry["to"])
            if os.path.lexists(source) == os.path.lexists(target):
                changed.append(target)
            done["renames"].append((source, target))
        else:
            path, new_path = owned(entry["path"]), owned(entry["new_path"])
            backup = confined(folder, entry["backup"]) if entry["backup"] else None
            location = new_path if os.path.lexists(new_path) else path
            if _content(location) not in {confined(folder, entry["new"]).read_bytes(), _content(backup) if backup else None}:
                changed.append(location)
            if backup:
                done["writes"].append((path, backup, entry["mode"], entry["mtime_ns"]))
            else:
                done["created"].append(path)
        if name not in marked:
            break  # Steps run in order: no step after the first unmarked one started.
    if changed:
        return [f"Changed after the stop, not restored: {path}" for path in changed]
    return [f"Not restored: {item['to']}: {item['message']}" for item in _rollback(done)]


def _restore_merge(folder: Path) -> list[str]:
    """Walk the started journal rows in reverse. Refuse a file changed after the stop."""
    root = folder.parent
    journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
    if journal.get("data_outcome") == "applied":
        return []  # The merge finished. Only its recovery data remains.
    actions, changed = [], []
    for row in reversed(journal["operations"]):
        if row["state"] not in {"attempting", "completed"}:
            continue
        path = confined(root, row["path"])
        if row["kind"] == "create_directory":
            actions.append((path, None, row))
            continue
        current = _content(path)
        original = confined(folder, row["original"]).read_bytes() if row.get("original") else None
        if current == original:
            continue  # Already restored, or never written.
        if current != confined(folder, row["written"]).read_bytes():
            changed.append(path)
        else:
            actions.append((path, original, row))
    if changed:
        return [f"Changed after the stop, not restored: {path}" for path in changed]
    for path, original, row in actions:
        if row["kind"] == "create_directory":
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        elif original is not None:
            _write(path, original, row["original_mode"], row["original_mtime_ns"])
        else:
            path.unlink()
    return []


def apply(repository: Path, workspace: Path) -> list[dict]:
    """Restore stopped rename and merge leftovers. Each row gets the refusals that kept it."""
    rows = scan(repository, workspace)
    naming = partial(_restore_naming, repository=repository, workspace=workspace)
    for family, restore in (("naming", naming), ("merge", _restore_merge)):
        lock = next((row for row in rows if row["kind"] == f"{family}-lock"), None)
        held = _refusal(lock) if lock else None  # A running or unknown lock owner may still write.
        folders = [row for row in rows if row["kind"] == f"{family}-recovery"]
        for row in folders:
            reason = _refusal(row) or held
            if reason:
                row["refusals"] = [reason]
                continue
            folder = Path(row["path"])
            try:
                row["refusals"] = restore(folder)
                if not row["refusals"]:
                    shutil.rmtree(folder)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row["refusals"] = [f"Not restored: {folder}: {exc}"]
        if lock:
            if held:
                lock["refusals"] = [held]
            elif any(row["refusals"] for row in folders):
                lock["refusals"] = [f"Kept until its recovery folder is restored: {lock['path']}"]
            else:
                try:
                    os.unlink(lock["path"])
                    lock["refusals"] = []
                except OSError as exc:
                    lock["refusals"] = [f"Not removed: {lock['path']}: {exc}"]
    for row in rows:
        if row["kind"] in _REPORT_ONLY:
            row["refusals"] = [f"Not changed: {row['path']}. {_REPORT_ONLY[row['kind']]}"]
    return rows
