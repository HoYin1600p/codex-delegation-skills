from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_claude_bridge.contracts import load_task
from codex_claude_bridge.launcher import (
    BridgeError,
    ClaudeBridge,
    _active_task_lock,
    _lifecycle_status,
    active_task_record,
    clear_stale_task_lock,
)


def write_task(root: Path, *, task_id: str = "lifecycle-fixture-001") -> Path:
    task_file = root / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "repo_root": str(root.resolve()),
                "base_commit": "a" * 40,
                "mode": "analyze",
                "objective": "Inspect the fixture",
                "context_paths": ["README.md"],
                "forbidden_context": ["Unrelated paths"],
                "allowed_changed_paths": [],
                "acceptance_criteria": ["No changes"],
                "model": None,
                "max_turns": 2,
                "timeout_seconds": 30,
                "allow_subagents": False,
                "require_subscription_auth": True,
            }
        ),
        encoding="utf-8",
    )
    return task_file


class LifecycleTests(unittest.TestCase):
    def test_lifecycle_status_never_claims_acceptance(self) -> None:
        self.assertEqual(_lifecycle_status(failures=["validation failed"], review_required=True), "BLOCKED")
        self.assertEqual(_lifecycle_status(failures=[], review_required=True), "REVIEW_PENDING")
        self.assertEqual(_lifecycle_status(failures=[], review_required=False), "IMPLEMENTED")
        self.assertEqual(
            _lifecycle_status(failures=[], review_required=True, extension_requested=True),
            "EXTENSION_REQUESTED",
        )
        self.assertEqual(
            _lifecycle_status(
                failures=["checkpoint formatting failed"],
                review_required=True,
                checkpoint_format_failed=True,
            ),
            "CHECKPOINT_FORMAT_FAILED",
        )

    def test_live_lock_is_reported_and_cannot_be_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file = write_task(root)
            task = load_task(task_file)
            with _active_task_lock(root, task, task_file):
                record = active_task_record(root, task.task_id)
                self.assertEqual(record["status"], "RUNNING")
                self.assertTrue(record["process_running"])
                with self.assertRaisesRegex(BridgeError, "running owner process"):
                    clear_stale_task_lock(root, task.task_id)

    def test_stale_lock_is_archived_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active_dir = root / ".active"
            active_dir.mkdir()
            lock_path = active_dir / "stale-fixture-001.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "task_id": "stale-fixture-001",
                        "status": "RUNNING",
                        "operation": "run",
                        "pid": 2147483647,
                    }
                ),
                encoding="utf-8",
            )
            result = clear_stale_task_lock(root, "stale-fixture-001")
            self.assertEqual(result["status"], "STALE_LOCK_ARCHIVED")
            self.assertFalse(lock_path.exists())
            self.assertTrue(Path(result["archived_lock"]).exists())

    def test_revalidate_obeys_the_same_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file = write_task(root)
            task = load_task(task_file)
            bridge = ClaudeBridge(root)
            with _active_task_lock(root, task, task_file):
                with self.assertRaisesRegex(BridgeError, "already has an active run"):
                    bridge.revalidate(task_file, root / "not-needed")

    def test_separate_process_cannot_claim_an_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file = write_task(root)
            task = load_task(task_file)
            script = """
import sys
from pathlib import Path
from codex_claude_bridge.contracts import load_task
from codex_claude_bridge.launcher import BridgeError, _active_task_lock

artifact_root = Path(sys.argv[1])
task_file = Path(sys.argv[2])
try:
    with _active_task_lock(artifact_root, load_task(task_file), task_file):
        pass
except BridgeError:
    raise SystemExit(23)
raise SystemExit(0)
"""
            with _active_task_lock(root, task, task_file):
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(root), str(task_file)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=os.environ.copy(),
                    timeout=10,
                )
            self.assertEqual(completed.returncode, 23, completed.stderr)


if __name__ == "__main__":
    unittest.main()
