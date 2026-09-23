from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_claude_bridge.contracts import ContractError, validate_task


def valid_task(repo: Path) -> dict:
    return {
        "task_id": "fixture-001",
        "repo_root": str(repo.resolve()),
        "base_commit": "a" * 40,
        "mode": "analyze",
        "objective": "Read the fixture.",
        "context_paths": ["README.md"],
        "forbidden_context": ["All unrelated repositories"],
        "allowed_changed_paths": [],
        "acceptance_criteria": ["No changes"],
        "model": None,
        "max_turns": 2,
        "timeout_seconds": 30,
        "allow_subagents": False,
        "require_subscription_auth": True,
    }


class ContractTests(unittest.TestCase):
    def test_valid_read_only_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = validate_task(valid_task(Path(temp)))
        self.assertEqual(task.mode, "analyze")
        self.assertEqual(task.plan_status, "READY")
        self.assertEqual(task.risk, "medium")
        self.assertTrue(task.review_required)
        self.assertIsNone(task.max_total_turns)
        self.assertIsNone(task.max_extensions)

    def test_explicit_routing_metadata_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw.update(
                {
                    "plan_status": "READY",
                    "risk": "low",
                    "review_required": False,
                    "locked_decisions": ["Keep the public interface"],
                    "stop_conditions": ["The fixture contradicts the assignment"],
                }
            )
            task = validate_task(raw)
        self.assertEqual(task.risk, "low")
        self.assertFalse(task.review_required)
        self.assertEqual(task.locked_decisions, ("Keep the public interface",))
        self.assertEqual(task.stop_conditions, ("The fixture contradicts the assignment",))

    def test_unready_plan_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["plan_status"] = "DRAFT"
            with self.assertRaisesRegex(ContractError, "must be READY"):
                validate_task(raw)

    def test_unknown_contract_fields_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["backend"] = "claude"
            with self.assertRaisesRegex(ContractError, "unsupported fields: backend"):
                validate_task(raw)

    def test_out_of_scope_context_is_denied_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["context_paths"] = ["../secret.txt"]
            with self.assertRaisesRegex(ContractError, "outside the repository"):
                validate_task(raw)

    def test_only_trailing_directory_wildcard_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["mode"] = "implement"
            raw["allowed_changed_paths"] = ["src/package/**"]
            task = validate_task(raw)
            self.assertEqual(task.allowed_changed_paths, ("src/package/**",))
            raw["allowed_changed_paths"] = ["src/*/module.py"]
            with self.assertRaisesRegex(ContractError, "only a trailing /\\*\\*"):
                validate_task(raw)

    def test_legacy_cap_fields_remain_readable_but_have_no_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw.update({"max_total_turns": 200, "max_extensions": 30})
            task = validate_task(raw)
            self.assertEqual(task.max_total_turns, 200)
            self.assertEqual(task.max_extensions, 30)
            raw["max_total_turns"] = "unbounded"
            with self.assertRaisesRegex(ContractError, "legacy max_total_turns"):
                validate_task(raw)

    def test_analysis_changes_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["allowed_changed_paths"] = ["README.md"]
            with self.assertRaisesRegex(ContractError, "cannot allow changed paths"):
                validate_task(raw)

    def test_nested_agents_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = valid_task(Path(temp))
            raw["allow_subagents"] = True
            with self.assertRaisesRegex(ContractError, "requires allow_subagents"):
                validate_task(raw)


if __name__ == "__main__":
    unittest.main()
