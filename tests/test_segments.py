from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_claude_bridge.auth import Preflight
from codex_claude_bridge.contracts import validate_task
from codex_claude_bridge.launcher import _complete_diff, _run_validation, _run_work_segment
from codex_claude_bridge.process import ProcessResult


def task_for(root: Path):
    return validate_task(
        {
            "task_id": "segment-fixture-001",
            "repo_root": str(root.resolve()),
            "base_commit": "a" * 40,
            "mode": "implement",
            "objective": "Implement the fixture",
            "context_paths": ["README.md"],
            "forbidden_context": ["Unrelated paths"],
            "allowed_changed_paths": ["src/**"],
            "acceptance_criteria": ["Fixture is implemented"],
            "model": None,
            "max_turns": 4,
            "max_total_turns": 12,
            "max_extensions": 2,
            "timeout_seconds": 30,
            "allow_subagents": False,
            "require_subscription_auth": True,
        }
    )


class SegmentTests(unittest.TestCase):
    def test_turn_cap_gets_a_no_tools_structured_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            preflight = Preflight(executable, "test", True, "claude.ai", "firstParty", "max", ())
            task = task_for(root)
            capped = ProcessResult(
                1,
                json.dumps(
                    {
                        "is_error": True,
                        "terminal_reason": "max_turns",
                        "subtype": "error_max_turns",
                        "session_id": "00000000-0000-4000-8000-000000000001",
                    }
                ),
                "",
                1.0,
                False,
                False,
                False,
            )
            checkpoint = ProcessResult(
                0,
                json.dumps(
                    {
                        "is_error": False,
                        "session_id": "00000000-0000-4000-8000-000000000001",
                        "structured_output": {
                            "status": "extension_requested",
                            "summary": "Parser completed; tests remain",
                            "findings": [],
                            "files_read": ["README.md"],
                            "files_changed": ["src/parser.py"],
                            "checks": [],
                            "blockers": [],
                            "extension_request": {
                                "completed_work": ["Implemented parser"],
                                "remaining_work": ["Add tests"],
                                "reason": "Tests remain",
                                "requested_turns": 4,
                            },
                        },
                    }
                ),
                "",
                0.2,
                False,
                False,
                False,
            )
            with patch("codex_claude_bridge.launcher.run_process", side_effect=[capped, checkpoint]) as runner:
                outcome = _run_work_segment(
                    preflight=preflight,
                    task=task,
                    worktree=root,
                    artifact_dir=root,
                    prompt="work",
                    turns=4,
                    label="initial",
                )
            self.assertTrue(outcome["turn_cap_reached"])
            self.assertEqual(outcome["structured"]["status"], "extension_requested")
            checkpoint_args = runner.call_args_list[1].args[1]
            self.assertIn("--resume", checkpoint_args)
            tools_index = checkpoint_args.index("--tools")
            self.assertEqual(checkpoint_args[tools_index + 1], "")
            turns_index = checkpoint_args.index("--max-turns")
            self.assertEqual(checkpoint_args[turns_index + 1], "2")
            schema_index = checkpoint_args.index("--json-schema")
            checkpoint_schema = json.loads(checkpoint_args[schema_index + 1])
            self.assertEqual(
                set(checkpoint_schema["required"]), {"status", "summary", "extension_request"}
            )
            self.assertNotIn("findings", checkpoint_schema["properties"])

    def test_complete_diff_contains_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            (root / "new.txt").write_text("preserved work\n", encoding="utf-8")
            diff = _complete_diff(root, base)
            self.assertIn("new file mode", diff)
            self.assertIn("preserved work", diff)

    def test_validation_resolves_repo_virtualenv_for_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            worktree = root / "worktree"
            artifact = root / "artifact"
            executable = repo / ".venv" / "Scripts" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            worktree.mkdir()
            artifact.mkdir()
            task = replace(
                task_for(repo),
                validation_command=(r".venv\Scripts\python.exe", "-m", "pytest", "-q"),
            )
            process = ProcessResult(0, "passed", "", 1.0, False, False, False)
            with patch("codex_claude_bridge.launcher.run_process", return_value=process) as runner:
                record, failure = _run_validation(task, worktree, artifact)
        self.assertIsNone(failure)
        self.assertEqual(record["status"], "passed")
        self.assertEqual(runner.call_args.args[0], executable.resolve())
        self.assertEqual(runner.call_args.kwargs["cwd"], worktree)


if __name__ == "__main__":
    unittest.main()
