from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_claude_bridge.auth import Preflight
from codex_claude_bridge.launcher import BridgeError, ClaudeBridge, _worktree_fingerprint
from codex_claude_bridge.process import ProcessResult
from codex_claude_bridge.revision import RevisionError, load_feedback, path_within_scope
from codex_claude_bridge.usage import UsageError


SESSION_ID = "00000000-0000-4000-8000-000000000007"
OTHER_SESSION_ID = "00000000-0000-4000-8000-000000000008"
FEEDBACK = {
    "findings": [
        {
            "path": "src/app.txt",
            "issue": "The greeting omits the reviewer name required by the contract",
            "expected_behavior": "The file must contain exactly 'hello, reviewer' followed by a newline",
        }
    ]
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def usage(remaining: float = 60.0) -> dict:
    return {
        "status": "available",
        "five_hour": {"used_percent": 100.0 - remaining, "remaining_percent": remaining, "resets_at": "later"},
        "weekly": {"used_percent": 20.0, "remaining_percent": 80.0, "resets_at": "later"},
        "weekly_scoped": [],
        "source": "test",
    }


def complete_claim() -> dict:
    return {
        "status": "complete",
        "summary": "Corrected the review findings",
        "extension_request": {"completed_work": [], "remaining_work": [], "reason": "", "requested_turns": 0},
    }


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.txt").write_text("hello\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.task_data = {
            "task_id": "revision-fixture-001",
            "repo_root": str(self.repo.resolve()),
            "base_commit": self.base,
            "mode": "implement",
            "objective": "Update the greeting",
            "context_paths": ["src/app.txt"],
            "forbidden_context": [],
            "allowed_changed_paths": ["src/**"],
            "acceptance_criteria": ["Greeting updated"],
            "model": "fable",
            "max_turns": 4,
            "timeout_seconds": 30,
            "allow_subagents": False,
            "require_subscription_auth": True,
        }
        self.task_file = root / "task.json"
        self.task_file.write_text(json.dumps(self.task_data), encoding="utf-8")
        self.bridge = ClaudeBridge(root / "artifacts")
        self.artifact = root / "artifacts" / "revision-fixture"
        self.artifact.mkdir(parents=True)
        (self.artifact / "task.json").write_text(json.dumps(self.task_data), encoding="utf-8")
        self.worktree = self.artifact / "worktree"
        git(self.repo, "worktree", "add", "-q", "-b", "delegate/revision-fixture", str(self.worktree), self.base)
        (self.worktree / "src" / "app.txt").write_text("hello, world\n", encoding="utf-8")
        (self.artifact / "stdout.json").write_text(json.dumps({"session_id": SESSION_ID}), encoding="utf-8")
        primary = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
        (self.artifact / "primary-status.before.txt").write_text(primary, encoding="utf-8")
        self.result = {
            "kind": "task",
            "task_id": self.task_data["task_id"],
            "status": "complete",
            "lifecycle_status": "REVIEW_PENDING",
            "starting_commit": self.base,
            "worktree": str(self.worktree),
            "requested_model": "fable",
            "actual_model": "claude-fable-5",
            "session_id": SESSION_ID,
            "process": {"exit_code": 0, "timed_out": False},
            "claude_claim": complete_claim(),
            "changed_paths": ["src/app.txt"],
            "unauthorized_changed_paths": [],
            "validation": {"requested": None, "status": "not_run", "process": None},
            "primary_checkout_unchanged": True,
            "worker_warnings": [],
            "failures": [],
            "error_kind": None,
            "extension_request": None,
            "continuation": {"extensions_used": 0, "total_granted_turns": 4, "can_continue": False},
            "segments": [
                {
                    "index": 0,
                    "label": "initial",
                    "session_id": SESSION_ID,
                    "checkpoint_process": None,
                    "worktree_fingerprint": _worktree_fingerprint(self.worktree, self.base),
                }
            ],
        }
        self.write_result()
        self.feedback = root / "feedback.json"
        self.feedback.write_text(json.dumps(FEEDBACK), encoding="utf-8")
        executable = root / "claude.exe"
        executable.write_bytes(b"")
        self.preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())

    def write_result(self, **updates) -> None:
        (self.artifact / "result.json").write_text(json.dumps({**self.result, **updates}), encoding="utf-8")

    @staticmethod
    def fake_segment(*, session: str = SESSION_ID, edit: bool = True):
        def run_segment(**kwargs):
            label = kwargs["label"]
            if edit:
                (kwargs["worktree"] / "src" / "app.txt").write_text("hello, reviewer\n", encoding="utf-8")
            (kwargs["artifact_dir"] / f"{label}.stdout.json").write_text(
                json.dumps({"session_id": session}), encoding="utf-8"
            )
            return {
                "process": ProcessResult(0, "{}", "", 1.0, False, False, False),
                "envelope": {"session_id": session, "is_error": False},
                "structured": complete_claim(),
                "parse_error": None,
                "turn_cap_reached": False,
                "session_id": session,
                "checkpoint_process": None,
                "checkpoint_envelope": None,
                "checkpoint_error": None,
                "label": label,
                "granted_turns": kwargs["turns"],
            }

        return run_segment

    def revise(self, runner, *, grant: int = 3):
        with (
            patch.object(self.bridge, "usage", side_effect=[usage(), usage()]),
            patch("codex_claude_bridge.launcher.inspect_preflight", return_value=self.preflight),
            patch("codex_claude_bridge.launcher._run_work_segment", side_effect=runner) as mocked,
        ):
            result = self.bridge.revise(self.task_file, self.artifact, self.feedback, grant)
        return result, mocked

    def assert_refused(self, pattern: str, *, grant: int = 3) -> None:
        with (
            patch.object(self.bridge, "usage", return_value=usage()),
            patch("codex_claude_bridge.launcher.inspect_preflight", return_value=self.preflight),
            patch("codex_claude_bridge.launcher._run_work_segment") as runner,
        ):
            with self.assertRaisesRegex((BridgeError, RevisionError), pattern):
                self.bridge.revise(self.task_file, self.artifact, self.feedback, grant)
        runner.assert_not_called()

    def test_successful_revision_resumes_same_session_and_preserves_history(self) -> None:
        original_result = (self.artifact / "result.json").read_bytes()
        original_task = (self.artifact / "task.json").read_bytes()
        result, runner = self.revise(self.fake_segment())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
        self.assertNotEqual(result["lifecycle_status"], "ACCEPTED")
        call = runner.call_args.kwargs
        self.assertEqual(call["resume_session_id"], SESSION_ID)
        self.assertEqual(call["turns"], 3)
        self.assertEqual(call["label"], "revision-1")
        self.assertIn("review_findings", call["prompt"])
        self.assertIn("hello, reviewer", call["prompt"])
        self.assertEqual((self.artifact / "revision-1.prior-result.json").read_bytes(), original_result)
        saved_feedback = json.loads((self.artifact / "revision-1.feedback.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_feedback, FEEDBACK)
        self.assertEqual((self.artifact / "task.json").read_bytes(), original_task)
        self.assertEqual(result["continuation"]["extensions_used"], 0)
        self.assertEqual(result["continuation"]["total_granted_turns"], 7)
        self.assertEqual([segment["label"] for segment in result["segments"]], ["initial", "revision-1"])
        self.assertEqual(result["segments"][1]["kind"], "revision")
        self.assertEqual(result["revisions"][0]["prior_lifecycle_status"], "REVIEW_PENDING")
        self.assertEqual(result["revisions"][0]["outcome_lifecycle_status"], "REVIEW_PENDING")
        self.assertTrue(result["usage_gate"]["allowed"])
        self.assertEqual((self.worktree / "src" / "app.txt").read_text(encoding="utf-8"), "hello, reviewer\n")
        self.assertTrue(result["primary_checkout_unchanged"])
        # Resubmitting identical feedback is refused even though lineage and fingerprint are still valid.
        self.assert_refused("already applied")

    def test_plain_directory_boundary_matches_task_contract_without_prefix_collisions(self) -> None:
        self.assertTrue(path_within_scope("src/file.py", ["src"]))
        self.assertTrue(path_within_scope("src/pkg/file.py", ["src"]))
        self.assertTrue(path_within_scope("src/pkg/file.py", ["src/**"]))
        self.assertTrue(path_within_scope("src/main.py", ["src/main.py"]))
        self.assertFalse(path_within_scope("src-other/file.py", ["src"]))
        self.assertFalse(path_within_scope("src-other/file.py", ["src/**"]))
        self.assertFalse(path_within_scope("srcfile.py", ["src"]))
        accepted = load_feedback(self.feedback, allowed_changed_paths=("src",), worktree=self.worktree)
        self.assertEqual(accepted["findings"][0]["path"], "src/app.txt")
        collision = self.feedback.parent / "collision.json"
        collision.write_text(
            json.dumps({"findings": [{**FEEDBACK["findings"][0], "path": "src-other/app.txt"}]}), encoding="utf-8"
        )
        with self.assertRaisesRegex(RevisionError, "outside"):
            load_feedback(collision, allowed_changed_paths=("src",), worktree=self.worktree)

    def test_task_with_plain_directory_scope_is_revisable(self) -> None:
        scoped = {**self.task_data, "allowed_changed_paths": ["src"]}
        for path in (self.task_file, self.artifact / "task.json"):
            path.write_text(json.dumps(scoped), encoding="utf-8")
        result, _runner = self.revise(self.fake_segment())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
        self.assertEqual(result["revisions"][0]["label"], "revision-1")

    def test_block_caused_only_by_validation_failure_is_revisable(self) -> None:
        self.write_result(
            status="failed",
            lifecycle_status="BLOCKED",
            failures=["independent validation failed"],
            validation={"requested": ["check"], "status": "failed", "process": {"timed_out": False}},
        )
        result, _runner = self.revise(self.fake_segment())
        self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
        self.assertEqual(result["revisions"][0]["target_state"], "validation_failed")
        self.assertEqual(result["revisions"][0]["prior_failures"], ["independent validation failed"])

    def test_wrong_states_are_refused(self) -> None:
        validation_failed = {"requested": ["check"], "status": "failed", "process": {"timed_out": False}}
        cases = {
            "extension_requested": {"status": "extension_requested", "lifecycle_status": "EXTENSION_REQUESTED"},
            "authentication": {
                "status": "failed",
                "lifecycle_status": "BLOCKED",
                "failures": ["Claude reported an error"],
                "error_kind": "authentication",
            },
            "scope": {
                "status": "failed",
                "lifecycle_status": "BLOCKED",
                "failures": ["out-of-scope changed paths detected"],
            },
            "timeout_with_validation": {
                "status": "failed",
                "lifecycle_status": "BLOCKED",
                "failures": ["Claude process timed out", "independent validation failed"],
                "validation": validation_failed,
            },
            "validation_timed_out": {
                "status": "failed",
                "lifecycle_status": "BLOCKED",
                "failures": ["independent validation failed"],
                "validation": {"requested": ["check"], "status": "failed", "process": {"timed_out": True}},
            },
            "checkpoint_failed": {"status": "checkpoint_failed", "lifecycle_status": "CHECKPOINT_FORMAT_FAILED"},
            "usage_blocked": {"status": "usage_blocked", "lifecycle_status": "BLOCKED"},
        }
        for name, updates in cases.items():
            with self.subTest(state=name):
                self.write_result(**updates)
                self.assert_refused("not a revisable result")

    def test_tampered_or_mismatched_session_is_refused(self) -> None:
        self.write_result(session_id=OTHER_SESSION_ID)
        self.assert_refused("session ID does not match")
        self.write_result()
        (self.artifact / "stdout.json").write_text(json.dumps({"session_id": OTHER_SESSION_ID}), encoding="utf-8")
        self.assert_refused("session lineage")

    def test_fingerprint_change_is_refused(self) -> None:
        (self.worktree / "src" / "app.txt").write_text("edited after the result\n", encoding="utf-8")
        self.assert_refused("changed after its recorded result")
        (self.worktree / "src" / "app.txt").write_text("hello, world\n", encoding="utf-8")
        (self.worktree / "src" / "extra.txt").write_text("untracked after result\n", encoding="utf-8")
        self.assert_refused("changed after its recorded result")

    def test_feedback_path_escapes_and_imprecise_feedback_are_refused(self) -> None:
        bad_paths = [
            "../outside.txt",
            "src/../README.md",
            "/etc/passwd",
            "C:/Windows/win.ini",
            "src\\app.txt",
            "README.md",
            "src/.git/config",
            "src/app.txt/",
            " src/app.txt",
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                finding = {**FEEDBACK["findings"][0], "path": path}
                self.feedback.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")
                self.assert_refused("path|outside|escapes|relative")
        malformed = [
            {"findings": []},
            {"findings": [FEEDBACK["findings"][0]], "note": "extra"},
            {"findings": [{**FEEDBACK["findings"][0], "issue": "bad"}]},
            {"findings": [{"path": "src/app.txt", "issue": "Missing the reviewer name"}]},
            {"findings": [FEEDBACK["findings"][0], FEEDBACK["findings"][0]]},
            [FEEDBACK["findings"][0]],
        ]
        for value in malformed:
            with self.subTest(feedback=value):
                self.feedback.write_text(json.dumps(value), encoding="utf-8")
                self.assert_refused("feedback|finding")

    def test_grant_must_be_bounded(self) -> None:
        for grant in (0, 13, True):
            with self.subTest(grant=grant):
                self.assert_refused("1 through 12", grant=grant)

    def test_exhausted_or_unknown_usage_pauses_revision_without_launch(self) -> None:
        for name, usage_patch, expected in (
            ("exhausted", {"return_value": usage(0.0)}, "five_hour_exhausted"),
            ("unavailable", {"side_effect": UsageError("offline")}, "usage_unavailable"),
        ):
            with self.subTest(case=name):
                with (
                    patch.object(self.bridge, "usage", **usage_patch),
                    patch("codex_claude_bridge.launcher._run_work_segment") as runner,
                ):
                    result = self.bridge.revise(self.task_file, self.artifact, self.feedback, 3)
                runner.assert_not_called()
                self.assertEqual(result["last_revision_decision"]["decision"], "paused")
                self.assertEqual(result["last_revision_decision"]["reason"], expected)
                self.assertEqual(result["status"], "complete")
                self.assertFalse((self.artifact / "revision-1.prior-result.json").exists())

    def test_revision_without_measurable_change_is_blocked(self) -> None:
        result, _runner = self.revise(self.fake_segment(edit=False))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("revision made no measurable change to the worktree", result["failures"])

    def test_revision_returning_different_session_is_blocked(self) -> None:
        result, _runner = self.revise(self.fake_segment(session=OTHER_SESSION_ID))
        self.assertEqual(result["lifecycle_status"], "BLOCKED")
        self.assertIn("Claude revision returned a different session ID", result["failures"])

    def test_revision_extension_request_is_continued_through_existing_mechanism(self) -> None:
        request = {
            "completed_work": ["Fixed the greeting"],
            "remaining_work": ["Update the matching note"],
            "reason": "A second file still needs the correction",
            "requested_turns": 2,
        }

        def extension_segment(**kwargs):
            outcome = self.fake_segment()(**kwargs)
            outcome["structured"] = {"status": "extension_requested", "extension_request": request}
            return outcome

        result, _runner = self.revise(extension_segment)
        self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED")
        self.assertEqual(result["continuation"]["extensions_used"], 0)

        def continuation_segment(**kwargs):
            (kwargs["worktree"] / "src" / "note.txt").write_text("reviewer\n", encoding="utf-8")
            return self.fake_segment(edit=False)(**kwargs)

        with (
            patch.object(self.bridge, "usage", side_effect=[usage(), usage()]),
            patch("codex_claude_bridge.launcher.inspect_preflight", return_value=self.preflight),
            patch("codex_claude_bridge.launcher._run_work_segment", side_effect=continuation_segment) as runner,
        ):
            continued = self.bridge.continue_task(self.task_file, self.artifact, 2)
        self.assertEqual(runner.call_args.kwargs["label"], "segment-2")
        self.assertEqual(continued["lifecycle_status"], "REVIEW_PENDING")
        self.assertEqual(continued["continuation"]["extensions_used"], 1)
        self.assertEqual(len(continued["revisions"]), 1)


if __name__ == "__main__":
    unittest.main()
