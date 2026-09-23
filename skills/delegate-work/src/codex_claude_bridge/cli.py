from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auth import PreflightError
from .contracts import ContractError
from .handoff import export_handoff
from .launcher import (
    BridgeError,
    ClaudeBridge,
    DEFAULT_ARTIFACT_ROOT,
    active_task_record,
    clear_stale_task_lock,
    denied_task_record,
)
from .usage import UsageError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded local Codex to Claude Code bridge")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="Inspect Claude version, subscription auth, and overrides")
    subparsers.add_parser("usage", help="Read current Claude five-hour and weekly subscription usage")
    smoke = subparsers.add_parser("smoke", help="Run one no-tool READY smoke test")
    smoke.add_argument("--timeout-seconds", type=int, default=60)
    run = subparsers.add_parser("run", help="Run one validated task file")
    run.add_argument("--task", type=Path, required=True)
    continue_task = subparsers.add_parser("continue", help="Grant turns to a Claude extension request")
    continue_task.add_argument("--task", type=Path, required=True)
    continue_task.add_argument("--artifact", type=Path, required=True)
    continue_task.add_argument("--grant-turns", type=int, required=True)
    continue_task.add_argument(
        "--override-five-hour-soft-limit",
        action="store_true",
        help=(
            "Legacy compatibility flag; recorded only. It cannot bypass five-hour exhaustion (0%% remaining) "
            "or unavailable usage"
        ),
    )
    repair = subparsers.add_parser(
        "repair-checkpoint", help="Repair a failed structured checkpoint without resuming work"
    )
    repair.add_argument("--task", type=Path, required=True)
    repair.add_argument("--artifact", type=Path, required=True)
    repair.add_argument(
        "--override-five-hour-soft-limit",
        action="store_true",
        help=(
            "Legacy compatibility flag; recorded only. It cannot bypass five-hour exhaustion (0%% remaining) "
            "or unavailable usage"
        ),
    )
    revise = subparsers.add_parser(
        "revise", help="Resume a reviewed result's same session and worktree with explicit Codex review findings"
    )
    revise.add_argument("--task", type=Path, required=True)
    revise.add_argument("--artifact", type=Path, required=True)
    revise.add_argument("--feedback", type=Path, required=True)
    revise.add_argument("--grant-turns", type=int, required=True)
    export = subparsers.add_parser(
        "export-handoff", help="Export verified preserved worktree changes as a handoff package"
    )
    export.add_argument("--task", type=Path, required=True)
    export.add_argument("--artifact", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    revalidate = subparsers.add_parser("revalidate", help="Independently validate a preserved task worktree")
    revalidate.add_argument("--task", type=Path, required=True)
    revalidate.add_argument("--artifact", type=Path, required=True)
    deny = subparsers.add_parser("check-task", help="Validate a task without starting Claude")
    deny.add_argument("--task", type=Path, required=True)
    active = subparsers.add_parser("active", help="Inspect the local active-run record without contacting Claude")
    active.add_argument("--task-id", required=True)
    clear = subparsers.add_parser("clear-stale-lock", help="Archive a lock only when its owner process is no longer running")
    clear.add_argument("--task-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check-task":
            record = denied_task_record(args.task, args.artifact_root)
        elif args.command == "active":
            bridge = ClaudeBridge(args.artifact_root)
            record = active_task_record(bridge.artifact_root, args.task_id)
        elif args.command == "clear-stale-lock":
            bridge = ClaudeBridge(args.artifact_root)
            record = clear_stale_task_lock(bridge.artifact_root, args.task_id)
        else:
            bridge = ClaudeBridge(args.artifact_root)
            if args.command == "preflight":
                record = bridge.preflight()
            elif args.command == "usage":
                record = bridge.usage()
            elif args.command == "smoke":
                record = bridge.smoke(args.timeout_seconds)
            elif args.command == "continue":
                record = bridge.continue_task(
                    args.task,
                    args.artifact,
                    args.grant_turns,
                    override_five_hour_soft_limit=args.override_five_hour_soft_limit,
                )
            elif args.command == "repair-checkpoint":
                record = bridge.repair_checkpoint(
                    args.task,
                    args.artifact,
                    override_five_hour_soft_limit=args.override_five_hour_soft_limit,
                )
            elif args.command == "revise":
                record = bridge.revise(args.task, args.artifact, args.feedback, args.grant_turns)
            elif args.command == "export-handoff":
                record = export_handoff(bridge.artifact_root, args.task, args.artifact, args.output)
            elif args.command == "revalidate":
                record = bridge.revalidate(args.task, args.artifact)
            else:
                record = bridge.run(args.task)
    except (BridgeError, ContractError, PreflightError, UsageError, OSError, ValueError) as exc:
        record = {"status": "failed", "error": str(exc)}
        print(json.dumps(record, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0 if record.get("status") not in {"failed", "denied", "checkpoint_failed", "cap_exhausted", "usage_blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
