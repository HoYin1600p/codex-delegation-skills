from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .process import run_process


OVERRIDE_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_API_KEY_HELPER",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preflight:
    claude_executable: Path
    version: str
    logged_in: bool
    auth_method: str | None
    api_provider: str | None
    subscription_type: str | None
    conflicting_overrides: tuple[str, ...]

    def public_record(self) -> dict[str, Any]:
        return {
            "claude_executable": str(self.claude_executable),
            "version": self.version,
            "logged_in": self.logged_in,
            "auth_method": self.auth_method,
            "api_provider": self.api_provider,
            "subscription_type": self.subscription_type,
            "conflicting_overrides": list(self.conflicting_overrides),
            "identity_fields_recorded": False,
        }


def _settings_override_names(settings_path: Path) -> set[str]:
    if not settings_path.exists():
        return set()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PreflightError(f"Claude settings are unreadable or invalid: {settings_path}")
    found: set[str] = set()
    env = data.get("env", {}) if isinstance(data, dict) else {}
    if isinstance(env, dict):
        for name in OVERRIDE_NAMES:
            if env.get(name):
                found.add(f"settings.env.{name}")

    def walk(value: Any, path: str = "settings") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key.lower() == "apikeyhelper" and child:
                    found.add(child_path)
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(data)
    return found


def _find_claude_executable() -> str | None:
    native_name = "claude.exe" if os.name == "nt" else "claude"
    native = Path.home() / ".local" / "bin" / native_name
    if native.is_file():
        return str(native)
    return shutil.which("claude")


def inspect_preflight() -> Preflight:
    executable = _find_claude_executable()
    if not executable:
        raise PreflightError("Claude Code was not found on PATH")
    executable_path = Path(executable).resolve(strict=True)
    cwd = Path.home()

    version_result = run_process(executable_path, ["--version"], cwd=cwd, timeout_seconds=15)
    if version_result.exit_code != 0 or version_result.timed_out:
        raise PreflightError("Claude Code version check failed")
    version = version_result.stdout.strip() or version_result.stderr.strip()

    auth_result = run_process(executable_path, ["auth", "status"], cwd=cwd, timeout_seconds=15)
    if auth_result.exit_code != 0 or auth_result.timed_out:
        raise PreflightError("Claude Code authentication check failed")
    try:
        auth = json.loads(auth_result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError("Claude Code returned malformed authentication status") from exc

    conflicts = {name for name in OVERRIDE_NAMES if os.environ.get(name)}
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    conflicts.update(_settings_override_names(config_dir / "settings.json"))
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise PreflightError(f"billing/provider override present; no changes made: {names}")

    logged_in = auth.get("loggedIn") is True
    auth_method = auth.get("authMethod") if isinstance(auth.get("authMethod"), str) else None
    api_provider = auth.get("apiProvider") if isinstance(auth.get("apiProvider"), str) else None
    subscription = auth.get("subscriptionType") if isinstance(auth.get("subscriptionType"), str) else None
    if not logged_in:
        raise PreflightError("Claude Code is not logged in")
    if auth_method != "claude.ai" or api_provider != "firstParty" or not subscription:
        raise PreflightError("Claude Code is not using confirmed first-party subscription authentication")

    return Preflight(
        claude_executable=executable_path,
        version=version,
        logged_in=logged_in,
        auth_method=auth_method,
        api_provider=api_provider,
        subscription_type=subscription,
        conflicting_overrides=(),
    )
