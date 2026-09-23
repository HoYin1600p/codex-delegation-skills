"""Check the bundled installation and guide provider setup without printing account data."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
CLAUDE_SETUP = "https://code.claude.com/docs/en/setup"
GROK_SETUP = "https://docs.x.ai/build/overview"


def run(args: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=45, check=False)


def grok_module():
    spec = importlib.util.spec_from_file_location("delegate_work_grok", ROOT / "scripts/grok_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capabilities(executable: Path, provider: str) -> None:
    args = [str(executable), "--help"]
    if provider == "grok":
        args.insert(1, "--no-auto-update")
    help_result = run(args, cwd=Path.home())
    required = {"--json-schema", "--resume", "--tools", "--permission-mode"}
    if provider == "claude":
        # Claude accepts --max-turns in print mode but hides it from --help.
        # The live smoke test exercises that flag; absence from help is not failure.
        required |= {"--strict-mcp-config", "--setting-sources", "--settings"}
    else:
        required |= {"--max-turns", "--no-subagents", "--prompt-file", "--disallowed-tools", "--sandbox"}
    missing = sorted(flag for flag in required if flag not in help_result.stdout)
    if help_result.returncode or missing:
        raise ValueError("Installed CLI lacks required bridge capabilities: " + ", ".join(missing))


def doctor(*, offline: bool = False, require_grok: bool = False) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, action: str = "", detail: str = ""):
        checks.append({"check": name, "status": "ready" if ok else "setup_required",
                       "detail": detail, "next_action": action if not ok else ""})

    add("python", sys.version_info >= (3, 11), "Use Python 3.11 or newer (Codex's bundled Python is suitable).")
    add("platform", platform.system() == "Windows", "This release is verified on native Windows only; macOS/WSL/Linux readiness is not claimed.")
    add("git", bool(shutil.which("git")), "Install Git for Windows: https://git-scm.com/downloads/win; reopen Codex afterward.")
    try:
        for name in ("task", "reply", "checkpoint"):
            json.loads((ROOT / f"schemas/{name}.schema.json").read_text(encoding="utf-8"))
        for name in ("claude_bridge.py", "grok_bridge.py"):
            if not (ROOT / "scripts" / name).is_file():
                raise ValueError("Missing launcher")
        from codex_claude_bridge import contracts, launcher
        from codex_claude_bridge.state import ensure_state_root
        grok = grok_module()
        ensure_state_root()
        add("bundled_runtime", True)
    except (ImportError, OSError, ValueError) as exc:
        add("bundled_runtime", False, "Reinstall the complete skill; use an empty external DELEGATE_WORK_STATE_DIR if state ownership conflicts.", str(exc))
        return {"status": "setup_required", "checks": checks, "provider_checks": "not_run"}
    if offline:
        ready = all(item["status"] == "ready" for item in checks)
        return {"status": "installation_ready" if ready else "setup_required", "checks": checks,
                "provider_checks": "not_run", "next_action": "Run doctor without --offline to verify your own provider login and capacity."}
    if not all(item["status"] == "ready" for item in checks):
        return {"status": "setup_required", "checks": checks, "provider_checks": "not_run"}
    try:
        from codex_claude_bridge.auth import inspect_preflight
        from codex_claude_bridge.usage import fetch_usage
        preflight = inspect_preflight()
        capabilities(preflight.claude_executable, "claude")
        add("claude_subscription", True, detail=preflight.version)
        # A read-only usage request. Doctor never refreshes by generating a model reply.
        usage = fetch_usage(claude_version=preflight.version)
        ready = usage["five_hour"]["remaining_percent"] > 0
        add("claude_capacity", ready, "Wait for the five-hour reset, or use the verified Grok fallback.",
            "available" if ready else "five_hour_exhausted")
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        add("claude_subscription", False,
            f"Install/update the native Claude Code CLI ({CLAUDE_SETUP}), then run claude auth login using your subscription. Resolve any billing override named in the detail; never print its value. For quota retrieval, this release requires the Windows CLI credential store.", str(exc))
    try:
        record = grok._preflight()
        capabilities(Path(record["executable"]), "grok")
        add("grok_subscription", True, detail=record["version"])
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        add("grok_subscription", False,
            f"For fallback, install/update native Grok Build ({GROK_SETUP}) and run grok login. Your account must list grok-4.7. Never configure an API key as a substitute.", str(exc))
    claude_ok = all(c["status"] == "ready" for c in checks if c["check"].startswith("claude"))
    grok_ok = all(c["status"] == "ready" for c in checks if c["check"].startswith("grok"))
    status = "claude_and_grok_ready" if claude_ok and grok_ok else ("claude_ready" if claude_ok and not require_grok else "setup_required")
    return {"status": status, "checks": checks, "provider_checks": "performed",
            "fallback_ready": grok_ok, "account_values_recorded": False}


def self_test() -> dict:
    from codex_claude_bridge.state import ensure_state_root
    state = ensure_state_root()
    scratch = state / "scratch"
    scratch.mkdir(exist_ok=True)
    if not shutil.which("git"):
        raise ValueError("Install Git before running self-test")
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=scratch) as temporary:
        parent = Path(temporary)
        repo = parent / "repository"
        repo.mkdir()
        git_env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for args in (["git", "init", "-q"],):
            result = run(args, cwd=repo, env=git_env)
            if result.returncode:
                raise ValueError("Git fixture initialization failed")
        (repo / "README.md").write_text("Return a friendly greeting.\n", encoding="utf-8")
        (repo / "greeting.py").write_text("def greet():\n    return 'Hello'\n", encoding="utf-8")
        (repo / "test_greeting.py").write_text("import unittest\nfrom greeting import greet\nclass TestGreeting(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet(), 'Hello')\n", encoding="utf-8")
        for args in (["git", "add", "."], ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false", "commit", "-qm", "Synthetic fixture"]):
            if run(args, cwd=repo, env=git_env).returncode:
                raise ValueError("Git fixture commit failed")
        task = json.loads((ROOT / "assets/task.template.json").read_text(encoding="utf-8"))
        task.update(repo_root=str(repo), base_commit=run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
                    validation_command=[sys.executable, "-m", "unittest", "-v"])
        task_path = parent / "task.json"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        for provider in ("claude", "grok"):
            result = run([sys.executable, "-B", str(ROOT / f"scripts/{provider}_bridge.py"), "check-task", "--task", str(task_path)])
            if result.returncode:
                raise ValueError(f"Bundled {provider} launcher failed the offline task check")
        from codex_claude_bridge.contracts import load_task
        from codex_claude_bridge.launcher import _work_args, _checkpoint_args
        parsed = load_task(task_path)
        _work_args(parsed, 2)
        _checkpoint_args("11111111-1111-4111-8111-111111111111")
        grok = grok_module()
        grok._args(parsed, repo, parent / "prompt.txt", 2, grok.REPLY_SCHEMA)
        if run([sys.executable, "-m", "unittest", "-v"], cwd=repo).returncode:
            raise ValueError("Independent fixture validation failed")
    return {"status": "passed", "provider_called": False, "checks": ["disposable Git fixture", "both packaged launchers", "task/reply/checkpoint schemas", "provider command construction", "independent validation"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--offline", action="store_true")
    d.add_argument("--require-grok", action="store_true")
    sub.add_parser("self-test")
    args = parser.parse_args()
    try:
        record = doctor(offline=args.offline, require_grok=args.require_grok) if args.command == "doctor" else self_test()
    except (ImportError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record = {"status": "setup_required", "detail": str(exc), "next_action": "Use the bundled references/setup.md; do not start provider work until checks pass."}
    print(json.dumps(record, indent=2))
    return 0 if record["status"] in {"passed", "installation_ready", "claude_ready", "claude_and_grok_ready"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
