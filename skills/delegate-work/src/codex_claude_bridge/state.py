"""Private runtime state, kept outside the distributable skill."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def installation_id() -> str:
    normalized = os.path.normcase(str(PACKAGE_ROOT.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def state_root() -> Path:
    override = os.environ.get("DELEGATE_WORK_STATE_DIR")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            raise ValueError("DELEGATE_WORK_STATE_DIR must be an absolute external directory")
        return root.resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return (codex_home / "delegate-work-state" / installation_id()[:16]).resolve()


def require_external_storage(root: Path) -> Path:
    root = root.expanduser().resolve()
    if root == PACKAGE_ROOT or PACKAGE_ROOT in root.parents:
        raise ValueError("Delegate Work state must be outside the installed skill")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("Private runtime storage must be outside every Git repository; choose an external directory")
    return root


def ensure_state_root() -> Path:
    root = require_external_storage(state_root())
    marker = root / ".delegate-work-state.json"
    expected = {"schema_version": 1, "application": "delegate-work", "installation_sha256": installation_id()}
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise ValueError("State directory belongs to another installation; select an empty external directory")
    else:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError("State directory is nonempty and unmarked; select an empty external directory")
        try:
            with marker.open("x", encoding="utf-8") as out:
                json.dump(expected, out, indent=2)
                out.write("\n")
        except FileExistsError:
            if json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise ValueError("State directory initialization conflict")
    return root
