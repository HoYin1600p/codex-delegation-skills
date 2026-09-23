from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .auth import Preflight, PreflightError, inspect_preflight
from .contracts import CHECKPOINT_TURN_RESERVE, TASK_ID_RE, ContractError, Task, load_task
from .process import ProcessResult, run_process
from .revision import MAX_CLAUDE_REVISION_TURNS, load_feedback, revision_target_state
from .usage import UsageError, fetch_usage


PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .state import state_root, ensure_state_root

DEFAULT_ARTIFACT_ROOT = state_root() / "artifacts" / "claude"
REPLY_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "reply.schema.json"
CHECKPOINT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "checkpoint.schema.json"
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
ISOLATED_SETTINGS_ARGS = ["--setting-sources", "user", "--settings", '{"disableAllHooks":true}']
# Claude work is allowed while any five-hour headroom remains; at or below 0% the window is exhausted.
FIVE_HOUR_EXHAUSTED_REMAINING = 0.0
USAGE_CACHE_TTL_SECONDS = 60.0
USAGE_STALE_FALLBACK_SECONDS = 300.0
USAGE_STALE_FALLBACK_MIN_REMAINING = 15.0


class BridgeError(RuntimeError):
    pass


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _active_lock_path(artifact_root: Path, task_id: str) -> Path:
    if not TASK_ID_RE.fullmatch(task_id):
        raise BridgeError("task_id is invalid")
    return artifact_root / ".active" / f"{task_id}.json"


def _pid_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied still proves that the process exists.
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # Refuse destructive recovery when process state cannot be proven.
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_task_record(artifact_root: Path, task_id: str) -> dict[str, Any]:
    lock_path = _active_lock_path(artifact_root, task_id)
    if not lock_path.exists():
        return {"task_id": task_id, "status": "NOT_RUNNING", "lock_path": str(lock_path)}
    try:
        record = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"active-task lock is invalid: {lock_path}") from exc
    if not isinstance(record, dict) or record.get("task_id") != task_id:
        raise BridgeError(f"active-task lock does not match task {task_id!r}")
    return {**record, "process_running": _pid_is_running(record.get("pid")), "lock_path": str(lock_path)}


def clear_stale_task_lock(artifact_root: Path, task_id: str) -> dict[str, Any]:
    record = active_task_record(artifact_root, task_id)
    if record.get("status") == "NOT_RUNNING":
        return record
    if record.get("process_running") is True:
        raise BridgeError(f"task {task_id!r} still has a running owner process")
    lock_path = Path(record["lock_path"]).resolve(strict=True)
    active_dir = (artifact_root / ".active").resolve(strict=True)
    if lock_path.parent != active_dir:
        raise BridgeError("active-task lock is outside the configured lock directory")
    stale_dir = artifact_root / ".stale-locks"
    stale_dir.mkdir(parents=True, exist_ok=True)
    destination = stale_dir / f"{task_id}-{_utc_stamp()}.json"
    lock_path.replace(destination)
    return {
        "task_id": task_id,
        "status": "STALE_LOCK_ARCHIVED",
        "archived_lock": str(destination),
        "previous": record,
    }


def _lifecycle_status(
    *,
    failures: Sequence[str],
    review_required: bool,
    extension_requested: bool = False,
    checkpoint_format_failed: bool = False,
) -> str:
    if checkpoint_format_failed:
        return "CHECKPOINT_FORMAT_FAILED"
    if failures:
        return "BLOCKED"
    if extension_requested:
        return "EXTENSION_REQUESTED"
    return "REVIEW_PENDING" if review_required else "IMPLEMENTED"


def _checkpoint_format_failed(segment: dict[str, Any]) -> bool:
    return bool(
        segment.get("turn_cap_reached")
        and segment.get("checkpoint_process") is not None
        and not isinstance(segment.get("structured"), dict)
    )


@contextmanager
def _active_task_lock(artifact_root: Path, task: Task, task_file: str | Path, *, operation: str = "run"):
    active_dir = artifact_root / ".active"
    active_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _active_lock_path(artifact_root, task.task_id)
    record = {
        "task_id": task.task_id,
        "status": "RUNNING",
        "operation": operation,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": task.timeout_seconds,
        "task_file": str(Path(task_file).resolve(strict=True)),
    }
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError as exc:
        raise BridgeError(
            f"task {task.task_id!r} already has an active run; wait for that process instead of starting another"
        ) from exc
    try:
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def _process_record(result: ProcessResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "elapsed_seconds": result.elapsed_seconds,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "cancelled": result.cancelled,
    }


def parse_envelope(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    if not stdout.strip():
        return None, "missing terminal result"
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON output: {exc}"
    if not isinstance(value, dict):
        return None, "terminal JSON must be an object"
    return value, None


def classify_error(envelope: dict[str, Any] | None) -> str | None:
    if not envelope or envelope.get("is_error") is not True:
        return None
    status = envelope.get("api_error_status")
    message = envelope.get("result")
    text = message.lower() if isinstance(message, str) else ""
    if status == 429 or any(term in text for term in ("rate limit", "usage limit", "quota exhausted")):
        return "rate_limit"
    if status == 401 or any(term in text for term in ("failed to authenticate", "token has expired")):
        return "authentication"
    return "claude_error"


def observed_metrics(envelope: dict[str, Any] | None) -> dict[str, Any]:
    usage = envelope.get("usage") if envelope else None
    usage = usage if isinstance(usage, dict) else {}
    model_usage = envelope.get("modelUsage") if envelope else None
    model_usage = model_usage if isinstance(model_usage, dict) else None
    incomplete = bool(envelope and envelope.get("is_error") is True)

    def metric(key: str) -> dict[str, Any]:
        value = usage.get(key)
        present = isinstance(value, (int, float)) and not isinstance(value, bool)
        return {
            "value": value if present else None,
            "provenance": "partial" if present and incomplete else ("measured" if present else "unavailable"),
        }

    cost = envelope.get("total_cost_usd") if envelope else None
    cost_present = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    return {
        "accounting_completeness": "partial" if incomplete else ("measured" if envelope else "unavailable"),
        "input_tokens": metric("input_tokens"),
        "cache_creation_input_tokens": metric("cache_creation_input_tokens"),
        "cache_read_input_tokens": metric("cache_read_input_tokens"),
        "output_tokens": metric("output_tokens"),
        "total_tokens": {"value": None, "provenance": "unavailable"},
        "model_usage": {
            "value": model_usage,
            "provenance": "partial" if model_usage is not None and incomplete else ("measured" if model_usage is not None else "unavailable"),
        },
        "estimated_api_equivalent_usd": {
            "value": cost if cost_present else None,
            "provenance": "partial" if cost_present and incomplete else ("estimated" if cost_present else "unavailable"),
        },
        "actual_incremental_charge_usd": {"value": None, "provenance": "unavailable"},
        "subscription_usage_before": {"value": None, "provenance": "unavailable"},
        "subscription_usage_after": {"value": None, "provenance": "unavailable"},
    }


def actual_model(envelope: dict[str, Any] | None) -> str | None:
    if not envelope:
        return None
    direct = envelope.get("model")
    if isinstance(direct, str) and direct:
        return direct
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        model_id, details = next(iter(model_usage.items()))
        if isinstance(details, dict) and isinstance(details.get("canonicalModel"), str):
            return details["canonicalModel"]
        return model_id
    return None


def _safe_artifact_root(root: Path) -> Path:
    from .state import require_external_storage
    resolved = require_external_storage(root)
    if resolved == DEFAULT_ARTIFACT_ROOT.resolve():
        ensure_state_root()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise BridgeError("Artifact storage must be outside the installed skill")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _git_executable() -> Path:
    value = shutil.which("git")
    if not value:
        raise BridgeError("Git is not available")
    return Path(value).resolve(strict=True)


def _git(cwd: Path, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(_git_executable()), *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        shell=False,
        check=False,
    )
    if check and result.returncode != 0:
        raise BridgeError(f"Git command failed: {' '.join(args)}\n{result.stderr.strip()}")
    return result


def _verify_repository(task: Task) -> None:
    root = _git(task.repo_root, ["rev-parse", "--show-toplevel"]).stdout.strip()
    if Path(root).resolve() != task.repo_root:
        raise BridgeError("repo_root must be the Git top-level directory")
    resolved = _git(task.repo_root, ["rev-parse", "--verify", f"{task.base_commit}^{{commit}}"]).stdout.strip().lower()
    if resolved != task.base_commit:
        raise BridgeError("base_commit did not resolve to the exact requested commit")


def _path_is_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path.replace("\\", "/"))
    for value in allowed:
        boundary = PurePosixPath(value[:-3].rstrip("/") if value.endswith("/**") else value)
        if candidate == boundary or boundary in candidate.parents:
            return True
    return False


def _changed_paths(worktree: Path, base_commit: str) -> list[str]:
    tracked = _git(worktree, ["diff", "--name-only", base_commit, "--"]).stdout.splitlines()
    untracked = _git(worktree, ["ls-files", "--others", "--exclude-standard"]).stdout.splitlines()
    return sorted({path.replace("\\", "/") for path in [*tracked, *untracked] if path})


def _complete_diff(worktree: Path, base_commit: str) -> str:
    sections = [_git(worktree, ["diff", "--binary", base_commit, "--"]).stdout]
    untracked = _git(worktree, ["ls-files", "--others", "--exclude-standard"]).stdout.splitlines()
    for path in untracked:
        patch = _git(worktree, ["diff", "--no-index", "--binary", "--", "/dev/null", path], check=False)
        if patch.returncode not in {0, 1}:
            raise BridgeError(f"Git could not render untracked file in diff: {path}")
        sections.append(patch.stdout)
    return "".join(sections)


def _worktree_fingerprint(worktree: Path, base_commit: str) -> str:
    digest = hashlib.sha256()
    for path in _changed_paths(worktree, base_commit):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        result = _git(worktree, ["hash-object", "--", path], check=False)
        value = result.stdout.strip() if result.returncode == 0 else "<deleted>"
        digest.update(value.encode("ascii", errors="replace"))
        digest.update(b"\n")
    return digest.hexdigest()


def _task_prompt(task: Task) -> str:
    contract = {
        "task_id": task.task_id,
        "mode": task.mode,
        "objective": task.objective,
        "permitted_context_paths": list(task.context_paths),
        "forbidden_context": list(task.forbidden_context),
        "permitted_changed_paths": list(task.allowed_changed_paths),
        "acceptance_criteria": list(task.acceptance_criteria),
        "plan_status": task.plan_status,
        "risk": task.risk,
        "review_required": task.review_required,
        "locked_decisions": list(task.locked_decisions),
        "stop_conditions": list(task.stop_conditions),
        "initial_turn_cap": task.max_turns,
        "continuation_policy": (
            "Codex may grant additional bounded segments until the task completes, progress stops, a real blocker "
            "appears, or the five-hour Claude usage window is verified exhausted (0% remaining)."
        ),
    }
    return (
        "You are a single bounded Claude Code worker. Do not delegate or spawn agents. "
        "Work only inside the current working directory and only on the supplied contract. "
        "Do not inspect unrelated paths. Do not use network access. Do not commit, merge, push, "
        "install packages, or change credentials. If the task cannot be completed within scope, "
        "return blocked. For implementation and test tasks, the Edit tool can create new files at permitted "
        "paths even though a separate Write tool is unavailable. Shell execution is intentionally unavailable; "
        "do not block solely because you cannot run the validation command. Implement the scoped work, report "
        "those checks as not_run, and let the bridge run independent validation after you return complete. "
        "If you are approaching the end of this work segment and material work remains, "
        "return status extension_requested before starting another large step. In extension_request, state "
        "the work completed, exact work remaining, why continuation is worthwhile, and the additional turns "
        "requested. For any other status, return an empty extension request with requested_turns 0. "
        "Keep checkpoints and results concise: a short summary, the exact remaining work, and the checks you "
        "performed; refer to changed files and artifacts instead of restating the full plan. "
        "Your final response must satisfy the provided JSON schema.\n\n"
        + json.dumps(contract, indent=2, ensure_ascii=False)
    )


def _continuation_prompt(task: Task, request: dict[str, Any], granted_turns: int) -> str:
    return (
        f"Codex reviewed your checkpoint and grants {granted_turns} additional work turns for the same task. "
        "Continue in the existing worktree and session. Do not redo completed work. Preserve the original "
        "scope, locked decisions, stop conditions, and acceptance criteria. If you finish, return complete. "
        "If meaningful work will still remain near this segment cap, return extension_requested again. "
        "For any non-extension status, use an empty extension request with requested_turns 0.\n\n"
        + json.dumps({"task_id": task.task_id, "prior_extension_request": request}, indent=2, ensure_ascii=False)
    )


def _revision_prompt(task: Task, findings: list[dict[str, str]], granted_turns: int) -> str:
    return (
        f"Codex reviewed your completed work and returns concrete review findings. You have {granted_turns} work "
        "turns in the same session and worktree to correct them. Fix only the listed defects inside the original "
        "permitted_changed_paths; do not redo unrelated work or widen scope. Preserve the locked decisions, stop "
        "conditions, and acceptance criteria. The bridge reruns independent validation afterwards. Return complete "
        "when every finding is corrected, blocked if a finding cannot be corrected within scope, or "
        "extension_requested if material correction work remains near this segment cap. For any non-extension "
        "status, use an empty extension request with requested_turns 0.\n\n"
        + json.dumps({"task_id": task.task_id, "review_findings": findings}, indent=2, ensure_ascii=False)
    )


def _checkpoint_prompt() -> str:
    return (
        "The preceding work segment reached its turn cap. Do not call tools or modify files. Report a checkpoint "
        "from the work already performed. Return complete if the assignment is actually finished, blocked if it "
        "cannot continue, or extension_requested if material work remains. For extension_requested, list concrete "
        "completed work, exact remaining work, why continuing is worthwhile, and request 1 through 24 additional "
        "turns. For any other status, use an empty extension request with requested_turns 0. Submit the StructuredOutput "
        "arguments as this exact top-level shape: "
        '{"status":"extension_requested","summary":"...","extension_request":{"completed_work":["..."],'
        '"remaining_work":["..."],"reason":"...","requested_turns":8}}. '
        "Never wrap the object in keys named 'parameter', 'parameter name', 'input', or 'arguments'. If schema "
        "validation rejects the first submission, correct it once using the validation message."
    )


def _extension_request(structured: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(structured, dict) or structured.get("status") != "extension_requested":
        return None
    request = structured.get("extension_request")
    if not isinstance(request, dict):
        return None
    completed = request.get("completed_work")
    remaining = request.get("remaining_work")
    reason = request.get("reason")
    turns = request.get("requested_turns")
    if (
        not isinstance(completed, list)
        or any(not isinstance(item, str) or not item.strip() for item in completed)
        or not isinstance(remaining, list)
        or not remaining
        or any(not isinstance(item, str) or not item.strip() for item in remaining)
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or not 1 <= turns <= 24
    ):
        return None
    return request


def _normalized_model_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _finite_percent(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    if not math.isfinite(number) or number > 100.0:
        return None
    return number


def _usage_gate(usage: Any, model: str | None) -> dict[str, Any]:
    """Block only a confirmed-exhausted or unverifiable five-hour window; weekly windows are advisory."""
    windows: list[dict[str, Any]] = []
    problems: list[str] = []

    def add_window(name: str, value: Any) -> None:
        remaining = _finite_percent(value.get("remaining_percent")) if isinstance(value, dict) else None
        if remaining is None:
            problems.append(f"usage snapshot has a missing or invalid {name} remaining percentage")
            return
        windows.append({"name": name, **value})

    if not isinstance(usage, dict) or usage.get("status") != "available":
        problems.append("usage snapshot is not an available reading")
        usage = usage if isinstance(usage, dict) else {}
    add_window("five_hour", usage.get("five_hour"))
    add_window("weekly", usage.get("weekly"))
    model_text = _normalized_model_name(model) if isinstance(model, str) else ""
    scoped = usage.get("weekly_scoped")
    if isinstance(scoped, list):
        for item in scoped:
            if not isinstance(item, dict):
                continue
            display = item.get("model")
            identifiers = [item.get("model_id"), display]
            normalized_identifiers = [
                _normalized_model_name(value)
                for value in identifiers
                if isinstance(value, str) and value.strip()
            ]
            matches_selected_model = bool(
                model_text
                and any(
                    identifier == model_text
                    or identifier in model_text
                    or model_text in identifier
                    for identifier in normalized_identifiers
                )
            )
            # If Claude did not identify the selected model, conservatively apply
            # every scoped weekly window rather than silently bypassing a limit.
            if not model_text or matches_selected_model:
                label = display if isinstance(display, str) and display else item.get("model_id", "scoped")
                add_window(f"weekly_{_normalized_model_name(str(label)) or 'scoped'}", item)
    five_hour = next((window for window in windows if window["name"] == "five_hour"), None)
    reason: str | None = None
    blockers: list[str] = []
    if problems or five_hour is None:
        reason = "usage_unavailable"
        blockers = problems or ["usage snapshot omitted the five-hour window"]
    elif float(five_hour["remaining_percent"]) <= FIVE_HOUR_EXHAUSTED_REMAINING:
        reason = "five_hour_exhausted"
        blockers = [f"five_hour has {five_hour['remaining_percent']}% remaining; the five-hour window is exhausted"]
    return {
        "allowed": reason is None,
        "reason": reason,
        "gate_window": "five_hour",
        "exhausted_at_remaining_percent": FIVE_HOUR_EXHAUSTED_REMAINING,
        "five_hour_remaining_percent": five_hour["remaining_percent"] if five_hour else None,
        "applicable_windows": windows,
        "advisory_windows": [window for window in windows if window["name"] != "five_hour"],
        "blockers": blockers,
    }


def _paused_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": "paused",
        "reason": decision["reason"],
        "detail": decision["detail"],
        "captured_at": decision["captured_at"],
        "usage": decision["usage"],
        "gate": decision["gate"],
        "legacy_override_requested": decision["legacy_override_requested"],
        "five_hour_soft_limit_override": False,
    }


def _trusted_session_id(artifact_dir: Path, segments: list[Any]) -> str:
    if not segments:
        raise BridgeError("artifact has no trusted segment lineage")
    session_ids: list[str] = []
    recorded_session_ids: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise BridgeError("artifact segment lineage is invalid")
        label = segment.get("label")
        if not isinstance(label, str) or not label:
            raise BridgeError("artifact segment label is invalid")
        paths = [artifact_dir / ("stdout.json" if label == "initial" else f"{label}.stdout.json")]
        if segment.get("checkpoint_process") is not None:
            paths.append(artifact_dir / f"{label}.checkpoint.stdout.json")
        repairs = segment.get("checkpoint_repairs", [])
        if repairs is not None and not isinstance(repairs, list):
            raise BridgeError("artifact checkpoint repair lineage is invalid")
        for repair in repairs or []:
            stdout_file = repair.get("stdout_file") if isinstance(repair, dict) else None
            if not isinstance(stdout_file, str) or Path(stdout_file).name != stdout_file:
                raise BridgeError("artifact checkpoint repair output path is invalid")
            paths.append(artifact_dir / stdout_file)
        for path in paths:
            try:
                raw_output = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise BridgeError(f"artifact session lineage is unreadable: {path.name}") from exc
            envelope, error = parse_envelope(raw_output)
            session_id = envelope.get("session_id") if envelope else None
            if error or not isinstance(session_id, str):
                raise BridgeError(f"artifact session lineage is unreadable: {path.name}")
            try:
                uuid.UUID(session_id)
            except ValueError as exc:
                raise BridgeError(f"artifact session ID is invalid: {path.name}") from exc
            session_ids.append(session_id)
        recorded = segment.get("session_id")
        if not isinstance(recorded, str):
            raise BridgeError("artifact segment session ID is invalid")
        recorded_session_ids.append(recorded)
    if len(set(session_ids + recorded_session_ids)) != 1:
        raise BridgeError("artifact session lineage does not match its raw segment output")
    return session_ids[0]


def _run_validation(task: Task, worktree: Path, artifact_dir: Path) -> tuple[dict[str, Any], str | None]:
    record: dict[str, Any] = {
        "requested": list(task.validation_command) if task.validation_command else None,
        "status": "not_run",
        "process": None,
    }
    if not task.validation_command:
        return record, None
    requested_executable = task.validation_command[0]
    requested_path = Path(requested_executable)
    executable: str | None = None
    if requested_path.is_absolute() or requested_path.parent != Path("."):
        candidates = [requested_path] if requested_path.is_absolute() else [
            worktree / requested_path,
            task.repo_root / requested_path,
        ]
        executable = next((str(candidate.resolve()) for candidate in candidates if candidate.is_file()), None)
    else:
        executable = shutil.which(requested_executable)
    if not executable:
        return record, f"validation executable not found: {task.validation_command[0]}"
    validation = run_process(
        Path(executable).resolve(strict=True),
        task.validation_command[1:],
        cwd=worktree,
        timeout_seconds=task.validation_timeout_seconds,
    )
    (artifact_dir / "validation.stdout.log").write_text(validation.stdout, encoding="utf-8")
    (artifact_dir / "validation.stderr.log").write_text(validation.stderr, encoding="utf-8")
    record = {
        "requested": list(task.validation_command),
        "status": "passed" if validation.exit_code == 0 and not validation.timed_out else "failed",
        "process": _process_record(validation),
        "stdout_path": str(artifact_dir / "validation.stdout.log"),
        "stderr_path": str(artifact_dir / "validation.stderr.log"),
    }
    return record, None if record["status"] == "passed" else "independent validation failed"


def _work_args(task: Task, turns: int, *, resume_session_id: str | None = None) -> list[str]:
    schema = json.loads(REPLY_SCHEMA_PATH.read_text(encoding="utf-8"))
    tool_set = "Read,Glob,Grep" if task.mode in {"analyze", "review"} else "Read,Glob,Grep,Edit"
    args = ["-p", *ISOLATED_SETTINGS_ARGS]
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    args.extend(
        [
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--tools",
            tool_set,
            "--allowedTools",
            tool_set,
            "--disallowedTools",
            "Task,Bash,Write,NotebookEdit,WebFetch,WebSearch,mcp__*",
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            str(turns),
            "--strict-mcp-config",
            "--mcp-config",
            EMPTY_MCP_CONFIG,
        ]
    )
    if task.model:
        args.extend(["--model", task.model])
    return args


def _checkpoint_args(session_id: str) -> list[str]:
    schema = json.loads(CHECKPOINT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return [
        "-p",
        *ISOLATED_SETTINGS_ARGS,
        "--resume",
        session_id,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
        "--disallowedTools",
        "Task,Bash,Write,Edit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,mcp__*",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        str(CHECKPOINT_TURN_RESERVE),
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
    ]


def _run_checkpoint(
    *,
    preflight: Preflight,
    task: Task,
    worktree: Path,
    artifact_dir: Path,
    session_id: str,
    output_stem: str,
) -> dict[str, Any]:
    process = run_process(
        preflight.claude_executable,
        _checkpoint_args(session_id),
        cwd=worktree,
        timeout_seconds=min(task.timeout_seconds, 120),
        stdin_text=_checkpoint_prompt(),
    )
    stdout_file = f"{output_stem}.stdout.json"
    stderr_file = f"{output_stem}.stderr.log"
    (artifact_dir / stdout_file).write_text(process.stdout, encoding="utf-8")
    (artifact_dir / stderr_file).write_text(process.stderr, encoding="utf-8")
    envelope, error = parse_envelope(process.stdout)
    structured = envelope.get("structured_output") if envelope else None
    if structured is not None and not isinstance(structured, dict):
        error = "checkpoint structured_output was not an object"
        structured = None
    return {
        "process": process,
        "envelope": envelope,
        "structured": structured,
        "error": error,
        "stdout_file": stdout_file,
        "stderr_file": stderr_file,
    }


def _run_work_segment(
    *,
    preflight: Preflight,
    task: Task,
    worktree: Path,
    artifact_dir: Path,
    prompt: str,
    turns: int,
    label: str,
    resume_session_id: str | None = None,
    checkpoint_allowed: bool = True,
) -> dict[str, Any]:
    process = run_process(
        preflight.claude_executable,
        _work_args(task, turns, resume_session_id=resume_session_id),
        cwd=worktree,
        timeout_seconds=task.timeout_seconds,
        stdin_text=prompt,
    )
    stdout_path = artifact_dir / ("stdout.json" if label == "initial" else f"{label}.stdout.json")
    stderr_path = artifact_dir / ("stderr.log" if label == "initial" else f"{label}.stderr.log")
    stdout_path.write_text(process.stdout, encoding="utf-8")
    stderr_path.write_text(process.stderr, encoding="utf-8")
    envelope, parse_error = parse_envelope(process.stdout)
    structured = envelope.get("structured_output") if envelope else None
    if structured is not None and not isinstance(structured, dict):
        parse_error = "structured_output was not an object"
        structured = None

    turn_cap_reached = bool(
        envelope
        and (envelope.get("terminal_reason") == "max_turns" or envelope.get("subtype") == "error_max_turns")
    )
    checkpoint_process: ProcessResult | None = None
    checkpoint_envelope: dict[str, Any] | None = None
    checkpoint_error: str | None = None
    session_id = envelope.get("session_id") if envelope and isinstance(envelope.get("session_id"), str) else None
    if turn_cap_reached and session_id and checkpoint_allowed:
        checkpoint = _run_checkpoint(
            preflight=preflight,
            task=task,
            worktree=worktree,
            artifact_dir=artifact_dir,
            session_id=session_id,
            output_stem=f"{label}.checkpoint",
        )
        checkpoint_process = checkpoint["process"]
        checkpoint_envelope = checkpoint["envelope"]
        checkpoint_error = checkpoint["error"]
        checkpoint_structured = checkpoint["structured"]
        if isinstance(checkpoint_structured, dict):
            structured = checkpoint_structured
            parse_error = None
    return {
        "process": process,
        "envelope": envelope,
        "structured": structured,
        "parse_error": parse_error,
        "turn_cap_reached": turn_cap_reached,
        "session_id": session_id,
        "checkpoint_process": checkpoint_process,
        "checkpoint_envelope": checkpoint_envelope,
        "checkpoint_error": checkpoint_error,
        "label": label,
        "granted_turns": turns,
    }


class ClaudeBridge:
    def __init__(self, artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT):
        self.artifact_root = _safe_artifact_root(Path(artifact_root))

    def preflight(self) -> dict[str, Any]:
        return inspect_preflight().public_record()

    def _cached_usage(self) -> tuple[dict[str, Any] | None, float | None]:
        cache_path = self.artifact_root / ".usage-cache.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            captured_at = datetime.fromisoformat(cached["captured_at"])
            if captured_at.tzinfo is None:
                return None, None
            age_seconds = max(0.0, (datetime.now(timezone.utc) - captured_at).total_seconds())
            remaining = cached.get("five_hour", {}).get("remaining_percent")
            if (
                cached.get("status") != "available"
                or not isinstance(remaining, (int, float))
                or isinstance(remaining, bool)
            ):
                return None, None
            return cached, age_seconds
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, None

    @staticmethod
    def _usage_with_retrieval(
        usage: dict[str, Any],
        *,
        mode: str,
        age_seconds: float,
        warning: str | None = None,
    ) -> dict[str, Any]:
        result = dict(usage)
        result["retrieval"] = {
            "mode": mode,
            "age_seconds": round(max(0.0, age_seconds), 3),
            "warning": warning,
        }
        return result

    def usage(self, *, force_refresh: bool = False) -> dict[str, Any]:
        preflight = inspect_preflight()
        cached, age_seconds = self._cached_usage()
        if not force_refresh and cached is not None and age_seconds is not None and age_seconds <= USAGE_CACHE_TTL_SECONDS:
            return self._usage_with_retrieval(cached, mode="cache", age_seconds=age_seconds)
        oauth_refresh: dict[str, Any] | None = None
        try:
            usage = fetch_usage(claude_version=preflight.version)
        except UsageError as exc:
            if exc.status_code == 401:
                refresh = run_process(
                    preflight.claude_executable,
                    [
                        "-p",
                        *ISOLATED_SETTINGS_ARGS,
                        "--output-format",
                        "json",
                        "--tools",
                        "",
                        "--disallowedTools",
                        "Task,Bash,Write,Edit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,mcp__*",
                        "--permission-mode",
                        "dontAsk",
                        "--permission-prompts",
                        "none",
                        "--max-turns",
                        "1",
                        "--no-session-persistence",
                        "--strict-mcp-config",
                        "--mcp-config",
                        EMPTY_MCP_CONFIG,
                    ],
                    cwd=PROJECT_ROOT,
                    timeout_seconds=90,
                    stdin_text="Reply with exactly READY. Do not call tools.",
                )
                oauth_refresh = {
                    "attempted": True,
                    "method": "official_claude_cli_no_tools",
                    "process": _process_record(refresh),
                }
                try:
                    usage = fetch_usage(claude_version=preflight.version)
                except UsageError as refresh_error:
                    exc = refresh_error
                else:
                    usage["oauth_refresh"] = oauth_refresh
                    _write_json(self.artifact_root / ".usage-cache.json", usage)
                    return self._usage_with_retrieval(usage, mode="live", age_seconds=0.0)
            remaining = cached.get("five_hour", {}).get("remaining_percent") if cached is not None else None
            fallback_allowed = bool(
                exc.transient
                and cached is not None
                and age_seconds is not None
                and age_seconds <= USAGE_STALE_FALLBACK_SECONDS
                and isinstance(remaining, (int, float))
                and not isinstance(remaining, bool)
                and float(remaining) >= USAGE_STALE_FALLBACK_MIN_REMAINING
            )
            if not fallback_allowed:
                raise
            return self._usage_with_retrieval(
                cached,
                mode="stale_fallback",
                age_seconds=age_seconds,
                warning=(
                    f"live usage refresh was transiently unavailable ({exc}); using a recent reading only because "
                    f"five-hour remaining was at least {USAGE_STALE_FALLBACK_MIN_REMAINING:.0f}%"
                ),
            )
        _write_json(self.artifact_root / ".usage-cache.json", usage)
        return self._usage_with_retrieval(usage, mode="live", age_seconds=0.0)

    def _usage_decision(self, model: str | None, *, override_requested: bool = False) -> dict[str, Any]:
        captured_at = datetime.now(timezone.utc).isoformat()
        try:
            usage = self.usage()
        except (UsageError, PreflightError, BridgeError, OSError) as exc:
            usage, gate = None, None
            allowed, reason, detail = False, "usage_unavailable", f"usage check unavailable: {exc}"
        else:
            gate = _usage_gate(usage, model)
            allowed, reason = gate["allowed"], gate["reason"]
            detail = "; ".join(gate["blockers"]) or None
        return {
            "allowed": allowed,
            "reason": reason,
            "detail": detail,
            "captured_at": captured_at,
            "usage": usage,
            "gate": gate,
            # The legacy soft-limit override is recorded but never bypasses exhaustion or unknown usage.
            "legacy_override_requested": override_requested,
            "legacy_override_applied": False,
        }

    def _usage_blocked_run(self, task_file: str | Path, task: Task, decision: dict[str, Any]) -> dict[str, Any]:
        artifact_dir = self.artifact_root / f"{task.task_id}-{_utc_stamp()}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(Path(task_file).resolve(strict=True), artifact_dir / "task.json")
        record = {
            "kind": "task",
            "task_id": task.task_id,
            "status": "usage_blocked",
            "lifecycle_status": "BLOCKED",
            "plan_status": task.plan_status,
            "risk": task.risk,
            "review_required": task.review_required,
            "artifact_directory": str(artifact_dir),
            "repository": str(task.repo_root),
            "starting_commit": task.base_commit,
            "worktree": None,
            "branch": None,
            "requested_model": task.model,
            "provider_launched": False,
            "error_kind": decision["reason"],
            "usage_gate": decision,
            "failures": [f"Claude usage gate blocked the run before launch: {decision['reason']}"],
            "segments": [],
        }
        _write_json(artifact_dir / "result.json", record)
        return record

    def smoke(self, timeout_seconds: int = 60) -> dict[str, Any]:
        decision = self._usage_decision(None)
        if not decision["allowed"]:
            return {"status": "usage_blocked", "provider_launched": False, "usage_gate": decision}
        preflight = inspect_preflight()
        artifact_dir = self.artifact_root / f"smoke-{_utc_stamp()}"
        workspace = artifact_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        prompt = "Reply with exactly READY. Do not call any tools."
        (artifact_dir / "assignment.txt").write_text(prompt + "\n", encoding="utf-8")
        args = [
            "-p",
            *ISOLATED_SETTINGS_ARGS,
            "--output-format",
            "json",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            "1",
            "--strict-mcp-config",
            "--mcp-config",
            EMPTY_MCP_CONFIG,
        ]
        process = run_process(
            preflight.claude_executable,
            args,
            cwd=workspace,
            timeout_seconds=timeout_seconds,
            stdin_text=prompt,
        )
        (artifact_dir / "stdout.json").write_text(process.stdout, encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(process.stderr, encoding="utf-8")
        envelope, parse_error = parse_envelope(process.stdout)
        result_text = envelope.get("result") if envelope and isinstance(envelope.get("result"), str) else None
        failures: list[str] = []
        if process.timed_out:
            failures.append("process timed out")
        if process.exit_code != 0:
            failures.append(f"process exited with {process.exit_code}")
        if parse_error:
            failures.append(parse_error)
        if envelope and envelope.get("is_error") is True:
            failures.append("Claude reported an error")
        if result_text is None or result_text.strip() != "READY":
            failures.append("response was not exactly READY")
        record = {
            "kind": "smoke",
            "status": "complete" if not failures else "failed",
            "artifact_directory": str(artifact_dir),
            "preflight": preflight.public_record(),
            "process": _process_record(process),
            "session_id": envelope.get("session_id") if envelope else None,
            "actual_model": actual_model(envelope),
            "api_error_status": envelope.get("api_error_status") if envelope else None,
            "error_kind": classify_error(envelope),
            "error_message": result_text if envelope and envelope.get("is_error") is True else None,
            "observed_metrics": observed_metrics(envelope),
            "failures": failures,
        }
        _write_json(artifact_dir / "result.json", record)
        return record

    def run(self, task_file: str | Path) -> dict[str, Any]:
        task = load_task(task_file)
        with _active_task_lock(self.artifact_root, task, task_file):
            return self._run_ready_task(task_file, task)

    def _run_ready_task(self, task_file: str | Path, task: Task) -> dict[str, Any]:
        _verify_repository(task)
        usage_decision = self._usage_decision(task.model)
        if not usage_decision["allowed"]:
            return self._usage_blocked_run(task_file, task, usage_decision)
        preflight = inspect_preflight()
        artifact_dir = self.artifact_root / f"{task.task_id}-{_utc_stamp()}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(Path(task_file).resolve(strict=True), artifact_dir / "task.json")

        primary_before = _git(task.repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        worktree = task.repo_root
        branch: str | None = None
        if task.mode in {"implement", "test"}:
            branch = f"delegate/{task.task_id}-{_utc_stamp().lower()}"
            worktree = artifact_dir / "worktree"
            _git(task.repo_root, ["worktree", "add", "-b", branch, str(worktree), task.base_commit])

        prompt = _task_prompt(task)
        (artifact_dir / "assignment.txt").write_text(prompt, encoding="utf-8")
        segment = _run_work_segment(
            preflight=preflight,
            task=task,
            worktree=worktree,
            artifact_dir=artifact_dir,
            prompt=prompt,
            turns=task.max_turns,
            label="initial",
        )
        process = segment["process"]
        envelope = segment["envelope"]
        structured = segment["structured"]
        parse_error = segment["parse_error"]

        changed = _changed_paths(worktree, task.base_commit)
        unauthorized = [path for path in changed if not _path_is_allowed(path, task.allowed_changed_paths)]
        diff = _complete_diff(worktree, task.base_commit)
        (artifact_dir / "diff.patch").write_text(diff, encoding="utf-8")
        _write_json(artifact_dir / "changed-paths.json", changed)
        fingerprint = _worktree_fingerprint(worktree, task.base_commit)
        checkpoint_turns = CHECKPOINT_TURN_RESERVE if segment["checkpoint_process"] is not None else 0
        total_authorized_turns = task.max_turns + checkpoint_turns

        validation_record: dict[str, Any]
        failures: list[str] = []
        worker_warnings: list[str] = []
        extension_request = _extension_request(structured)
        extension_requested = extension_request is not None
        cap_recovered = segment["turn_cap_reached"] and isinstance(structured, dict)
        checkpoint_format_failed = _checkpoint_format_failed(segment)
        if process.timed_out:
            failures.append("Claude process timed out")
        if checkpoint_format_failed:
            failures.append("checkpoint structured output failed after two bounded formatting attempts")
        elif process.exit_code != 0 and not cap_recovered:
            failures.append(f"Claude process exited with {process.exit_code}")
        if parse_error and not cap_recovered and not checkpoint_format_failed:
            failures.append(parse_error)
        if envelope and envelope.get("is_error") is True and not cap_recovered and not checkpoint_format_failed:
            failures.append("Claude reported an error")
        if structured is None:
            if not checkpoint_format_failed:
                failures.append("missing structured_output")
        elif structured.get("status") == "blocked":
            failures.append("Claude reported status 'blocked'")
        elif structured.get("status") == "partial":
            worker_warnings.append("Claude reported status 'partial'; independent evidence decides acceptance")
        elif structured.get("status") == "extension_requested":
            if extension_request is None:
                failures.append("Claude returned an invalid extension request")
            else:
                worker_warnings.append("Claude requested a bounded continuation; Codex decision required")
        elif structured.get("status") != "complete":
            failures.append(f"Claude reported invalid status {structured.get('status')!r}")
        if unauthorized:
            failures.append("out-of-scope changed paths detected")

        if not failures and not extension_requested:
            validation_record, validation_failure = _run_validation(task, worktree, artifact_dir)
            if validation_failure:
                failures.append(validation_failure)
        else:
            validation_record = {
                "requested": list(task.validation_command) if task.validation_command else None,
                "status": "not_run",
                "process": None,
            }

        primary_after = _git(task.repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        primary_unchanged = primary_before == primary_after
        if not primary_unchanged:
            failures.append("primary checkout changed during delegation")
        (artifact_dir / "primary-status.before.txt").write_text(primary_before, encoding="utf-8")
        (artifact_dir / "primary-status.after.txt").write_text(primary_after, encoding="utf-8")

        checkpoint_recoverable = bool(
            checkpoint_format_failed
            and failures == ["checkpoint structured output failed after two bounded formatting attempts"]
        )
        can_continue = bool(extension_requested and not failures)
        record = {
            "kind": "task",
            "task_id": task.task_id,
            "status": (
                "checkpoint_failed"
                if checkpoint_recoverable
                else (
                    "failed"
                    if failures
                    else ("extension_requested" if extension_requested else "complete")
                )
            ),
            "lifecycle_status": _lifecycle_status(
                failures=failures,
                review_required=task.review_required,
                extension_requested=extension_requested,
                checkpoint_format_failed=checkpoint_recoverable,
            ),
            "plan_status": task.plan_status,
            "risk": task.risk,
            "review_required": task.review_required,
            "artifact_directory": str(artifact_dir),
            "repository": str(task.repo_root),
            "starting_commit": task.base_commit,
            "worktree": str(worktree),
            "branch": branch,
            "preflight": preflight.public_record(),
            "requested_model": task.model,
            "actual_model": actual_model(envelope),
            "api_error_status": envelope.get("api_error_status") if envelope else None,
            "error_kind": "checkpoint_format_error" if checkpoint_recoverable else (None if cap_recovered else classify_error(envelope)),
            "error_message": (
                None
                if cap_recovered
                else (envelope.get("result") if envelope and envelope.get("is_error") is True else None)
            ),
            "session_id": envelope.get("session_id") if envelope else None,
            "process": _process_record(process),
            "claude_claim": structured,
            "changed_paths": changed,
            "unauthorized_changed_paths": unauthorized,
            "diff_path": str(artifact_dir / "diff.patch"),
            "validation": validation_record,
            "primary_checkout_unchanged": primary_unchanged,
            "observed_metrics": observed_metrics(envelope),
            "usage_gate": usage_decision,
            "worker_warnings": worker_warnings,
            "failures": failures,
            "extension_request": extension_request,
            "continuation": {
                "extensions_used": 0,
                "total_granted_turns": total_authorized_turns,
                "can_continue": can_continue,
                "policy": "until_complete_or_usage_pause",
                "legacy_declared_max_extensions": task.max_extensions,
                "legacy_declared_max_total_turns": task.max_total_turns,
                "can_repair_checkpoint": checkpoint_recoverable,
            },
            "segments": [
                {
                    "index": 0,
                    "label": "initial",
                    "granted_turns": task.max_turns,
                    "turn_cap_reached": segment["turn_cap_reached"],
                    "process": _process_record(process),
                    "checkpoint_process": (
                        _process_record(segment["checkpoint_process"])
                        if segment["checkpoint_process"] is not None
                        else None
                    ),
                    "checkpoint_turns": checkpoint_turns,
                    "checkpoint_error": segment["checkpoint_error"],
                    "checkpoint_subtype": (
                        segment["checkpoint_envelope"].get("subtype")
                        if isinstance(segment["checkpoint_envelope"], dict)
                        else None
                    ),
                    "session_id": segment["session_id"],
                    "worktree_fingerprint": fingerprint,
                }
            ],
        }
        _write_json(artifact_dir / "result.json", record)
        return record

    def continue_task(
        self,
        task_file: str | Path,
        artifact_directory: str | Path,
        granted_turns: int,
        override_five_hour_soft_limit: bool = False,
    ) -> dict[str, Any]:
        task = load_task(task_file)
        if not isinstance(granted_turns, int) or isinstance(granted_turns, bool) or not 1 <= granted_turns <= 24:
            raise BridgeError("granted_turns must be an integer from 1 through 24")
        if not isinstance(override_five_hour_soft_limit, bool):
            raise BridgeError("override_five_hour_soft_limit must be a boolean")
        with _active_task_lock(self.artifact_root, task, task_file, operation="continue"):
            _verify_repository(task)
            artifact_dir = Path(artifact_directory).resolve(strict=True)
            if self.artifact_root not in artifact_dir.parents:
                raise BridgeError("artifact directory is outside the configured artifact root")
            result_path = artifact_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("task_id") != task.task_id or result.get("starting_commit") != task.base_commit:
                raise BridgeError("artifact result does not match the supplied task")
            artifact_task = load_task(artifact_dir / "task.json")
            if artifact_task.raw != task.raw:
                raise BridgeError("continuation task contract differs from the original artifact task")
            awaiting_extension = bool(
                (
                    result.get("status") == "extension_requested"
                    and result.get("lifecycle_status") == "EXTENSION_REQUESTED"
                )
                or (
                    result.get("status") == "cap_exhausted"
                    and result.get("lifecycle_status") == "CAP_EXHAUSTED"
                )
            )
            if not awaiting_extension:
                raise BridgeError("artifact is not awaiting a Codex continuation decision")
            request = result.get("extension_request")
            if not isinstance(request, dict):
                raise BridgeError("artifact does not contain a valid extension request")
            requested_turns = request.get("requested_turns")
            if not isinstance(requested_turns, int) or isinstance(requested_turns, bool):
                raise BridgeError("artifact extension request has an invalid turn request")
            if granted_turns > requested_turns:
                raise BridgeError("grant cannot exceed the turns Claude requested")
            continuation = result.get("continuation")
            if not isinstance(continuation, dict):
                raise BridgeError("artifact continuation accounting is missing")
            extensions_used = continuation.get("extensions_used")
            total_granted = continuation.get("total_granted_turns")
            if not isinstance(extensions_used, int) or not isinstance(total_granted, int):
                raise BridgeError("artifact continuation accounting is invalid")
            segments = list(result.get("segments") or [])
            session_id = _trusted_session_id(artifact_dir, segments)
            if result.get("session_id") != session_id:
                raise BridgeError("artifact result session ID does not match its trusted segment lineage")
            worktree = Path(result.get("worktree", "")).resolve(strict=True)
            if task.mode in {"implement", "test"}:
                if artifact_dir not in worktree.parents:
                    raise BridgeError("artifact worktree is outside its artifact directory")
            elif worktree != task.repo_root:
                raise BridgeError("read-only continuation worktree does not match the repository")

            decision = self._usage_decision(
                result.get("actual_model") or result.get("requested_model"),
                override_requested=override_five_hour_soft_limit,
            )
            if not decision["allowed"]:
                continuation["last_decision"] = _paused_decision(decision)
                result["continuation"] = continuation
                result["usage_gate"] = decision
                result["usage_before_continuation"] = decision["usage"]
                if decision["reason"] == "usage_unavailable":
                    result["worker_warnings"] = list(result.get("worker_warnings") or []) + [
                        "continuation paused because current Claude usage could not be verified"
                    ]
                _write_json(result_path, result)
                return result
            usage_before = decision["usage"]
            continuation["last_decision"] = {
                "decision": "granted",
                "granted_turns": granted_turns,
                "five_hour_soft_limit_override": False,
                "legacy_override_requested": override_five_hour_soft_limit,
                "captured_at": decision["captured_at"],
                "usage": usage_before,
                "gate": decision["gate"],
            }
            result["continuation"] = continuation
            _write_json(result_path, result)
            preflight = inspect_preflight()
            segment_number = extensions_used + 2
            label = f"segment-{segment_number}"
            prompt = _continuation_prompt(task, request, granted_turns)
            (artifact_dir / f"{label}.assignment.txt").write_text(prompt, encoding="utf-8")
            prior_fingerprint = _worktree_fingerprint(worktree, task.base_commit)
            segment = _run_work_segment(
                preflight=preflight,
                task=task,
                worktree=worktree,
                artifact_dir=artifact_dir,
                prompt=prompt,
                turns=granted_turns,
                label=label,
                resume_session_id=session_id,
                checkpoint_allowed=True,
            )
            return self._finish_resumed_segment(
                task=task,
                artifact_dir=artifact_dir,
                result_path=result_path,
                result=result,
                segments=segments,
                session_id=session_id,
                segment=segment,
                label=label,
                segment_index=segment_number - 1,
                granted_turns=granted_turns,
                worktree=worktree,
                prior_fingerprint=prior_fingerprint,
                prior_request=request,
                usage_before=usage_before,
                continuation=continuation,
                extensions_used=extensions_used,
                total_granted=total_granted,
                kind="continuation",
            )

    def _finish_resumed_segment(
        self,
        *,
        task: Task,
        artifact_dir: Path,
        result_path: Path,
        result: dict[str, Any],
        segments: list[Any],
        session_id: str,
        segment: dict[str, Any],
        label: str,
        segment_index: int,
        granted_turns: int,
        worktree: Path,
        prior_fingerprint: str,
        prior_request: dict[str, Any] | None,
        usage_before: dict[str, Any] | None,
        continuation: dict[str, Any],
        extensions_used: int,
        total_granted: int,
        kind: str,
    ) -> dict[str, Any]:
        """Apply the shared scope, session, progress, validation, and primary-checkout checks to a resumed segment."""
        process = segment["process"]
        envelope = segment["envelope"]
        structured = segment["structured"]
        parse_error = segment["parse_error"]
        changed = _changed_paths(worktree, task.base_commit)
        unauthorized = [path for path in changed if not _path_is_allowed(path, task.allowed_changed_paths)]
        diff = _complete_diff(worktree, task.base_commit)
        (artifact_dir / "diff.patch").write_text(diff, encoding="utf-8")
        _write_json(artifact_dir / "changed-paths.json", changed)
        fingerprint = _worktree_fingerprint(worktree, task.base_commit)

        failures: list[str] = []
        warnings = list(result.get("worker_warnings") or [])
        extension_request = _extension_request(structured)
        extension_requested = extension_request is not None
        cap_recovered = segment["turn_cap_reached"] and isinstance(structured, dict)
        checkpoint_format_failed = _checkpoint_format_failed(segment)
        if process.timed_out:
            failures.append(f"Claude {kind} timed out")
        if checkpoint_format_failed:
            failures.append("checkpoint structured output failed after two bounded formatting attempts")
        elif process.exit_code != 0 and not cap_recovered:
            failures.append(f"Claude {kind} exited with {process.exit_code}")
        if parse_error and not cap_recovered and not checkpoint_format_failed:
            failures.append(parse_error)
        if envelope and envelope.get("is_error") is True and not cap_recovered and not checkpoint_format_failed:
            failures.append(f"Claude reported an error during {kind}")
        if structured is None:
            if not checkpoint_format_failed:
                failures.append("missing structured_output")
        elif structured.get("status") == "blocked":
            failures.append("Claude reported status 'blocked'")
        elif structured.get("status") == "partial":
            warnings.append("Claude reported status 'partial'; independent evidence decides acceptance")
        elif structured.get("status") == "extension_requested":
            if extension_request is None:
                failures.append("Claude returned an invalid extension request")
            else:
                warnings.append("Claude requested another bounded continuation; Codex decision required")
        elif structured.get("status") != "complete":
            failures.append(f"Claude reported invalid status {structured.get('status')!r}")
        if unauthorized:
            failures.append("out-of-scope changed paths detected")
        resumed_session_id = segment.get("session_id")
        if resumed_session_id != session_id:
            failures.append(f"Claude {kind} returned a different session ID")
        else:
            candidate_segment = {
                "label": label,
                "session_id": resumed_session_id,
                "checkpoint_process": segment.get("checkpoint_process"),
            }
            try:
                _trusted_session_id(artifact_dir, segments + [candidate_segment])
            except BridgeError as exc:
                failures.append(str(exc))

        if task.mode in {"implement", "test"}:
            progress_changed = fingerprint != prior_fingerprint
        else:
            progress_changed = extension_request != prior_request
        if kind == "revision" and not progress_changed:
            failures.append("revision made no measurable change to the worktree")
        elif extension_requested and not progress_changed:
            failures.append("continuation requested more turns without measurable new progress")

        if not failures and not extension_requested:
            validation, validation_failure = _run_validation(task, worktree, artifact_dir)
            if validation_failure:
                failures.append(validation_failure)
        else:
            validation = {
                "requested": list(task.validation_command) if task.validation_command else None,
                "status": "not_run",
                "process": None,
            }

        before_path = artifact_dir / "primary-status.before.txt"
        primary_before = before_path.read_text(encoding="utf-8") if before_path.exists() else None
        primary_after = _git(task.repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        primary_unchanged = primary_before is not None and primary_before == primary_after
        (artifact_dir / "primary-status.after.txt").write_text(primary_after, encoding="utf-8")
        if not primary_unchanged:
            failures.append("primary checkout changed during delegation")

        try:
            usage_after: dict[str, Any] | None = self.usage(force_refresh=True)
        except (UsageError, PreflightError) as exc:
            usage_after = None
            warnings.append(f"post-segment usage check unavailable: {exc}")

        # Revisions are Codex review feedback, never counted as worker-requested extensions.
        next_extensions_used = extensions_used + (1 if kind == "continuation" else 0)
        checkpoint_turns = CHECKPOINT_TURN_RESERVE if segment["checkpoint_process"] is not None else 0
        next_total_granted = total_granted + granted_turns + checkpoint_turns
        checkpoint_recoverable = bool(
            checkpoint_format_failed
            and failures == ["checkpoint structured output failed after two bounded formatting attempts"]
        )
        can_continue = bool(extension_requested and not failures)
        segments = list(segments)
        segments.append(
            {
                "index": segment_index,
                "label": label,
                "kind": kind,
                "granted_turns": granted_turns,
                "turn_cap_reached": segment["turn_cap_reached"],
                "process": _process_record(process),
                "checkpoint_process": (
                    _process_record(segment["checkpoint_process"])
                    if segment["checkpoint_process"] is not None
                    else None
                ),
                "checkpoint_turns": checkpoint_turns,
                "checkpoint_error": segment["checkpoint_error"],
                "checkpoint_subtype": (
                    segment["checkpoint_envelope"].get("subtype")
                    if isinstance(segment["checkpoint_envelope"], dict)
                    else None
                ),
                "session_id": segment["session_id"] or session_id,
                "worktree_fingerprint": fingerprint,
                "progress_changed": progress_changed,
                "usage_before": usage_before,
                "usage_after": usage_after,
            }
        )
        status = (
            "checkpoint_failed"
            if checkpoint_recoverable
            else ("failed" if failures else ("extension_requested" if extension_requested else "complete"))
        )
        lifecycle_status = _lifecycle_status(
            failures=failures,
            review_required=task.review_required,
            extension_requested=extension_requested,
            checkpoint_format_failed=checkpoint_recoverable,
        )
        result.update(
            {
                "status": status,
                "lifecycle_status": lifecycle_status,
                "actual_model": actual_model(envelope) or result.get("actual_model"),
                "api_error_status": envelope.get("api_error_status") if envelope else None,
                "error_kind": (
                    "checkpoint_format_error"
                    if checkpoint_recoverable
                    else (None if cap_recovered else classify_error(envelope))
                ),
                "error_message": (
                    None
                    if cap_recovered
                    else (envelope.get("result") if envelope and envelope.get("is_error") is True else None)
                ),
                "session_id": session_id,
                "process": _process_record(process),
                "claude_claim": structured,
                "changed_paths": changed,
                "unauthorized_changed_paths": unauthorized,
                "validation": validation,
                "primary_checkout_unchanged": primary_unchanged,
                "observed_metrics": observed_metrics(envelope),
                "worker_warnings": warnings,
                "failures": failures,
                "extension_request": extension_request,
                "continuation": {
                    "extensions_used": next_extensions_used,
                    "total_granted_turns": next_total_granted,
                    "can_continue": can_continue,
                    "policy": "until_complete_or_usage_pause",
                    "legacy_declared_max_extensions": task.max_extensions,
                    "legacy_declared_max_total_turns": task.max_total_turns,
                    "last_decision": continuation.get("last_decision"),
                    "can_repair_checkpoint": checkpoint_recoverable,
                },
                "segments": segments,
                f"usage_before_{kind}": usage_before,
                f"usage_after_{kind}": usage_after,
            }
        )
        if kind == "revision" and isinstance(result.get("revisions"), list) and result["revisions"]:
            result["revisions"][-1].update(
                {"outcome_status": status, "outcome_lifecycle_status": lifecycle_status, "worktree_fingerprint": fingerprint}
            )
        _write_json(result_path, result)
        return result

    def revise(
        self,
        task_file: str | Path,
        artifact_directory: str | Path,
        feedback_file: str | Path,
        granted_turns: int,
    ) -> dict[str, Any]:
        task = load_task(task_file)
        if (
            not isinstance(granted_turns, int)
            or isinstance(granted_turns, bool)
            or not 1 <= granted_turns <= MAX_CLAUDE_REVISION_TURNS
        ):
            raise BridgeError(f"revision grant must be an integer from 1 through {MAX_CLAUDE_REVISION_TURNS}")
        if task.mode not in {"implement", "test"}:
            raise BridgeError("revision requires an implement or test task")
        with _active_task_lock(self.artifact_root, task, task_file, operation="revise"):
            _verify_repository(task)
            artifact_dir = Path(artifact_directory).resolve(strict=True)
            if self.artifact_root not in artifact_dir.parents:
                raise BridgeError("artifact directory is outside the configured artifact root")
            result_path = artifact_dir / "result.json"
            result_bytes = result_path.read_bytes()
            result = json.loads(result_bytes.decode("utf-8"))
            if result.get("task_id") != task.task_id or result.get("starting_commit") != task.base_commit:
                raise BridgeError("artifact result does not match the supplied task")
            artifact_task = load_task(artifact_dir / "task.json")
            if artifact_task.raw != task.raw:
                raise BridgeError("revision task contract differs from the original artifact task")
            target_state = revision_target_state(result)
            continuation = result.get("continuation")
            if not isinstance(continuation, dict):
                raise BridgeError("artifact continuation accounting is missing")
            extensions_used = continuation.get("extensions_used")
            total_granted = continuation.get("total_granted_turns")
            if not isinstance(extensions_used, int) or not isinstance(total_granted, int):
                raise BridgeError("artifact continuation accounting is invalid")
            segments = list(result.get("segments") or [])
            session_id = _trusted_session_id(artifact_dir, segments)
            if result.get("session_id") != session_id:
                raise BridgeError("artifact result session ID does not match its trusted segment lineage")
            worktree_value = result.get("worktree")
            if not isinstance(worktree_value, str) or not worktree_value:
                raise BridgeError("artifact has no recorded worktree")
            worktree = Path(worktree_value).resolve(strict=True)
            if artifact_dir not in worktree.parents:
                raise BridgeError("artifact worktree is outside its artifact directory")
            if _git(worktree, ["rev-parse", "HEAD"]).stdout.strip().lower() != task.base_commit:
                raise BridgeError("artifact worktree HEAD moved away from the original base commit")
            recorded_fingerprint = segments[-1].get("worktree_fingerprint")
            prior_fingerprint = _worktree_fingerprint(worktree, task.base_commit)
            if not isinstance(recorded_fingerprint, str) or prior_fingerprint != recorded_fingerprint:
                raise BridgeError("artifact worktree changed after its recorded result; revision refused")
            feedback = load_feedback(
                feedback_file, allowed_changed_paths=task.allowed_changed_paths, worktree=worktree
            )
            revisions = list(result.get("revisions") or [])
            if any(isinstance(item, dict) and item.get("feedback_sha256") == feedback["sha256"] for item in revisions):
                raise BridgeError("identical review feedback was already applied to this artifact")

            decision = self._usage_decision(result.get("actual_model") or result.get("requested_model"))
            if not decision["allowed"]:
                result["last_revision_decision"] = _paused_decision(decision)
                result["usage_gate"] = decision
                _write_json(result_path, result)
                return result

            number = len(revisions) + 1
            label = f"revision-{number}"
            prior_result_file = f"{label}.prior-result.json"
            feedback_record_file = f"{label}.feedback.json"
            with (artifact_dir / prior_result_file).open("xb") as handle:
                handle.write(result_bytes)
            with (artifact_dir / feedback_record_file).open("x", encoding="utf-8") as handle:
                json.dump({"findings": feedback["findings"]}, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            prompt = _revision_prompt(task, feedback["findings"], granted_turns)
            (artifact_dir / f"{label}.assignment.txt").write_text(prompt, encoding="utf-8")
            revisions.append(
                {
                    "index": number,
                    "label": label,
                    "granted_turns": granted_turns,
                    "feedback_file": feedback_record_file,
                    "feedback_sha256": feedback["sha256"],
                    "finding_count": len(feedback["findings"]),
                    "prior_result_file": prior_result_file,
                    "prior_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                    "prior_status": result.get("status"),
                    "prior_lifecycle_status": result.get("lifecycle_status"),
                    "prior_failures": list(result.get("failures") or []),
                    "target_state": target_state,
                    "prior_worktree_fingerprint": prior_fingerprint,
                    "captured_at": decision["captured_at"],
                }
            )
            result["revisions"] = revisions
            result["usage_gate"] = decision
            result["last_revision_decision"] = {
                "decision": "granted",
                "granted_turns": granted_turns,
                "captured_at": decision["captured_at"],
                "usage": decision["usage"],
                "gate": decision["gate"],
            }
            _write_json(result_path, result)
            preflight = inspect_preflight()
            segment = _run_work_segment(
                preflight=preflight,
                task=task,
                worktree=worktree,
                artifact_dir=artifact_dir,
                prompt=prompt,
                turns=granted_turns,
                label=label,
                resume_session_id=session_id,
                checkpoint_allowed=True,
            )
            return self._finish_resumed_segment(
                task=task,
                artifact_dir=artifact_dir,
                result_path=result_path,
                result=result,
                segments=segments,
                session_id=session_id,
                segment=segment,
                label=label,
                segment_index=len(segments),
                granted_turns=granted_turns,
                worktree=worktree,
                prior_fingerprint=prior_fingerprint,
                prior_request=None,
                usage_before=decision["usage"],
                continuation=continuation,
                extensions_used=extensions_used,
                total_granted=total_granted,
                kind="revision",
            )

    def repair_checkpoint(
        self,
        task_file: str | Path,
        artifact_directory: str | Path,
        override_five_hour_soft_limit: bool = False,
    ) -> dict[str, Any]:
        task = load_task(task_file)
        if not isinstance(override_five_hour_soft_limit, bool):
            raise BridgeError("override_five_hour_soft_limit must be a boolean")
        with _active_task_lock(self.artifact_root, task, task_file, operation="repair-checkpoint"):
            _verify_repository(task)
            artifact_dir = Path(artifact_directory).resolve(strict=True)
            if self.artifact_root not in artifact_dir.parents:
                raise BridgeError("artifact directory is outside the configured artifact root")
            result_path = artifact_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("task_id") != task.task_id or result.get("starting_commit") != task.base_commit:
                raise BridgeError("artifact result does not match the supplied task")
            artifact_task = load_task(artifact_dir / "task.json")
            if artifact_task.raw != task.raw:
                raise BridgeError("checkpoint repair task contract differs from the original artifact task")
            segments = list(result.get("segments") or [])
            if not segments or not isinstance(segments[-1], dict):
                raise BridgeError("artifact has no checkpoint segment to repair")
            last_segment = segments[-1]
            legacy_checkpoint_failure = bool(
                result.get("status") == "failed"
                and last_segment.get("turn_cap_reached") is True
                and last_segment.get("checkpoint_process") is not None
                and result.get("claude_claim") is None
                and not result.get("unauthorized_changed_paths")
                and result.get("primary_checkout_unchanged") is True
                and result.get("process", {}).get("timed_out") is False
            )
            if not (
                result.get("status") == "checkpoint_failed"
                and result.get("lifecycle_status") == "CHECKPOINT_FORMAT_FAILED"
                and not result.get("unauthorized_changed_paths")
                and result.get("primary_checkout_unchanged") is True
                and result.get("failures")
                == ["checkpoint structured output failed after two bounded formatting attempts"]
            ) and not legacy_checkpoint_failure:
                raise BridgeError("artifact is not a recoverable checkpoint-format failure")
            continuation = result.get("continuation")
            if not isinstance(continuation, dict):
                raise BridgeError("artifact continuation accounting is missing")
            total_granted = continuation.get("total_granted_turns")
            extensions_used = continuation.get("extensions_used")
            if not isinstance(total_granted, int) or not isinstance(extensions_used, int):
                raise BridgeError("artifact continuation accounting is invalid")
            session_id = _trusted_session_id(artifact_dir, segments)
            if result.get("session_id") != session_id:
                raise BridgeError("artifact result session ID does not match its trusted segment lineage")
            worktree = Path(result.get("worktree", "")).resolve(strict=True)
            if task.mode in {"implement", "test"}:
                if artifact_dir not in worktree.parents:
                    raise BridgeError("artifact worktree is outside its artifact directory")
            elif worktree != task.repo_root:
                raise BridgeError("read-only checkpoint worktree does not match the repository")

            decision = self._usage_decision(
                result.get("actual_model") or result.get("requested_model"),
                override_requested=override_five_hour_soft_limit,
            )
            if not decision["allowed"]:
                result["checkpoint_recovery"] = _paused_decision(decision)
                result["usage_gate"] = decision
                _write_json(result_path, result)
                return result
            usage_before = decision["usage"]

            preflight = inspect_preflight()
            repairs = list(last_segment.get("checkpoint_repairs") or [])
            if repairs:
                raise BridgeError("artifact already used its one bounded checkpoint repair")
            repair_number = len(repairs) + 1
            label = last_segment.get("label")
            if not isinstance(label, str) or not label:
                raise BridgeError("artifact checkpoint segment label is invalid")
            checkpoint = _run_checkpoint(
                preflight=preflight,
                task=task,
                worktree=worktree,
                artifact_dir=artifact_dir,
                session_id=session_id,
                output_stem=f"{label}.checkpoint-repair-{repair_number}",
            )
            envelope = checkpoint["envelope"]
            returned_session = envelope.get("session_id") if isinstance(envelope, dict) else None
            failures: list[str] = []
            structured = checkpoint["structured"]
            if checkpoint["process"].timed_out:
                failures.append("checkpoint repair timed out")
            if checkpoint["process"].exit_code != 0:
                failures.append(f"checkpoint repair exited with {checkpoint['process'].exit_code}")
            if checkpoint["error"]:
                failures.append(checkpoint["error"])
            if not isinstance(structured, dict):
                failures.append("checkpoint repair returned no structured output")
            if returned_session != session_id:
                failures.append("checkpoint repair returned a different session ID")

            extension_request = _extension_request(structured)
            extension_requested = extension_request is not None
            if isinstance(structured, dict):
                status = structured.get("status")
                if status == "blocked":
                    failures.append("Claude reported status 'blocked'")
                elif status == "extension_requested" and extension_request is None:
                    failures.append("Claude returned an invalid extension request")
                elif status not in {"complete", "extension_requested"}:
                    failures.append(f"Claude reported invalid status {status!r}")

            repair_record = {
                "index": repair_number,
                "process": _process_record(checkpoint["process"]),
                "stdout_file": checkpoint["stdout_file"],
                "stderr_file": checkpoint["stderr_file"],
                "subtype": envelope.get("subtype") if isinstance(envelope, dict) else None,
                "structured_valid": isinstance(structured, dict),
                "usage_before": usage_before,
            }
            repairs.append(repair_record)
            last_segment["checkpoint_repairs"] = repairs
            last_segment["checkpoint_turns"] = int(last_segment.get("checkpoint_turns") or 0) + CHECKPOINT_TURN_RESERVE
            segments[-1] = last_segment
            next_total = total_granted + CHECKPOINT_TURN_RESERVE
            can_continue = bool(extension_requested and not failures)

            changed = _changed_paths(worktree, task.base_commit)
            unauthorized = [path for path in changed if not _path_is_allowed(path, task.allowed_changed_paths)]
            if unauthorized:
                failures.append("out-of-scope changed paths detected")
            (artifact_dir / "diff.patch").write_text(_complete_diff(worktree, task.base_commit), encoding="utf-8")
            _write_json(artifact_dir / "changed-paths.json", changed)
            primary_before_path = artifact_dir / "primary-status.before.txt"
            primary_before = primary_before_path.read_text(encoding="utf-8") if primary_before_path.exists() else None
            primary_after = _git(task.repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            primary_unchanged = primary_before is not None and primary_before == primary_after
            (artifact_dir / "primary-status.after.txt").write_text(primary_after, encoding="utf-8")
            if not primary_unchanged:
                failures.append("primary checkout changed during checkpoint repair")

            if not failures and not extension_requested:
                validation, validation_failure = _run_validation(task, worktree, artifact_dir)
                if validation_failure:
                    failures.append(validation_failure)
            else:
                validation = {
                    "requested": list(task.validation_command) if task.validation_command else None,
                    "status": "not_run",
                    "process": None,
                }
            try:
                usage_after: dict[str, Any] | None = self.usage(force_refresh=True)
            except (UsageError, PreflightError):
                usage_after = None
            repair_record["usage_after"] = usage_after

            repair_format_failed = not isinstance(structured, dict)
            result.update(
                {
                    "status": (
                        "failed"
                        if failures
                        else (
                            "extension_requested" if extension_requested else "complete"
                        )
                    ),
                    "lifecycle_status": _lifecycle_status(
                        failures=failures,
                        review_required=task.review_required,
                        extension_requested=extension_requested,
                    ),
                    "error_kind": "checkpoint_format_error" if repair_format_failed else None,
                    "error_message": None,
                    "claude_claim": structured,
                    "changed_paths": changed,
                    "unauthorized_changed_paths": unauthorized,
                    "validation": validation,
                    "primary_checkout_unchanged": primary_unchanged,
                    "failures": failures,
                    "extension_request": extension_request,
                    "continuation": {
                        "extensions_used": extensions_used,
                        "total_granted_turns": next_total,
                        "can_continue": can_continue,
                        "policy": "until_complete_or_usage_pause",
                        "legacy_declared_max_extensions": task.max_extensions,
                        "legacy_declared_max_total_turns": task.max_total_turns,
                        "can_repair_checkpoint": False,
                    },
                    "segments": segments,
                    "checkpoint_recovery": {
                        "decision": "attempted",
                        "attempt": repair_number,
                        "five_hour_soft_limit_override": False,
                        "legacy_override_requested": override_five_hour_soft_limit,
                        "usage_gate": decision["gate"],
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "usage_before": usage_before,
                        "usage_after": usage_after,
                        "prior_failures": list(result.get("failures") or []),
                    },
                }
            )
            _write_json(result_path, result)
            return result

    def revalidate(self, task_file: str | Path, artifact_directory: str | Path) -> dict[str, Any]:
        task = load_task(task_file)
        with _active_task_lock(self.artifact_root, task, task_file, operation="revalidate"):
            return self._revalidate_ready_task(task, artifact_directory)

    def _revalidate_ready_task(self, task: Task, artifact_directory: str | Path) -> dict[str, Any]:
        _verify_repository(task)
        artifact_dir = Path(artifact_directory).resolve(strict=True)
        if self.artifact_root not in artifact_dir.parents:
            raise BridgeError("artifact directory is outside the configured artifact root")
        result_path = artifact_dir / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("task_id") != task.task_id or result.get("starting_commit") != task.base_commit:
            raise BridgeError("artifact result does not match the supplied task")
        worktree = Path(result.get("worktree", "")).resolve(strict=True)
        if artifact_dir not in worktree.parents:
            raise BridgeError("artifact worktree is outside its artifact directory")

        changed = _changed_paths(worktree, task.base_commit)
        unauthorized = [path for path in changed if not _path_is_allowed(path, task.allowed_changed_paths)]
        diff = _complete_diff(worktree, task.base_commit)
        (artifact_dir / "diff.patch").write_text(diff, encoding="utf-8")
        _write_json(artifact_dir / "changed-paths.json", changed)

        failures: list[str] = []
        worker_warnings = list(result.get("worker_warnings") or [])
        claim = result.get("claude_claim")
        claim_status = claim.get("status") if isinstance(claim, dict) else None
        if claim_status == "blocked":
            failures.append("Claude reported status 'blocked'")
        elif claim_status == "partial" and not worker_warnings:
            worker_warnings.append("Claude reported status 'partial'; independent evidence decides acceptance")
        if unauthorized:
            failures.append("out-of-scope changed paths detected")

        if not failures:
            validation, validation_failure = _run_validation(task, worktree, artifact_dir)
            if validation_failure:
                failures.append(validation_failure)
        else:
            validation = {
                "requested": list(task.validation_command) if task.validation_command else None,
                "status": "not_run",
                "process": None,
            }

        before_path = artifact_dir / "primary-status.before.txt"
        primary_before = before_path.read_text(encoding="utf-8") if before_path.exists() else None
        primary_after = _git(task.repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        primary_unchanged = primary_before is not None and primary_before == primary_after
        (artifact_dir / "primary-status.after.txt").write_text(primary_after, encoding="utf-8")
        if not primary_unchanged:
            failures.append("primary checkout changed during delegation")

        result.update(
            {
                "status": "complete" if not failures else "failed",
                "lifecycle_status": _lifecycle_status(failures=failures, review_required=task.review_required),
                "changed_paths": changed,
                "unauthorized_changed_paths": unauthorized,
                "validation": validation,
                "primary_checkout_unchanged": primary_unchanged,
                "worker_warnings": worker_warnings,
                "failures": failures,
                "revalidated_without_claude": True,
            }
        )
        _write_json(result_path, result)
        return result


def denied_task_record(task_file: str | Path, artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT) -> dict[str, Any]:
    try:
        load_task(task_file)
    except (ContractError, OSError) as exc:
        return {"status": "denied", "reason": str(exc), "process_started": False}
    return {"status": "accepted", "reason": None, "process_started": False}
