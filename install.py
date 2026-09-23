"""Install only the skill's distributable files. Does not copy account state."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "skills" / "delegate-work"
SCRIPTS = {"claude_bridge.py", "grok_bridge.py", "setup.py"}
SCHEMAS = {"task.schema.json", "reply.schema.json", "checkpoint.schema.json"}


def package_files():
    for path in SOURCE.rglob("*"):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("The skill package must not contain symbolic links or junctions")
        if not path.is_file():
            continue
        rel = path.relative_to(SOURCE)
        parts = rel.parts
        allowed = (
            rel.as_posix() in {"SKILL.md", "agents/openai.yaml", "assets/task.template.json"}
            or (len(parts) == 2 and parts[0] == "references" and path.suffix == ".md")
            or (len(parts) == 2 and parts[0] == "scripts" and path.name in SCRIPTS)
            or (len(parts) == 2 and parts[0] == "schemas" and path.name in SCHEMAS)
            or (len(parts) == 3 and parts[:2] == ("src", "codex_claude_bridge") and path.suffix == ".py")
        )
        if allowed:
            yield path, rel


def install(destination: Path) -> dict:
    if sys.version_info < (3, 11):
        raise ValueError("Install Python 3.11 or later, then rerun this installer")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; preserve it and select a new --destination for review")
    required = [SOURCE / "SKILL.md", *(SOURCE / "scripts" / p for p in SCRIPTS),
                *(SOURCE / "schemas" / p for p in SCHEMAS), SOURCE / "src/codex_claude_bridge/state.py"]
    if not all(p.is_file() for p in required):
        raise ValueError("Incomplete download: clone/download the whole repository before installing")
    files = list(package_files())
    destination.mkdir(parents=True)
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return {"status": "installed", "skill": str(destination), "files": len(files),
            "next": "In Codex, ask: Use $delegate-work to finish setup and verify Claude and Grok readiness."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path.home() / ".agents/skills/delegate-work")
    args = parser.parse_args()
    try:
        record = install(args.destination)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "setup_required", "message": str(exc)}))
        return 2
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
