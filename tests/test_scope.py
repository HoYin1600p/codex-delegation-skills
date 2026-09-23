from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_claude_bridge.contracts import validate_task
from codex_claude_bridge.launcher import (
    BridgeError,
    _active_task_lock,
    _extension_request,
    _path_is_allowed,
    _task_prompt,
)


class ScopeTests(unittest.TestCase):
    def test_exact_file_is_allowed(self) -> None:
        self.assertTrue(_path_is_allowed("src/greeting.py", ("src/greeting.py",)))

    def test_directory_boundary_is_allowed(self) -> None:
        self.assertTrue(_path_is_allowed("src/pkg/module.py", ("src/pkg",)))

    def test_prefix_collision_is_denied(self) -> None:
        self.assertFalse(_path_is_allowed("src/package-secret/file.py", ("src/package",)))

    def test_trailing_double_star_is_a_directory_boundary(self) -> None:
        self.assertTrue(_path_is_allowed("src/package/module.py", ("src/package/**",)))
        self.assertFalse(_path_is_allowed("src/package-secret/module.py", ("src/package/**",)))

    def test_extension_request_requires_specific_remaining_work(self) -> None:
        claim = {
            "status": "extension_requested",
            "extension_request": {
                "completed_work": ["Implemented parser"],
                "remaining_work": ["Add validation tests"],
                "reason": "The tests are needed to meet acceptance criteria",
                "requested_turns": 6,
            },
        }
        self.assertEqual(_extension_request(claim), claim["extension_request"])
        claim["extension_request"]["remaining_work"] = []
        self.assertIsNone(_extension_request(claim))

    def test_active_task_lock_rejects_duplicate_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file = root / "task.json"
            task_file.write_text("{}", encoding="utf-8")
            task = validate_task(
                {
                    "task_id": "lock-fixture-001",
                    "repo_root": str(root),
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
            )
            with _active_task_lock(root, task, task_file) as lock_path:
                self.assertTrue(lock_path.exists())
                with self.assertRaisesRegex(BridgeError, "already has an active run"):
                    with _active_task_lock(root, task, task_file):
                        pass
            self.assertFalse(lock_path.exists())
            with _active_task_lock(root, task, task_file):
                pass

    def test_task_prompt_carries_stop_and_review_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = validate_task(
                {
                    "task_id": "prompt-fixture-001",
                    "repo_root": str(root),
                    "base_commit": "b" * 40,
                    "mode": "analyze",
                    "objective": "Inspect the fixture",
                    "context_paths": ["README.md"],
                    "forbidden_context": ["Unrelated paths"],
                    "allowed_changed_paths": [],
                    "acceptance_criteria": ["No changes"],
                    "plan_status": "READY",
                    "risk": "high",
                    "review_required": True,
                    "locked_decisions": ["Do not redesign"],
                    "stop_conditions": ["Missing source file"],
                    "model": None,
                    "max_turns": 2,
                    "timeout_seconds": 30,
                    "allow_subagents": False,
                    "require_subscription_auth": True,
                }
            )
        prompt = _task_prompt(task)
        self.assertIn('"plan_status": "READY"', prompt)
        self.assertIn('"risk": "high"', prompt)
        self.assertIn("Missing source file", prompt)
        self.assertIn("extension_requested", prompt)
        self.assertIn("Edit tool can create new files", prompt)
        self.assertIn("bridge run independent validation", prompt)


if __name__ == "__main__":
    unittest.main()
