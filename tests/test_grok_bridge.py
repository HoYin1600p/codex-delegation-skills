from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "delegate-work" / "scripts" / "grok_bridge.py"
SPEC = importlib.util.spec_from_file_location("grok_bridge", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
SESSION = "00000000-0000-4000-8000-000000000004"


def claim(status: str = "complete") -> dict:
    return {
        "status": status,
        "summary": "Read the requested context.",
        "findings": ["The bridge can return a structured result."],
        "files_read": ["README.md"],
        "files_changed": [],
        "checks": [{"description": "Scope", "reported_outcome": "passed", "evidence": "No edits"}],
        "blockers": [],
        "extension_request": {"completed_work": [], "remaining_work": [], "reason": "", "requested_turns": 0},
    }


def git(root: Path, *args: str) -> str:
    process = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return process.stdout.strip()


class GrokBridgeTests(unittest.TestCase):
    def test_parses_grok_json_text_and_metrics(self) -> None:
        payload = {
            "text": json.dumps(claim()),
            "sessionId": "00000000-0000-4000-8000-000000000004",
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            "total_cost_usd": 0.001,
        }
        envelope, parsed, error = bridge._envelope(json.dumps(payload))
        self.assertIsNone(error)
        self.assertEqual(parsed["status"], "complete")
        self.assertEqual(bridge._session_id(envelope), payload["sessionId"])
        self.assertEqual(bridge._metrics(envelope)["total_tokens"], 15)

    def test_report_turn_usage_is_included_in_segment_total(self) -> None:
        work = {"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                "total_cost_usd": 0.001, "modelUsage": {"grok-4.7": {"modelCalls": 2}}}
        report = {"usage": bridge._metrics({"usage": {"input_tokens": 4, "output_tokens": 1,
                                                       "total_tokens": 5}, "total_cost_usd": 0.0005,
                                             "modelUsage": {"grok-4.7": {"modelCalls": 1}}})}
        totals = bridge._segment_metrics(work, report)
        self.assertEqual(totals["total_tokens"], 17)
        self.assertEqual(totals["model_usage"]["grok-4.7"]["modelCalls"], 3)
        self.assertAlmostEqual(totals["estimated_api_equivalent_usd"], 0.0015)

    def test_max_turn_exit_gets_one_no_tools_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            session = "00000000-0000-4000-8000-000000000004"
            work = bridge.ProcessResult(1, json.dumps({
                "text": "I read the file and need to report back.", "stopReason": "cancelled",
                "sessionId": session, "num_turns": 3,
            }), "Error: max turns reached\n", 1.0, False, False, False)
            report = bridge.ProcessResult(0, json.dumps({
                "structured_output": claim(), "sessionId": session,
            }), "", 0.1, False, False, False)
            context = {"status": "verified", "observed_context_paths": ["README.md"],
                       "missing_context_paths": []}
            with patch.object(bridge, "run_process", side_effect=[work, report]) as runner, \
                 patch.object(bridge, "_export_session", return_value=context):
                segment = bridge._run_segment(task, root, artifact, Path("grok"), "Read README.md", 3, "initial")
            self.assertEqual(runner.call_count, 2)
            report_args = runner.call_args_list[1].args[1]
            self.assertEqual(report_args[report_args.index("--resume") + 1], session)
            self.assertEqual(report_args[report_args.index("--max-turns") + 1], "1")
            self.assertIn("search_replace", report_args[report_args.index("--disallowed-tools") + 1])
            self.assertTrue(segment["hit_cap"])
            self.assertEqual(segment["claim"]["status"], "complete")
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, root, segment, before)
            self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
            self.assertEqual(result["failures"], [])

    def test_max_turn_checkpoint_can_request_reviewed_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            session = "00000000-0000-4000-8000-000000000004"
            work = bridge.ProcessResult(1, json.dumps({"text": "More work remains", "stopReason": "cancelled",
                                                         "sessionId": session}),
                                        "Error: max turns reached", 1.0, False, False, False)
            extension = claim("extension_requested")
            extension["extension_request"] = {"completed_work": ["Read context"],
                                               "remaining_work": ["Finish analysis"],
                                               "reason": "Turn limit", "requested_turns": 2}
            report = bridge.ProcessResult(0, json.dumps({"structured_output": extension,
                                                          "sessionId": session}),
                                          "", 0.1, False, False, False)
            with patch.object(bridge, "run_process", side_effect=[work, report]), \
                 patch.object(bridge, "_export_session", return_value={"status": "verified"}):
                segment = bridge._run_segment(task, root, artifact, Path("grok"), "Read README.md", 3, "initial")
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, root, segment, before)
            self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED")
            self.assertEqual(result["extension_request"]["requested_turns"], 2)

    def test_failed_checkpoint_and_other_nonzero_exits_do_not_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            session = "00000000-0000-4000-8000-000000000004"
            context = {"status": "verified", "observed_context_paths": ["README.md"],
                       "missing_context_paths": []}
            for stderr, stopped, checkpoint_expected in (
                ("Error: max turns reached", False, True),
                ("Error: server failed", False, False),
                ("Error: max turns reached", True, False),
            ):
                with self.subTest(stderr=stderr, timed_out=stopped):
                    artifact = Path(temp) / f"artifact-{int(checkpoint_expected)}-{int(stopped)}-{len(stderr)}"
                    artifact.mkdir()
                    work = bridge.ProcessResult(1, json.dumps({"text": "Unstructured", "stopReason": "cancelled",
                                                                 "sessionId": session}),
                                                stderr, 1.0, stopped, False, False)
                    bad_report = bridge.ProcessResult(1, "not JSON", "report failed", 0.1,
                                                      False, False, False)
                    with patch.object(bridge, "run_process", side_effect=[work, bad_report]) as runner, \
                         patch.object(bridge, "_export_session", return_value=context):
                        segment = bridge._run_segment(task, root, artifact, Path("grok"), "Read README.md", 3, "initial")
                    self.assertEqual(runner.call_count, 2 if checkpoint_expected else 1)
                    before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
                    result = bridge._finish(task, artifact, root, segment, before)
                    self.assertNotEqual(result["status"], "complete")
                    if checkpoint_expected:
                        self.assertEqual(result["lifecycle_status"], "CHECKPOINT_FORMAT_FAILED")

    def test_checkpoint_must_preserve_session_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "implement", ("allowed.txt",), base)
            session = "00000000-0000-4000-8000-000000000004"
            context = {"status": "verified", "observed_context_paths": ["README.md"],
                       "missing_context_paths": []}
            for mutation in (False, True):
                with self.subTest(mutation=mutation):
                    artifact = Path(temp) / f"artifact-{mutation}"
                    artifact.mkdir()
                    worktree = artifact / "worktree"
                    git(root, "worktree", "add", "-b", f"codex/grok-checkpoint-{mutation}", str(worktree), base)
                    work = bridge.ProcessResult(1, json.dumps({"text": "Work done", "stopReason": "cancelled",
                                                                 "sessionId": session}),
                                                "Error: max turns reached", 1.0, False, False, False)
                    report = bridge.ProcessResult(0, json.dumps({"structured_output": claim(),
                                                                   "sessionId": session if mutation else
                                                                   "00000000-0000-4000-8000-000000000005"}),
                                                  "", 0.1, False, False, False)

                    def simulate(_executable, _args, **_kwargs):
                        if simulate.calls == 0:
                            simulate.calls += 1
                            return work
                        if mutation:
                            (worktree / "allowed.txt").write_text("changed by checkpoint\n", encoding="utf-8")
                        return report

                    simulate.calls = 0
                    with patch.object(bridge, "run_process", side_effect=simulate), \
                         patch.object(bridge, "_export_session", return_value=context):
                        segment = bridge._run_segment(task, worktree, artifact, Path("grok"), "Implement", 3, "initial")
                    self.assertIsNone(segment["claim"])
                    self.assertIn("checkpoint format failed", segment["error"])
                    if mutation:
                        self.assertIn("changed the worktree", segment["error"])
                    else:
                        self.assertIn("different session", segment["error"])

    def test_saved_max_turn_failure_can_recover_without_repeating_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, artifact, _worktree, artifact_root = self._failed_cap_artifact(Path(temp))
            session = "00000000-0000-4000-8000-000000000004"
            extension = claim("extension_requested")
            extension["extension_request"] = {"completed_work": ["Created allowed.txt"],
                                               "remaining_work": ["Review the edit"],
                                               "reason": "Reached turn cap", "requested_turns": 2}
            report = bridge.ProcessResult(0, json.dumps({"structured_output": extension,
                                                          "sessionId": session}),
                                          "", 0.1, False, False, False)
            context = {"status": "verified", "observed_context_paths": ["README.md"],
                       "missing_context_paths": []}
            with patch.object(bridge, "ARTIFACT_ROOT", artifact_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight", return_value={"executable": "grok"}), \
                 patch.object(bridge, "run_process", return_value=report) as runner, \
                 patch.object(bridge, "_export_session", return_value=context):
                result = bridge.recover_turn_cap(task_file, artifact)
            self.assertEqual(runner.call_count, 1)
            args = runner.call_args.args[1]
            self.assertEqual(args[args.index("--reasoning-effort") + 1], "high")
            self.assertEqual(args[args.index("--max-turns") + 1], "1")
            self.assertEqual(result["lifecycle_status"], "EXTENSION_REQUESTED")
            self.assertEqual(result["requested_reasoning_effort"], "high")
            self.assertTrue((artifact / "result.before-turn-cap-recovery.json").is_file())
            self.assertTrue((artifact / "initial.session-export.before-turn-cap-recovery.md").is_file())

    def test_saved_max_turn_recovery_rejects_changed_worktree_without_grok_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, artifact, worktree, artifact_root = self._failed_cap_artifact(Path(temp))
            (worktree / "allowed.txt").write_text("changed since failure\n", encoding="utf-8")
            with patch.object(bridge, "ARTIFACT_ROOT", artifact_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight") as preflight, \
                 patch.object(bridge, "run_process") as runner:
                with self.assertRaisesRegex(bridge.GrokBridgeError, "worktree moved or changed"):
                    bridge.recover_turn_cap(task_file, artifact)
            preflight.assert_not_called()
            runner.assert_not_called()

    def test_rejects_incomplete_structured_reply(self) -> None:
        _, parsed, error = bridge._envelope(json.dumps({"text": '{"status":"complete"}'}))
        self.assertIsNone(parsed)
        self.assertIn("contract", error)

    def test_rejects_empty_success_without_work_evidence(self) -> None:
        empty = claim()
        empty.update(summary="", findings=[], files_read=[], files_changed=[], checks=[])
        _, parsed, error = bridge._envelope(json.dumps({"text": json.dumps(empty)}))
        self.assertIsNone(parsed)
        self.assertIn("empty completion", error)

    def test_exact_path_scope_and_directory_scope(self) -> None:
        self.assertTrue(bridge._allowed_path("src/main.py", ("src/main.py",)))
        self.assertFalse(bridge._allowed_path("src/main.py/extra", ("src/main.py",)))
        self.assertTrue(bridge._allowed_path("src/pkg/new.py", ("src/**",)))
        self.assertFalse(bridge._allowed_path("other/new.py", ("src/**",)))

    def test_absolute_claimed_path_is_compared_relative_to_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            normalized, outside = bridge._normalized_claim_paths(
                root, [str(root / "hello_world.py"), "src/main.py", str(Path(temp) / "outside.py")]
            )
            self.assertEqual(normalized, {"hello_world.py", "src/main.py"})
            self.assertEqual(outside, [str(Path(temp) / "outside.py")])

    def test_cli_args_are_bounded_and_block_external_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = self._task(root, "analyze", ())
            args = bridge._args(task, root, root / "assignment.txt", 3, bridge.REPLY_SCHEMA)
            self.assertIn("--no-subagents", args)
            self.assertIn("--disable-web-search", args)
            self.assertIn("dontAsk", args)
            self.assertIn("workspace", args)
            self.assertEqual(args[args.index("--tools") + 1], "read_file,list_dir,grep")
            self.assertEqual(args[args.index("--agent") + 1], "general-purpose")
            self.assertIn("run_terminal_command", args[args.index("--disallowed-tools") + 1])
            self.assertEqual(args[args.index("--reasoning-effort") + 1], "low")
            write_task = self._task(root, "implement", ("hello_world.py",))
            write_args = bridge._args(write_task, root, root / "assignment.txt", 3, bridge.REPLY_SCHEMA)
            self.assertEqual(write_args[write_args.index("--tools") + 1],
                             "read_file,list_dir,grep,search_replace")
            self.assertNotIn("search_replace", write_args[write_args.index("--disallowed-tools") + 1])
            work_args = bridge._args(write_task, root, root / "assignment.txt", 3, None)
            self.assertNotIn("--json-schema", work_args)
            report_args = bridge._args(write_task, root, root / "assignment.txt", 1,
                                       bridge.REPLY_SCHEMA, no_tools=True)
            self.assertIn("search_replace", report_args[report_args.index("--disallowed-tools") + 1])
            default_task = bridge.Task(**{**task.__dict__, "model": None})
            default_args = bridge._args(default_task, root, root / "assignment.txt", 3, None)
            self.assertEqual(default_args[default_args.index("--model") + 1], "grok-4.7")

    def test_context_read_requires_exported_tool_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            fabricated = '## User\nRead README.md\n\n## Assistant\nI read README.md.\n'
            self.assertEqual(bridge._observed_context_reads(fabricated, task), ([], ["README.md"]))
            observed = '## User\nRead README.md\n\n## Tools\n\n- Read: README.md\n\n## Assistant\nDone.\n'
            self.assertEqual(bridge._observed_context_reads(observed, task), (["README.md"], []))
            (root / "docs").mkdir()
            directory_task = bridge.Task(**{**task.__dict__, "context_paths": ("docs",)})
            listing = '## Tools\n\n- ListDir: docs\n'
            self.assertEqual(bridge._observed_context_reads(listing, directory_task), (["docs"], []))

    def test_read_only_result_preserves_primary_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            process = bridge.ProcessResult(0, "", "", 0.1, False, False, False)
            segment = {
                "process": process, "envelope": {"sessionId": "00000000-0000-4000-8000-000000000004"},
                "claim": claim(), "error": None,
                "session_id": "00000000-0000-4000-8000-000000000004",
                "hit_cap": False, "checkpoint": None, "label": "initial", "granted_turns": 2,
                "context_evidence": {"status": "verified", "observed_context_paths": ["README.md"], "missing_context_paths": []},
            }
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, root, segment, before)
            self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
            self.assertEqual(result["changed_paths"], [])
            self.assertTrue(result["primary_checkout_unchanged"])

    def test_completed_answer_without_observed_read_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "analyze", (), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            process = bridge.ProcessResult(0, "", "", 0.1, False, False, False)
            segment = {
                "process": process, "envelope": {"sessionId": "00000000-0000-4000-8000-000000000004"},
                "claim": claim(), "error": None,
                "session_id": "00000000-0000-4000-8000-000000000004",
                "hit_cap": False, "checkpoint": None, "label": "initial", "granted_turns": 2,
                "context_evidence": {"status": "missing_reads", "observed_context_paths": [], "missing_context_paths": ["README.md"]},
            }
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, root, segment, before)
            self.assertEqual(result["lifecycle_status"], "BLOCKED")
            self.assertIn("Grok did not demonstrably open every named context path", result["failures"])

    def test_worktree_scope_violation_blocks_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "implement", ("allowed.txt",), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            worktree = artifact / "worktree"
            git(root, "worktree", "add", "-b", "codex/grok-test", str(worktree), base)
            (worktree / "unapproved.txt").write_text("outside scope\n", encoding="utf-8")
            process = bridge.ProcessResult(0, "", "", 0.1, False, False, False)
            segment = {
                "process": process, "envelope": {"sessionId": "00000000-0000-4000-8000-000000000004"},
                "claim": claim(), "error": None,
                "session_id": "00000000-0000-4000-8000-000000000004",
                "hit_cap": False, "checkpoint": None, "label": "initial", "granted_turns": 2,
                "context_evidence": {"status": "verified", "observed_context_paths": ["README.md"], "missing_context_paths": []},
            }
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, worktree, segment, before)
            self.assertEqual(result["lifecycle_status"], "BLOCKED")
            self.assertEqual(result["unauthorized_changed_paths"], ["unapproved.txt"])

    def test_claimed_edit_without_a_diff_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, base = self._repo(Path(temp))
            task = self._task(root, "implement", ("allowed.txt",), base)
            artifact = Path(temp) / "artifact"
            artifact.mkdir()
            worktree = artifact / "worktree"
            git(root, "worktree", "add", "-b", "codex/grok-test", str(worktree), base)
            false_claim = claim()
            false_claim["files_changed"] = ["allowed.txt"]
            process = bridge.ProcessResult(0, "", "", 0.1, False, False, False)
            segment = {
                "process": process, "envelope": {"sessionId": "00000000-0000-4000-8000-000000000004"},
                "claim": false_claim, "error": None,
                "session_id": "00000000-0000-4000-8000-000000000004",
                "hit_cap": False, "checkpoint": None, "label": "initial", "granted_turns": 2,
                "context_evidence": {"status": "verified", "observed_context_paths": ["README.md"], "missing_context_paths": []},
            }
            before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
            result = bridge._finish(task, artifact, worktree, segment, before)
            self.assertEqual(result["lifecycle_status"], "BLOCKED")
            self.assertIn("Grok claimed file changes that are absent from the worktree", result["failures"])

    def test_revise_resumes_same_session_with_review_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, artifact, _worktree, artifact_root, feedback = self._complete_artifact(Path(temp))
            original = (artifact / "result.json").read_bytes()
            original_task = (artifact / "task.json").read_bytes()
            calls = []

            def fake_segment(task, worktree, art, executable, assignment, turns, label, *, resume_session=None):
                calls.append({"assignment": assignment, "turns": turns, "label": label, "resume": resume_session})
                (worktree / "allowed.txt").write_text("fixed by revision\n", encoding="utf-8")
                return self._segment(self._changed_claim(), label, turns)

            with patch.object(bridge, "ARTIFACT_ROOT", artifact_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight", return_value={"executable": "grok"}), \
                 patch.object(bridge, "_run_segment", side_effect=fake_segment):
                result = bridge.revise_task(task_file, artifact, feedback, 4)
                self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
                self.assertEqual(calls[0]["resume"], SESSION)
                self.assertEqual(calls[0]["label"], "revision-1")
                self.assertEqual(calls[0]["turns"], 4)
                self.assertIn("The draft text does not match the contract", calls[0]["assignment"])
                self.assertEqual([item["label"] for item in result["segments"]], ["initial", "revision-1"])
                self.assertEqual(result["revisions"][0]["target_state"], "complete")
                self.assertEqual((artifact / "revision-1.prior-result.json").read_bytes(), original)
                self.assertEqual((artifact / "task.json").read_bytes(), original_task)
                with self.assertRaisesRegex(bridge.GrokBridgeError, "already applied"):
                    bridge.revise_task(task_file, artifact, feedback, 4)
            self.assertEqual(len(calls), 1)

    def test_revise_rejects_unbounded_grants_escapes_wrong_state_and_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, artifact, worktree, artifact_root, feedback = self._complete_artifact(Path(temp))

            def unchanged_segment(task, wt, art, executable, assignment, turns, label, *, resume_session=None):
                return self._segment(self._changed_claim(), label, turns)

            with patch.object(bridge, "ARTIFACT_ROOT", artifact_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight", return_value={"executable": "grok"}) as preflight, \
                 patch.object(bridge, "_run_segment", side_effect=unchanged_segment):
                for grant in (0, 7):
                    with self.assertRaisesRegex(bridge.GrokBridgeError, "1 through 6"):
                        bridge.revise_task(task_file, artifact, feedback, grant)
                escaped = json.loads(feedback.read_text(encoding="utf-8"))
                for path in ("../allowed.txt", "README.md", "/allowed.txt"):
                    escaped["findings"][0]["path"] = path
                    feedback.write_text(json.dumps(escaped), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "path|outside|escapes"):
                        bridge.revise_task(task_file, artifact, feedback, 2)
                preflight.assert_not_called()
                escaped["findings"][0]["path"] = "allowed.txt"
                feedback.write_text(json.dumps(escaped), encoding="utf-8")
                result_path = artifact / "result.json"
                saved = json.loads(result_path.read_text(encoding="utf-8"))
                result_path.write_text(json.dumps({**saved, "status": "extension_requested",
                                                   "lifecycle_status": "EXTENSION_REQUESTED"}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "not a revisable result"):
                    bridge.revise_task(task_file, artifact, feedback, 2)
                result_path.write_text(json.dumps(saved), encoding="utf-8")
                (worktree / "allowed.txt").write_text("changed after result\n", encoding="utf-8")
                with self.assertRaisesRegex(bridge.GrokBridgeError, "changed after the recorded result"):
                    bridge.revise_task(task_file, artifact, feedback, 2)
                preflight.assert_not_called()
                (worktree / "allowed.txt").write_text("draft\n", encoding="utf-8")
                result = bridge.revise_task(task_file, artifact, feedback, 2)
            self.assertEqual(result["lifecycle_status"], "BLOCKED")
            self.assertIn("revision made no measurable change to the worktree", result["failures"])

    def test_run_seeds_verified_claude_handoff_into_new_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            task_file, package, source_worktree = self._claude_handoff(parent)
            grok_root = parent / "grok-artifacts"
            grok_root.mkdir()
            seen = {}

            def fake_segment(task, worktree, art, executable, assignment, turns, label, *, resume_session=None):
                seen["assignment"] = assignment
                seen["files"] = (worktree / "allowed.txt").read_text(encoding="utf-8")
                return self._segment(claim(), label, turns)

            with patch.object(bridge, "ARTIFACT_ROOT", grok_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight", return_value={"executable": "grok"}), \
                 patch.object(bridge, "_run_segment", side_effect=fake_segment):
                result = bridge.run(task_file, handoff=package)
            worktree = Path(result["worktree"])
            self.assertNotEqual(worktree, source_worktree.resolve())
            self.assertEqual(seen["files"], "from claude\n")
            self.assertIn("allowed.txt", seen["assignment"])
            self.assertEqual((worktree / "data.bin").read_bytes(), b"\x00\x01binary\xff")
            self.assertEqual(result["handoff"]["inherited_changed_paths"], ["allowed.txt", "data.bin"])
            self.assertEqual(result["changed_paths"], ["allowed.txt", "data.bin"])
            self.assertEqual(result["lifecycle_status"], "REVIEW_PENDING")
            self.assertTrue(Path(result["handoff"]["inherited_diff_path"]).is_file())
            self.assertTrue((Path(result["artifact_directory"]) / "handoff-manifest.json").is_file())
            self.assertEqual((source_worktree / "allowed.txt").read_text(encoding="utf-8"), "from claude\n")

    def test_run_refuses_corrupt_handoff_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            task_file, package, _source = self._claude_handoff(parent)
            (package / "files" / "allowed.txt").write_text("tampered\n", encoding="utf-8")
            grok_root = parent / "grok-artifacts"
            grok_root.mkdir()
            with patch.object(bridge, "ARTIFACT_ROOT", grok_root), \
                 patch.object(bridge, "_shadow_root"), \
                 patch.object(bridge, "_preflight") as preflight, \
                 patch.object(bridge, "_run_segment") as runner:
                with self.assertRaisesRegex(bridge.BridgeError, "corrupt"):
                    bridge.run(task_file, handoff=package)
            preflight.assert_not_called()
            runner.assert_not_called()
            self.assertEqual(list(grok_root.iterdir()), [])

    @staticmethod
    def _changed_claim() -> dict:
        changed = claim()
        changed["files_changed"] = ["allowed.txt"]
        return changed

    @staticmethod
    def _segment(worker_claim: dict, label: str, turns: int) -> dict:
        return {
            "process": bridge.ProcessResult(0, "", "", 0.1, False, False, False),
            "envelope": {"sessionId": SESSION}, "claim": worker_claim, "error": None,
            "session_id": SESSION, "hit_cap": False, "checkpoint": None,
            "label": label, "granted_turns": turns,
            "context_evidence": {"status": "verified", "observed_context_paths": ["README.md"],
                                 "missing_context_paths": []},
        }

    @classmethod
    def _complete_artifact(cls, parent: Path) -> tuple[Path, Path, Path, Path, Path]:
        root, base = cls._repo(parent)
        task = cls._task(root, "implement", ("allowed.txt",), base)
        artifact_root = parent / "artifacts"
        artifact = artifact_root / "complete"
        artifact.mkdir(parents=True)
        worktree = artifact / "worktree"
        git(root, "worktree", "add", "-b", "codex/grok-revise", str(worktree), base)
        (worktree / "allowed.txt").write_text("draft\n", encoding="utf-8")
        task_file = parent / "task.json"
        task_file.write_text(json.dumps(task.raw), encoding="utf-8")
        (artifact / "task.json").write_text(json.dumps(task.raw), encoding="utf-8")
        before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        (artifact / "primary-status.before.txt").write_text(before, encoding="utf-8")
        result = bridge._finish(task, artifact, worktree, cls._segment(cls._changed_claim(), "initial", 3), before)
        assert result["lifecycle_status"] == "REVIEW_PENDING", result["failures"]
        result["preflight"] = {"executable": "grok"}
        (artifact / "result.json").write_text(json.dumps(result), encoding="utf-8")
        feedback = parent / "feedback.json"
        feedback.write_text(json.dumps({"findings": [{
            "path": "allowed.txt",
            "issue": "The draft text does not match the contract",
            "expected_behavior": "The file must say 'fixed by revision' on one line",
        }]}), encoding="utf-8")
        return task_file, artifact, worktree, artifact_root, feedback

    @classmethod
    def _claude_handoff(cls, parent: Path) -> tuple[Path, Path, Path]:
        root, base = cls._repo(parent)
        task = cls._task(root, "implement", ("allowed.txt", "data.bin"), base)
        task_file = parent / "task.json"
        task_file.write_text(json.dumps(task.raw), encoding="utf-8")
        claude_root = parent / "claude-artifacts"
        artifact = claude_root / "source"
        artifact.mkdir(parents=True)
        (artifact / "task.json").write_text(json.dumps(task.raw), encoding="utf-8")
        worktree = artifact / "worktree"
        git(root, "worktree", "add", "-b", "delegate/claude-source", str(worktree), base)
        (worktree / "allowed.txt").write_text("from claude\n", encoding="utf-8")
        (worktree / "data.bin").write_bytes(b"\x00\x01binary\xff")
        fingerprint = bridge.shared_handoff._worktree_fingerprint(worktree, base)
        (artifact / "result.json").write_text(json.dumps({
            "task_id": task.task_id, "status": "failed", "lifecycle_status": "BLOCKED",
            "error_kind": "rate_limit", "starting_commit": base, "worktree": str(worktree),
            "primary_checkout_unchanged": True, "unauthorized_changed_paths": [],
            "segments": [{"label": "initial", "worktree_fingerprint": fingerprint}],
        }), encoding="utf-8")
        exported = bridge.shared_handoff.export_handoff(claude_root, task_file, artifact, parent / "package")
        return task_file, Path(exported["output_directory"]), worktree

    @staticmethod
    def _repo(parent: Path) -> tuple[Path, str]:
        root = parent / "repo"
        root.mkdir()
        git(root, "init", "-q")
        (root / "README.md").write_text("Small test repository\n", encoding="utf-8")
        git(root, "add", "README.md")
        git(root, "-c", "user.name=HoYin1600p", "-c", "user.email=hoyin1600p@gmail.com", "commit", "-qm", "Initial")
        return root, git(root, "rev-parse", "HEAD")

    @staticmethod
    def _task(root: Path, mode: str, allowed: tuple[str, ...], base: str = "0" * 40):
        from codex_claude_bridge.contracts import validate_task

        return validate_task({
            "task_id": "grok-test", "repo_root": str(root), "base_commit": base,
            "mode": mode, "objective": "Return the heading", "context_paths": ["README.md"],
            "forbidden_context": [], "allowed_changed_paths": list(allowed),
            "acceptance_criteria": ["Structured reply"], "model": "grok-4.7-build-fast",
            "max_turns": 3, "timeout_seconds": 60,
            "allow_subagents": False, "require_subscription_auth": True,
        })

    @classmethod
    def _failed_cap_artifact(cls, parent: Path) -> tuple[Path, Path, Path, Path]:
        root, base = cls._repo(parent)
        task = cls._task(root, "implement", ("allowed.txt",), base)
        artifact_root = parent / "artifacts"
        artifact = artifact_root / "failed-cap"
        artifact.mkdir(parents=True)
        worktree = artifact / "worktree"
        git(root, "worktree", "add", "-b", "codex/grok-recover", str(worktree), base)
        (worktree / "allowed.txt").write_text("worker edit\n", encoding="utf-8")
        task_file = parent / "task.json"
        task_file.write_text(json.dumps(task.raw), encoding="utf-8")
        (artifact / "task.json").write_text(json.dumps(task.raw), encoding="utf-8")
        session = "00000000-0000-4000-8000-000000000004"
        stdout = json.dumps({"text": "Started the requested edit", "stopReason": "cancelled",
                             "sessionId": session, "num_turns": 3})
        stderr = "Error: max turns reached\n"
        (artifact / "initial.stdout.json").write_text(stdout, encoding="utf-8")
        (artifact / "initial.stderr.log").write_text(stderr, encoding="utf-8")
        (artifact / "initial.session-export.md").write_text("## Tools\n\n- Read: README.md\n", encoding="utf-8")
        before = bridge._git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
        (artifact / "primary-status.before.txt").write_text(before, encoding="utf-8")
        process = bridge.ProcessResult(1, stdout, stderr, 1.0, False, False, False)
        segment = {"process": process, "envelope": json.loads(stdout), "claim": None,
                   "error": "Grok text was not structured JSON", "session_id": session,
                   "hit_cap": True, "checkpoint": None, "label": "initial", "granted_turns": 3,
                   "context_evidence": {"status": "verified", "observed_context_paths": ["README.md"],
                                        "missing_context_paths": []}}
        result = bridge._finish(task, artifact, worktree, segment, before)
        result["preflight"] = {"executable": "grok"}
        result["requested_reasoning_effort"] = "high"
        (artifact / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return task_file, artifact, worktree, artifact_root


if __name__ == "__main__":
    unittest.main()
