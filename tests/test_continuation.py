from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_claude_bridge.auth import Preflight
from codex_claude_bridge.launcher import BridgeError, ClaudeBridge
from codex_claude_bridge.process import ProcessResult
from codex_claude_bridge.usage import UsageError


SESSION_ID = "00000000-0000-4000-8000-000000000001"


def healthy_usage() -> dict:
    return {
        "status": "available",
        "five_hour": {"used_percent": 40.0, "remaining_percent": 60.0, "resets_at": "later"},
        "weekly": {"used_percent": 20.0, "remaining_percent": 80.0, "resets_at": "later"},
        "weekly_scoped": [
            {
                "model": "Fable",
                "model_id": "claude-fable-5",
                "used_percent": 30.0,
                "remaining_percent": 70.0,
                "resets_at": "later",
            }
        ],
        "source": "test",
    }


class ContinuationTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[ClaudeBridge, Path, Path, Path, Preflight]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        task_data = {
            "task_id": "continuation-fixture-001",
            "repo_root": str(repo.resolve()),
            "base_commit": base,
            "mode": "analyze",
            "objective": "Review the fixture",
            "context_paths": ["README.md"],
            "forbidden_context": ["Unrelated paths"],
            "allowed_changed_paths": [],
            "acceptance_criteria": ["Return findings"],
            "model": "fable",
            "max_turns": 2,
            "max_total_turns": 8,
            "max_extensions": 2,
            "timeout_seconds": 30,
            "allow_subagents": False,
            "require_subscription_auth": True,
        }
        task_file = root / "task.json"
        task_file.write_text(json.dumps(task_data), encoding="utf-8")
        artifact_root = root / "artifacts"
        bridge = ClaudeBridge(artifact_root)
        artifact = artifact_root / "continuation-fixture"
        artifact.mkdir()
        (artifact / "task.json").write_text(json.dumps(task_data), encoding="utf-8")
        (artifact / "stdout.json").write_text(json.dumps({"session_id": SESSION_ID}), encoding="utf-8")
        (artifact / "primary-status.before.txt").write_text("", encoding="utf-8")
        request = {
            "completed_work": ["Reviewed the public API"],
            "remaining_work": ["Review error handling"],
            "reason": "The remaining acceptance criterion needs evidence",
            "requested_turns": 3,
        }
        result = {
            "task_id": task_data["task_id"],
            "status": "extension_requested",
            "lifecycle_status": "EXTENSION_REQUESTED",
            "starting_commit": base,
            "worktree": str(repo.resolve()),
            "requested_model": "fable",
            "actual_model": "claude-fable-5",
            "session_id": SESSION_ID,
            "extension_request": request,
            "continuation": {
                "extensions_used": 0,
                "max_extensions": 2,
                "total_granted_turns": 2,
                "max_total_turns": 8,
                "can_continue": True,
            },
            "segments": [
                {
                    "index": 0,
                    "label": "initial",
                    "session_id": SESSION_ID,
                    "checkpoint_process": None,
                }
            ],
            "worker_warnings": [],
        }
        (artifact / "result.json").write_text(json.dumps(result), encoding="utf-8")
        executable = root / "claude.exe"
        executable.write_bytes(b"")
        preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())
        return bridge, task_file, artifact, repo, preflight

    def test_healthy_usage_allows_same_session_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            process = ProcessResult(0, "{}", "", 1.0, False, False, False)
            outcome = {
                "process": process,
                "envelope": {"session_id": SESSION_ID, "is_error": False},
                "structured": {
                    "status": "complete",
                    "extension_request": {
                        "completed_work": [],
                        "remaining_work": [],
                        "reason": "",
                        "requested_turns": 0,
                    },
                },
                "parse_error": None,
                "turn_cap_reached": False,
                "session_id": SESSION_ID,
                "checkpoint_process": None,
                "checkpoint_envelope": None,
                "checkpoint_error": None,
                "label": "segment-2",
                "granted_turns": 3,
            }
            (artifact / "segment-2.stdout.json").write_text(
                json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
            )
            with (
                patch.object(bridge, "usage", side_effect=[healthy_usage(), healthy_usage()]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", return_value=outcome) as runner,
            ):
                result = bridge.continue_task(task_file, artifact, 3)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(runner.call_args.kwargs["resume_session_id"], SESSION_ID)
            self.assertEqual(result["continuation"]["total_granted_turns"], 5)

    def test_grant_cannot_exceed_claudes_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
            with self.assertRaisesRegex(BridgeError, "cannot exceed"):
                bridge.continue_task(task_file, artifact, 4)

    def test_legacy_cap_exhausted_artifact_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            result_path = artifact / "result.json"
            prior = json.loads(result_path.read_text(encoding="utf-8"))
            prior["status"] = "cap_exhausted"
            prior["lifecycle_status"] = "CAP_EXHAUSTED"
            prior["continuation"]["can_continue"] = False
            result_path.write_text(json.dumps(prior), encoding="utf-8")
            process = ProcessResult(0, "{}", "", 1.0, False, False, False)
            outcome = {
                "process": process,
                "envelope": {"session_id": SESSION_ID, "is_error": False},
                "structured": {
                    "status": "complete",
                    "extension_request": {
                        "completed_work": [],
                        "remaining_work": [],
                        "reason": "",
                        "requested_turns": 0,
                    },
                },
                "parse_error": None,
                "turn_cap_reached": False,
                "session_id": SESSION_ID,
                "checkpoint_process": None,
                "checkpoint_envelope": None,
                "checkpoint_error": None,
                "label": "segment-2",
                "granted_turns": 2,
            }
            (artifact / "segment-2.stdout.json").write_text(
                json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
            )
            with (
                patch.object(bridge, "usage", side_effect=[healthy_usage(), healthy_usage()]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", return_value=outcome),
            ):
                resumed = bridge.continue_task(task_file, artifact, 2)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["continuation"]["policy"], "until_complete_or_usage_pause")

    def test_continuation_ignores_legacy_sixty_turn_and_four_extension_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            for path in (task_file, artifact / "task.json"):
                task = json.loads(path.read_text(encoding="utf-8"))
                task["max_total_turns"] = 60
                task["max_extensions"] = 4
                path.write_text(json.dumps(task), encoding="utf-8")
            result_path = artifact / "result.json"
            prior = json.loads(result_path.read_text(encoding="utf-8"))
            prior["continuation"]["extensions_used"] = 4
            prior["continuation"]["total_granted_turns"] = 60
            prior["continuation"]["max_extensions"] = 4
            prior["continuation"]["max_total_turns"] = 60
            result_path.write_text(json.dumps(prior), encoding="utf-8")
            process = ProcessResult(0, "{}", "", 1.0, False, False, False)

            def run_segment(**kwargs):
                (kwargs["artifact_dir"] / "segment-6.stdout.json").write_text(
                    json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
                )
                return {
                    "process": process,
                    "envelope": {"session_id": SESSION_ID, "is_error": False},
                    "structured": {
                        "status": "complete",
                        "extension_request": {
                            "completed_work": [],
                            "remaining_work": [],
                            "reason": "",
                            "requested_turns": 0,
                        },
                    },
                    "parse_error": None,
                    "turn_cap_reached": False,
                    "session_id": SESSION_ID,
                    "checkpoint_process": None,
                    "checkpoint_envelope": None,
                    "checkpoint_error": None,
                    "label": "segment-6",
                    "granted_turns": 2,
                }

            with (
                patch.object(bridge, "usage", side_effect=[healthy_usage(), healthy_usage()]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment),
            ):
                resumed = bridge.continue_task(task_file, artifact, 2)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["continuation"]["extensions_used"], 5)
            self.assertEqual(resumed["continuation"]["total_granted_turns"], 62)

    def test_low_usage_pauses_without_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
            low = healthy_usage()
            low["five_hour"]["remaining_percent"] = 0.0
            with (
                patch.object(bridge, "usage", return_value=low),
                patch("codex_claude_bridge.launcher._run_work_segment") as runner,
            ):
                result = bridge.continue_task(task_file, artifact, 2)
            runner.assert_not_called()
            self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED")
            self.assertEqual(result["continuation"]["last_decision"]["decision"], "paused")
            self.assertEqual(result["continuation"]["last_decision"]["reason"], "five_hour_exhausted")
            self.assertEqual(result["usage_gate"]["reason"], "five_hour_exhausted")

    def test_legacy_override_never_bypasses_exhaustion_or_unknown_usage(self) -> None:
        exhausted = healthy_usage()
        exhausted["five_hour"]["remaining_percent"] = 0.0
        for name, usage_patch, expected in (
            ("exhausted", {"return_value": exhausted}, "five_hour_exhausted"),
            ("unavailable", {"side_effect": UsageError("offline")}, "usage_unavailable"),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
                with (
                    patch.object(bridge, "usage", **usage_patch),
                    patch("codex_claude_bridge.launcher._run_work_segment") as runner,
                ):
                    result = bridge.continue_task(task_file, artifact, 2, override_five_hour_soft_limit=True)
                runner.assert_not_called()
                decision = result["continuation"]["last_decision"]
                self.assertEqual(decision["decision"], "paused")
                self.assertEqual(decision["reason"], expected)
                self.assertTrue(decision["legacy_override_requested"])
                self.assertFalse(decision["five_hour_soft_limit_override"])

    def test_low_but_nonzero_usage_continues_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            low = healthy_usage()
            low["five_hour"]["remaining_percent"] = 3.0
            process = ProcessResult(0, "{}", "", 1.0, False, False, False)
            outcome = {
                "process": process,
                "envelope": {"session_id": SESSION_ID, "is_error": False},
                "structured": {
                    "status": "complete",
                    "extension_request": {
                        "completed_work": [],
                        "remaining_work": [],
                        "reason": "",
                        "requested_turns": 0,
                    },
                },
                "parse_error": None,
                "turn_cap_reached": False,
                "session_id": SESSION_ID,
                "checkpoint_process": None,
                "checkpoint_envelope": None,
                "checkpoint_error": None,
                "label": "segment-2",
                "granted_turns": 2,
            }
            (artifact / "segment-2.stdout.json").write_text(
                json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
            )
            with (
                patch.object(bridge, "usage", side_effect=[low, low]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", return_value=outcome) as runner,
            ):
                result = bridge.continue_task(task_file, artifact, 2)
            runner.assert_called_once()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["continuation"]["last_decision"]["decision"], "granted")
            self.assertFalse(
                result["continuation"]["last_decision"]["five_hour_soft_limit_override"]
            )

    def test_unavailable_usage_is_durably_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
            with patch.object(bridge, "usage", side_effect=UsageError("offline")):
                result = bridge.continue_task(
                    task_file,
                    artifact,
                    2,
                    override_five_hour_soft_limit=True,
                )
            saved = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["continuation"]["last_decision"]["decision"], "paused")
            self.assertEqual(saved["continuation"]["last_decision"]["decision"], "paused")
            self.assertEqual(saved["continuation"]["last_decision"]["reason"], "usage_unavailable")
            self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED")

    def test_tampered_session_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
            result_path = artifact / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["session_id"] = "00000000-0000-4000-8000-000000000002"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(BridgeError, "does not match"):
                bridge.continue_task(task_file, artifact, 2)

    def test_resumed_segment_cannot_change_session_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            changed_session = "00000000-0000-4000-8000-000000000003"
            process = ProcessResult(0, "{}", "", 1.0, False, False, False)

            def run_segment(**kwargs):
                (kwargs["artifact_dir"] / "segment-2.stdout.json").write_text(
                    json.dumps({"session_id": changed_session}), encoding="utf-8"
                )
                return {
                    "process": process,
                    "envelope": {"session_id": changed_session, "is_error": False},
                    "structured": {
                        "status": "complete",
                        "extension_request": {
                            "completed_work": [],
                            "remaining_work": [],
                            "reason": "",
                            "requested_turns": 0,
                        },
                    },
                    "parse_error": None,
                    "turn_cap_reached": False,
                    "session_id": changed_session,
                    "checkpoint_process": None,
                    "checkpoint_envelope": None,
                    "checkpoint_error": None,
                    "label": "segment-2",
                    "granted_turns": 2,
                }

            with (
                patch.object(bridge, "usage", side_effect=[healthy_usage(), healthy_usage()]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment),
            ):
                result = bridge.continue_task(task_file, artifact, 2)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("different session ID" in failure for failure in result["failures"]))

    def test_legacy_task_cap_does_not_stop_a_valid_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, _fixture_artifact, _repo, preflight = self.make_fixture(Path(temp))
            work_process = ProcessResult(1, "{}", "", 1.0, False, False, False)
            checkpoint_process = ProcessResult(0, "{}", "", 0.1, False, False, False)
            complete_process = ProcessResult(0, "{}", "", 1.0, False, False, False)
            request = {
                "completed_work": ["Reviewed the public API"],
                "remaining_work": ["Review error handling"],
                "reason": "The remaining acceptance criterion needs evidence",
                "requested_turns": 3,
            }

            def run_segment(**kwargs):
                artifact_dir = kwargs["artifact_dir"]
                label = kwargs["label"]
                if label == "initial":
                    raw = json.dumps({"session_id": SESSION_ID})
                    (artifact_dir / "stdout.json").write_text(raw, encoding="utf-8")
                    (artifact_dir / "initial.checkpoint.stdout.json").write_text(raw, encoding="utf-8")
                    return {
                        "process": work_process,
                        "envelope": {"session_id": SESSION_ID, "is_error": True},
                        "structured": {"status": "extension_requested", "extension_request": request},
                        "parse_error": "turn cap reached",
                        "turn_cap_reached": True,
                        "session_id": SESSION_ID,
                        "checkpoint_process": checkpoint_process,
                        "checkpoint_envelope": {"session_id": SESSION_ID},
                        "checkpoint_error": None,
                        "label": label,
                        "granted_turns": 2,
                    }
                (artifact_dir / "segment-2.stdout.json").write_text(
                    json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
                )
                return {
                    "process": complete_process,
                    "envelope": {"session_id": SESSION_ID, "is_error": False},
                    "structured": {
                        "status": "complete",
                        "extension_request": {
                            "completed_work": [],
                            "remaining_work": [],
                            "reason": "",
                            "requested_turns": 0,
                        },
                    },
                    "parse_error": None,
                    "turn_cap_reached": False,
                    "session_id": SESSION_ID,
                    "checkpoint_process": None,
                    "checkpoint_envelope": None,
                    "checkpoint_error": None,
                    "label": label,
                    "granted_turns": 2,
                }

            with (
                patch.object(bridge, "usage", return_value=healthy_usage()),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment),
            ):
                initial = bridge.run(task_file)
            self.assertEqual(initial["status"], "extension_requested")
            self.assertEqual(initial["continuation"]["total_granted_turns"], 4)
            artifact = Path(initial["artifact_directory"])
            with (
                patch.object(bridge, "usage", side_effect=[healthy_usage(), healthy_usage()]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment),
            ):
                completed = bridge.continue_task(task_file, artifact, 3)
            self.assertEqual(completed["status"], "complete")
            self.assertEqual(completed["continuation"]["total_granted_turns"], 7)

    def test_checkpoint_format_failure_is_recoverable_not_generic_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, _fixture_artifact, _repo, preflight = self.make_fixture(Path(temp))
            work_process = ProcessResult(1, "{}", "", 1.0, False, False, False)
            checkpoint_process = ProcessResult(1, "{}", "", 0.1, False, False, False)

            def run_segment(**kwargs):
                artifact_dir = kwargs["artifact_dir"]
                raw = json.dumps({"session_id": SESSION_ID, "subtype": "error_max_turns"})
                (artifact_dir / "stdout.json").write_text(raw, encoding="utf-8")
                (artifact_dir / "initial.checkpoint.stdout.json").write_text(raw, encoding="utf-8")
                return {
                    "process": work_process,
                    "envelope": {"session_id": SESSION_ID, "is_error": True},
                    "structured": None,
                    "parse_error": "turn cap reached",
                    "turn_cap_reached": True,
                    "session_id": SESSION_ID,
                    "checkpoint_process": checkpoint_process,
                    "checkpoint_envelope": {
                        "session_id": SESSION_ID,
                        "subtype": "error_max_turns",
                    },
                    "checkpoint_error": None,
                    "label": "initial",
                    "granted_turns": 2,
                }

            with (
                patch.object(bridge, "usage", return_value=healthy_usage()),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment),
            ):
                result = bridge.run(task_file)
            self.assertEqual(result["status"], "checkpoint_failed")
            self.assertEqual(result["lifecycle_status"], "CHECKPOINT_FORMAT_FAILED")
            self.assertEqual(result["error_kind"], "checkpoint_format_error")
            self.assertTrue(result["continuation"]["can_repair_checkpoint"])
            self.assertEqual(result["continuation"]["total_granted_turns"], 4)

    def test_checkpoint_repair_resumes_only_control_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, preflight = self.make_fixture(Path(temp))
            result_path = artifact / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result.update(
                {
                    "status": "failed",
                    "lifecycle_status": "BLOCKED",
                    "claude_claim": None,
                    "process": {"timed_out": False},
                    "failures": [
                        "Claude process exited with 1",
                        "Claude reported an error",
                        "missing structured_output",
                    ],
                    "primary_checkout_unchanged": True,
                }
            )
            result["continuation"]["total_granted_turns"] = 4
            result["continuation"]["can_continue"] = False
            result["continuation"]["can_repair_checkpoint"] = True
            result["segments"][0]["checkpoint_process"] = {"exit_code": 1}
            result["segments"][0]["checkpoint_turns"] = 2
            result["segments"][0]["turn_cap_reached"] = True
            (artifact / "initial.checkpoint.stdout.json").write_text(
                json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
            )
            result_path.write_text(json.dumps(result), encoding="utf-8")
            process = ProcessResult(0, "{}", "", 0.1, False, False, False)

            def repair_checkpoint(**kwargs):
                stdout_file = "initial.checkpoint-repair-1.stdout.json"
                (kwargs["artifact_dir"] / stdout_file).write_text(
                    json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
                )
                return {
                    "process": process,
                    "envelope": {"session_id": SESSION_ID, "subtype": "success"},
                    "structured": {
                        "status": "complete",
                        "summary": "Review complete",
                        "extension_request": {
                            "completed_work": [],
                            "remaining_work": [],
                            "reason": "",
                            "requested_turns": 0,
                        },
                    },
                    "error": None,
                    "stdout_file": stdout_file,
                    "stderr_file": "initial.checkpoint-repair-1.stderr.log",
                }

            low = healthy_usage()
            low["five_hour"]["remaining_percent"] = 3.0
            with (
                patch.object(bridge, "usage", side_effect=[low, low]),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_checkpoint", side_effect=repair_checkpoint) as runner,
            ):
                repaired = bridge.repair_checkpoint(
                    task_file,
                    artifact,
                    override_five_hour_soft_limit=True,
                )
            self.assertEqual(repaired["status"], "complete")
            self.assertEqual(repaired["lifecycle_status"], "REVIEW_PENDING")
            self.assertEqual(repaired["continuation"]["total_granted_turns"], 6)
            self.assertEqual(runner.call_args.kwargs["session_id"], SESSION_ID)
            self.assertFalse(repaired["checkpoint_recovery"]["five_hour_soft_limit_override"])
            self.assertTrue(repaired["checkpoint_recovery"]["legacy_override_requested"])

    def test_checkpoint_repair_override_cannot_bypass_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, artifact, _repo, _preflight = self.make_fixture(Path(temp))
            result_path = artifact / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result.update(
                {
                    "status": "checkpoint_failed",
                    "lifecycle_status": "CHECKPOINT_FORMAT_FAILED",
                    "failures": ["checkpoint structured output failed after two bounded formatting attempts"],
                    "unauthorized_changed_paths": [],
                    "primary_checkout_unchanged": True,
                }
            )
            result["segments"][0]["checkpoint_process"] = {"exit_code": 1}
            (artifact / "initial.checkpoint.stdout.json").write_text(
                json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
            )
            result_path.write_text(json.dumps(result), encoding="utf-8")
            exhausted = healthy_usage()
            exhausted["five_hour"]["remaining_percent"] = 0.0
            with (
                patch.object(bridge, "usage", return_value=exhausted),
                patch("codex_claude_bridge.launcher._run_checkpoint") as runner,
            ):
                paused = bridge.repair_checkpoint(task_file, artifact, override_five_hour_soft_limit=True)
            runner.assert_not_called()
            self.assertEqual(paused["checkpoint_recovery"]["decision"], "paused")
            self.assertEqual(paused["checkpoint_recovery"]["reason"], "five_hour_exhausted")
            self.assertEqual(paused["status"], "checkpoint_failed")

    def test_initial_run_is_gated_before_provider_launch(self) -> None:
        exhausted = healthy_usage()
        exhausted["five_hour"]["remaining_percent"] = 0.0
        malformed = healthy_usage()
        malformed["five_hour"]["remaining_percent"] = float("nan")
        for name, usage_patch, expected in (
            ("exhausted", {"return_value": exhausted}, "five_hour_exhausted"),
            ("unavailable", {"side_effect": UsageError("offline")}, "usage_unavailable"),
            ("malformed", {"return_value": malformed}, "usage_unavailable"),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                bridge, task_file, _artifact, _repo, _preflight = self.make_fixture(Path(temp))
                with (
                    patch.object(bridge, "usage", **usage_patch),
                    patch("codex_claude_bridge.launcher.inspect_preflight") as preflight,
                    patch("codex_claude_bridge.launcher._run_work_segment") as runner,
                ):
                    result = bridge.run(task_file)
                runner.assert_not_called()
                preflight.assert_not_called()
                self.assertEqual(result["status"], "usage_blocked")
                self.assertEqual(result["lifecycle_status"], "BLOCKED")
                self.assertEqual(result["error_kind"], expected)
                self.assertEqual(result["usage_gate"]["reason"], expected)
                self.assertFalse(result["provider_launched"])
                saved = json.loads(
                    (Path(result["artifact_directory"]) / "result.json").read_text(encoding="utf-8")
                )
                self.assertEqual(saved["status"], "usage_blocked")

    def test_initial_run_records_allowing_usage_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bridge, task_file, _artifact, _repo, preflight = self.make_fixture(Path(temp))
            low = healthy_usage()
            low["five_hour"]["remaining_percent"] = 1.0

            def run_segment(**kwargs):
                (kwargs["artifact_dir"] / "stdout.json").write_text(
                    json.dumps({"session_id": SESSION_ID}), encoding="utf-8"
                )
                return {
                    "process": ProcessResult(0, "{}", "", 1.0, False, False, False),
                    "envelope": {"session_id": SESSION_ID, "is_error": False},
                    "structured": {
                        "status": "complete",
                        "extension_request": {
                            "completed_work": [],
                            "remaining_work": [],
                            "reason": "",
                            "requested_turns": 0,
                        },
                    },
                    "parse_error": None,
                    "turn_cap_reached": False,
                    "session_id": SESSION_ID,
                    "checkpoint_process": None,
                    "checkpoint_envelope": None,
                    "checkpoint_error": None,
                    "label": "initial",
                    "granted_turns": 2,
                }

            with (
                patch.object(bridge, "usage", return_value=low),
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher._run_work_segment", side_effect=run_segment) as runner,
            ):
                result = bridge.run(task_file)
            runner.assert_called_once()
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["usage_gate"]["allowed"])
            self.assertEqual(result["usage_gate"]["usage"]["five_hour"]["remaining_percent"], 1.0)


if __name__ == "__main__":
    unittest.main()
