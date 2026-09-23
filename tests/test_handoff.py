from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_claude_bridge.contracts import validate_task
from codex_claude_bridge.handoff import (
    HandoffError,
    export_handoff,
    package_fingerprint,
    seed_handoff,
    verify_handoff,
)
from codex_claude_bridge.launcher import _worktree_fingerprint


SESSION_ID = "00000000-0000-4000-8000-000000000006"
BINARY_BASE = bytes(range(256))
BINARY_NEW = b"\x00\xff\x10binary\x00\r\n\x80"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        # Byte-exact fixtures: write_text would translate newlines on Windows.
        (self.repo / "src" / "keep.txt").write_bytes(b"keep\n")
        (self.repo / "src" / "remove.bin").write_bytes(BINARY_BASE)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.task_data = {
            "task_id": "handoff-fixture-001",
            "repo_root": str(self.repo.resolve()),
            "base_commit": self.base,
            "mode": "implement",
            "objective": "Change the fixture files",
            "context_paths": ["src"],
            "forbidden_context": [],
            "allowed_changed_paths": ["src/**"],
            "acceptance_criteria": ["Files changed"],
            "model": "fable",
            "max_turns": 4,
            "timeout_seconds": 30,
            "allow_subagents": False,
            "require_subscription_auth": True,
        }
        self.task_file = self.root / "task.json"
        self.task_file.write_text(json.dumps(self.task_data), encoding="utf-8")
        self.artifact_root = self.root / "artifacts"
        self.artifact = self.artifact_root / "handoff-source"
        self.artifact.mkdir(parents=True)
        (self.artifact / "task.json").write_text(json.dumps(self.task_data), encoding="utf-8")
        self.worktree = self.artifact / "worktree"
        git(self.repo, "worktree", "add", "-q", "-b", "delegate/handoff-source", str(self.worktree), self.base)
        (self.worktree / "src" / "keep.txt").write_bytes(b"keep\nchanged\n")
        (self.worktree / "src" / "new.txt").write_bytes(b"new text\n")
        (self.worktree / "src" / "nested").mkdir()
        (self.worktree / "src" / "nested" / "image.bin").write_bytes(BINARY_NEW)
        (self.worktree / "src" / "remove.bin").unlink()
        self.record_result()
        self.output = self.root / "handoff"

    def record_result(self, **updates) -> str:
        fingerprint = _worktree_fingerprint(self.worktree, self.base)
        result = {
            "task_id": self.task_data["task_id"],
            "status": "failed",
            "lifecycle_status": "BLOCKED",
            "error_kind": "rate_limit",
            "starting_commit": self.base,
            "worktree": str(self.worktree),
            "session_id": SESSION_ID,
            "primary_checkout_unchanged": True,
            "unauthorized_changed_paths": [],
            "segments": [{"label": "initial", "session_id": SESSION_ID, "worktree_fingerprint": fingerprint}],
            **updates,
        }
        (self.artifact / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return fingerprint

    def export(self, output: Path | None = None) -> dict:
        return export_handoff(self.artifact_root, self.task_file, self.artifact, output or self.output)

    def target_task(self, **updates):
        return validate_task({**self.task_data, "task_id": "handoff-target-001", **updates})

    def target_worktree(self, name: str = "target") -> Path:
        path = self.root / name
        git(self.repo, "worktree", "add", "-q", "-b", f"grok/{name}", str(path), self.base)
        return path

    def rewrite_manifest(self, mutate, *, refingerprint: bool = True) -> None:
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        if refingerprint:
            manifest["package_fingerprint"] = package_fingerprint(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_round_trip_preserves_text_binary_untracked_and_deleted_files(self) -> None:
        primary_before = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
        recorded = _worktree_fingerprint(self.worktree, self.base)
        exported = self.export()
        manifest = exported["manifest"]
        self.assertEqual(exported["status"], "exported")
        self.assertEqual(
            {entry["path"]: entry["change"] for entry in manifest["files"]},
            {
                "src/keep.txt": "modified",
                "src/nested/image.bin": "added",
                "src/new.txt": "added",
                "src/remove.bin": "deleted",
            },
        )
        self.assertEqual((self.output / "files" / "src" / "nested" / "image.bin").read_bytes(), BINARY_NEW)
        self.assertFalse((self.output / "files" / "src" / "remove.bin").exists())
        self.assertEqual(manifest["base_commit"], self.base)
        self.assertEqual(manifest["source"]["worktree_fingerprint"], recorded)
        self.assertEqual(manifest["source"]["error_kind"], "rate_limit")
        self.assertEqual(manifest["package_fingerprint"], package_fingerprint(manifest))
        self.assertTrue(manifest["repository"]["git_common_dir"])

        task = self.target_task()
        verified = verify_handoff(self.output, task)
        target = self.target_worktree()
        seeded = seed_handoff(self.output, verified, target, task)
        self.assertEqual((target / "src" / "keep.txt").read_bytes(), b"keep\nchanged\n")
        self.assertEqual((target / "src" / "new.txt").read_bytes(), b"new text\n")
        self.assertEqual((target / "src" / "nested" / "image.bin").read_bytes(), BINARY_NEW)
        self.assertFalse((target / "src" / "remove.bin").exists())
        self.assertEqual(seeded["inherited_deleted_paths"], ["src/remove.bin"])
        self.assertEqual(len(seeded["inherited_changed_paths"]), 4)
        self.assertEqual(git(target, "rev-parse", "HEAD").strip(), self.base)
        # Source evidence and the primary checkout are untouched.
        self.assertEqual(_worktree_fingerprint(self.worktree, self.base), recorded)
        self.assertEqual((self.worktree / "src" / "nested" / "image.bin").read_bytes(), BINARY_NEW)
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), primary_before)

    def test_export_refuses_active_source_task(self) -> None:
        active = self.artifact_root / ".active"
        active.mkdir()
        (active / "handoff-fixture-001.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "active"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_export_refuses_fingerprint_mismatch(self) -> None:
        (self.worktree / "src" / "new.txt").write_text("changed after result\n", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "fingerprint"):
            self.export()

    def test_export_refuses_untrusted_or_out_of_scope_source(self) -> None:
        self.record_result(primary_checkout_unchanged=False)
        with self.assertRaisesRegex(HandoffError, "not trustworthy"):
            self.export()
        self.record_result(unauthorized_changed_paths=["README.md"])
        with self.assertRaisesRegex(HandoffError, "not trustworthy"):
            self.export()
        (self.worktree / "README.md").write_text("outside scope\n", encoding="utf-8")
        self.record_result()
        with self.assertRaisesRegex(HandoffError, "outside the source task scope"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_export_refuses_unsafe_output_locations(self) -> None:
        for output in (self.repo / "handoff", self.worktree / "handoff", self.artifact / "handoff"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(HandoffError, "outside the repository"):
                    self.export(output)
        self.output.mkdir()
        with self.assertRaisesRegex(HandoffError, "must not already exist"):
            self.export()

    def test_export_refuses_symlinked_change(self) -> None:
        try:
            os.symlink(self.worktree / "src" / "keep.txt", self.worktree / "src" / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable on this system")
        self.record_result()
        with self.assertRaisesRegex(HandoffError, "link"):
            self.export()

    def test_verify_rejects_corrupt_extra_and_tampered_packages(self) -> None:
        self.export()
        task = self.target_task()
        (self.output / "files" / "src" / "new.txt").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(HandoffError, "corrupt"):
            verify_handoff(self.output, task)
        (self.output / "files" / "src" / "new.txt").write_bytes(b"new text\n")
        (self.output / "files" / "src" / "extra.txt").write_bytes(b"undeclared\n")
        with self.assertRaisesRegex(HandoffError, "undeclared"):
            verify_handoff(self.output, task)
        (self.output / "files" / "src" / "extra.txt").unlink()
        self.rewrite_manifest(lambda manifest: manifest["files"][0].update(size=1), refingerprint=False)
        with self.assertRaisesRegex(HandoffError, "package fingerprint"):
            verify_handoff(self.output, task)

    def test_verify_rejects_path_traversal_even_with_recomputed_fingerprint(self) -> None:
        self.export()
        task = self.target_task()
        original = (self.output / "manifest.json").read_text(encoding="utf-8")
        for path in ("../evil.txt", "src/../../evil.txt", "C:/evil.txt", "/evil.txt", "src/.git/config"):
            with self.subTest(path=path):
                (self.output / "manifest.json").write_text(original, encoding="utf-8")
                self.rewrite_manifest(lambda manifest: manifest["files"].append({"path": path, "change": "deleted"}))
                with self.assertRaisesRegex(HandoffError, "escapes|relative|path"):
                    verify_handoff(self.output, task)

    def test_verify_rejects_foreign_base_and_repository(self) -> None:
        self.export()
        (self.repo / "other.txt").write_text("second commit\n", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-q", "-m", "second")
        second = git(self.repo, "rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(HandoffError, "different base commit"):
            verify_handoff(self.output, self.target_task(base_commit=second))
        clone = self.root / "clone"
        git(self.root, "clone", "-q", str(self.repo), str(clone))
        with self.assertRaisesRegex(HandoffError, "different repository"):
            verify_handoff(self.output, self.target_task(repo_root=str(clone.resolve())))

    def test_verify_rejects_paths_outside_target_scope(self) -> None:
        self.export()
        with self.assertRaisesRegex(HandoffError, "outside the target allowed_changed_paths"):
            verify_handoff(self.output, self.target_task(allowed_changed_paths=["docs/**"]))
        with self.assertRaisesRegex(HandoffError, "outside the target allowed_changed_paths"):
            verify_handoff(self.output, self.target_task(allowed_changed_paths=["src/keep.txt", "src/new.txt"]))

    def test_verify_rejects_source_changed_after_export(self) -> None:
        self.export()
        (self.worktree / "src" / "new.txt").write_text("changed after export\n", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "changed after the handoff"):
            verify_handoff(self.output, self.target_task())

    def test_verify_rejects_omitted_or_misclassified_changes_with_recomputed_fingerprint(self) -> None:
        self.export()
        task = self.target_task()
        manifest_path = self.output / "manifest.json"
        original = manifest_path.read_bytes()
        for omitted in ("src/new.txt", "src/remove.bin"):
            with self.subTest(omitted=omitted):
                manifest_path.write_bytes(original)
                blob = self.output / "files" / Path(omitted)
                removed = blob.read_bytes() if blob.exists() else None
                if removed is not None:
                    blob.unlink()
                self.rewrite_manifest(
                    lambda manifest: manifest.update(
                        files=[entry for entry in manifest["files"] if entry["path"] != omitted]
                    )
                )
                with self.assertRaisesRegex(HandoffError, "inventory"):
                    verify_handoff(self.output, task)
                if removed is not None:
                    blob.write_bytes(removed)
        manifest_path.write_bytes(original)

        def misclassify(manifest: dict) -> None:
            for entry in manifest["files"]:
                if entry["path"] == "src/keep.txt":
                    entry["change"] = "added"

        self.rewrite_manifest(misclassify)
        with self.assertRaisesRegex(HandoffError, "inventory"):
            verify_handoff(self.output, task)
        manifest_path.write_bytes(original)
        self.assertEqual(verify_handoff(self.output, task)["base_commit"], self.base)

    def test_verify_rejects_modified_source_result_or_task(self) -> None:
        self.export()
        task = self.target_task()
        result_path = self.artifact / "result.json"
        original_result = result_path.read_bytes()
        result_path.write_bytes(original_result + b"\n")
        with self.assertRaisesRegex(HandoffError, "source result changed"):
            verify_handoff(self.output, task)
        result_path.write_bytes(original_result)
        verify_handoff(self.output, task)
        task_path = self.artifact / "task.json"
        task_path.write_bytes(json.dumps(self.task_data, indent=2).encode("utf-8"))
        with self.assertRaisesRegex(HandoffError, "source task contract changed"):
            verify_handoff(self.output, task)

    def test_verify_rejects_source_task_active_after_export(self) -> None:
        self.export()
        active = self.artifact_root / ".active"
        active.mkdir()
        (active / "handoff-fixture-001.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "active"):
            verify_handoff(self.output, self.target_task())

    def test_plain_directory_scope_is_honoured_without_prefix_collisions(self) -> None:
        scoped = {**self.task_data, "allowed_changed_paths": ["src"]}
        for path in (self.task_file, self.artifact / "task.json"):
            path.write_text(json.dumps(scoped), encoding="utf-8")
        self.task_data = scoped
        exported = self.export()
        self.assertEqual(len(exported["manifest"]["files"]), 4)
        task = self.target_task()
        manifest = verify_handoff(self.output, task)
        seed_handoff(self.output, manifest, self.target_worktree(), task)
        (self.worktree / "src-other").mkdir()
        (self.worktree / "src-other" / "file.txt").write_bytes(b"prefix collision\n")
        self.record_result()
        with self.assertRaisesRegex(HandoffError, "outside the source task scope"):
            self.export(self.root / "collision-package")

    def test_seed_refuses_dirty_target(self) -> None:
        self.export()
        task = self.target_task()
        manifest = verify_handoff(self.output, task)
        target = self.target_worktree()
        (target / "src" / "stray.txt").write_text("not from the package\n", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "not clean"):
            seed_handoff(self.output, manifest, target, task)


if __name__ == "__main__":
    unittest.main()
