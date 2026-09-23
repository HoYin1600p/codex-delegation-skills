from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MODES = {"analyze", "implement", "review", "test"}
RISK_LEVELS = {"low", "medium", "high"}
CHECKPOINT_TURN_RESERVE = 2
TASK_FIELDS = {
    "task_id",
    "repo_root",
    "base_commit",
    "mode",
    "objective",
    "context_paths",
    "forbidden_context",
    "allowed_changed_paths",
    "acceptance_criteria",
    "plan_status",
    "risk",
    "review_required",
    "locked_decisions",
    "stop_conditions",
    "model",
    "max_turns",
    "max_total_turns",
    "max_extensions",
    "timeout_seconds",
    "validation_command",
    "validation_timeout_seconds",
    "allow_subagents",
    "require_subscription_auth",
}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    task_id: str
    repo_root: Path
    base_commit: str
    mode: str
    objective: str
    context_paths: tuple[str, ...]
    forbidden_context: tuple[str, ...]
    allowed_changed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    plan_status: str
    risk: str
    review_required: bool
    locked_decisions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    model: str | None
    max_turns: int
    max_total_turns: int | None
    max_extensions: int | None
    timeout_seconds: int
    validation_command: tuple[str, ...] | None
    validation_timeout_seconds: int
    allow_subagents: bool
    require_subscription_auth: bool
    raw: dict[str, Any]


def _string_list(raw: dict[str, Any], key: str, *, required: bool = True) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{key} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _relative_path(value: str, key: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(f"{key} contains a path outside the repository: {value!r}")
    if ":" in path.parts[0]:
        raise ContractError(f"{key} contains an absolute Windows path: {value!r}")
    wildcard_chars = {"*", "?", "[", "]"}
    if any(char in normalized for char in wildcard_chars):
        is_directory_boundary = key == "allowed_changed_paths" and normalized.endswith("/**")
        prefix = normalized[:-3] if is_directory_boundary else normalized
        if not is_directory_boundary or any(char in prefix for char in wildcard_chars):
            raise ContractError(f"{key} supports only a trailing /** directory boundary: {value!r}")
    return path.as_posix()


def load_task(path: str | Path) -> Task:
    task_path = Path(path).expanduser().resolve(strict=True)
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"task file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("task file must contain a JSON object")
    return validate_task(raw)


def validate_task(raw: dict[str, Any]) -> Task:
    unknown = sorted(set(raw) - TASK_FIELDS)
    if unknown:
        raise ContractError(f"task contains unsupported fields: {', '.join(unknown)}")

    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ContractError("task_id must match ^[a-z0-9][a-z0-9._-]{0,63}$")

    repo_value = raw.get("repo_root")
    if not isinstance(repo_value, str) or not Path(repo_value).is_absolute():
        raise ContractError("repo_root must be an absolute path")
    repo_root = Path(repo_value).expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise ContractError("repo_root must be a directory")

    base_commit = raw.get("base_commit")
    if not isinstance(base_commit, str) or not COMMIT_RE.fullmatch(base_commit):
        raise ContractError("base_commit must be a full 40-character Git commit ID")

    mode = raw.get("mode")
    if mode not in MODES:
        raise ContractError(f"mode must be one of {sorted(MODES)}")

    objective = raw.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 10_000:
        raise ContractError("objective must be a non-empty string no longer than 10,000 characters")

    context_paths = tuple(_relative_path(item, "context_paths") for item in _string_list(raw, "context_paths"))
    allowed_changed_paths = tuple(
        _relative_path(item, "allowed_changed_paths")
        for item in _string_list(raw, "allowed_changed_paths")
    )
    forbidden_context = _string_list(raw, "forbidden_context")
    acceptance_criteria = _string_list(raw, "acceptance_criteria")
    plan_status = raw.get("plan_status", "READY")
    if plan_status != "READY":
        raise ContractError("plan_status must be READY before delegation")
    risk = raw.get("risk", "medium")
    if risk not in RISK_LEVELS:
        raise ContractError(f"risk must be one of {sorted(RISK_LEVELS)}")
    review_required = raw.get("review_required", True)
    if not isinstance(review_required, bool):
        raise ContractError("review_required must be a boolean")
    locked_decisions = _string_list(raw, "locked_decisions", required=False)
    stop_conditions = _string_list(raw, "stop_conditions", required=False)

    if mode in {"analyze", "review"} and allowed_changed_paths:
        raise ContractError(f"{mode} tasks cannot allow changed paths")
    if mode == "implement" and not allowed_changed_paths:
        raise ContractError("implement tasks require at least one allowed_changed_path")

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ContractError("model must be null or a non-empty string")

    max_turns = raw.get("max_turns")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 12:
        raise ContractError("max_turns must be an integer from 1 through 12")
    # Accepted only so preserved pre-unbounded task contracts remain readable.
    # The launcher records but does not enforce these legacy fields.
    max_total_turns = raw.get("max_total_turns")
    if max_total_turns is not None and (
        not isinstance(max_total_turns, int) or isinstance(max_total_turns, bool) or max_total_turns < 1
    ):
        raise ContractError("legacy max_total_turns must be a positive integer when present")
    max_extensions = raw.get("max_extensions")
    if max_extensions is not None and (
        not isinstance(max_extensions, int) or isinstance(max_extensions, bool) or max_extensions < 0
    ):
        raise ContractError("legacy max_extensions must be a non-negative integer when present")
    timeout_seconds = raw.get("timeout_seconds")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 5 <= timeout_seconds <= 1800:
        raise ContractError("timeout_seconds must be an integer from 5 through 1800")

    validation = raw.get("validation_command")
    if validation is not None:
        if (
            not isinstance(validation, list)
            or not validation
            or len(validation) > 24
            or any(not isinstance(item, str) or not item for item in validation)
        ):
            raise ContractError("validation_command must be null or a non-empty argument array")
        validation_command: tuple[str, ...] | None = tuple(validation)
    else:
        validation_command = None
    validation_timeout = raw.get("validation_timeout_seconds", 120)
    if not isinstance(validation_timeout, int) or isinstance(validation_timeout, bool) or not 1 <= validation_timeout <= 1800:
        raise ContractError("validation_timeout_seconds must be an integer from 1 through 1800")

    allow_subagents = raw.get("allow_subagents")
    if allow_subagents is not False:
        raise ContractError("the first milestone requires allow_subagents to be false")
    require_subscription_auth = raw.get("require_subscription_auth")
    if require_subscription_auth is not True:
        raise ContractError("require_subscription_auth must be true")

    return Task(
        task_id=task_id,
        repo_root=repo_root,
        base_commit=base_commit.lower(),
        mode=mode,
        objective=objective.strip(),
        context_paths=context_paths,
        forbidden_context=forbidden_context,
        allowed_changed_paths=allowed_changed_paths,
        acceptance_criteria=acceptance_criteria,
        plan_status=plan_status,
        risk=risk,
        review_required=review_required,
        locked_decisions=locked_decisions,
        stop_conditions=stop_conditions,
        model=model.strip() if isinstance(model, str) else None,
        max_turns=max_turns,
        max_total_turns=max_total_turns,
        max_extensions=max_extensions,
        timeout_seconds=timeout_seconds,
        validation_command=validation_command,
        validation_timeout_seconds=validation_timeout,
        allow_subagents=allow_subagents,
        require_subscription_auth=require_subscription_auth,
        raw=raw,
    )
