"""Fresh-profile installation and provider-routing regression tests."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_grok_bridge as grok_tests

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_private_storage_rejects_repository_before_writing(self):
        from codex_claude_bridge.state import ensure_state_root
        from codex_claude_bridge.launcher import _safe_artifact_root
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            target = repo / "private-state"
            with patch.dict(os.environ, DELEGATE_WORK_STATE_DIR=str(target)):
                with self.assertRaisesRegex(ValueError, "outside every Git repository"):
                    ensure_state_root()
            with self.assertRaisesRegex(ValueError, "outside every Git repository"):
                _safe_artifact_root(target)
            self.assertFalse(target.exists())

    def test_fresh_profile_install_and_offline_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "new-user"
            home.mkdir()
            destination = home / ".agents/skills/delegate-work"
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("CLAUDE", "ANTHROPIC", "GROK", "XAI", "CODEX", "PYTHONPATH", "DELEGATE_WORK"))}
            env.update(USERPROFILE=str(home), HOME=str(home), CODEX_HOME=str(home / ".codex"),
                       GROK_HOME=str(home / ".grok"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                       PYTHONDONTWRITEBYTECODE="1", CODEX_CLAUDE_BRIDGE_ROOT=str(root / "absent-legacy-bridge"))
            env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent),
                                          str(Path(shutil.which("git")).parent),
                                          str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")])

            def command(*args, expected=0):
                result = subprocess.run([sys.executable, "-B", *map(str, args)], env=env,
                                        cwd=root, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                return json.loads(result.stdout)

            installed = command(ROOT / "install.py", "--destination", destination)
            self.assertEqual(installed["status"], "installed")
            setup = destination / "scripts/setup.py"
            self.assertEqual(command(setup, "doctor", "--offline")["status"], "installation_ready")
            self.assertFalse(command(setup, "self-test")["provider_called"])
            live = command(setup, "doctor", "--require-grok", expected=2)
            self.assertEqual(live["status"], "setup_required")
            missing = [c for c in live["checks"] if c["status"] == "setup_required"]
            self.assertEqual({c["check"] for c in missing}, {"claude_subscription", "grok_subscription"})
            self.assertTrue(all(c["next_action"] for c in missing))
            command(ROOT / "install.py", "--destination", destination, expected=2)
            self.assertFalse(list(destination.rglob("__pycache__")))
            self.assertFalse(list(destination.rglob(".delegate-work-state.json")))

    def test_installer_excludes_account_and_runtime_files(self):
        spec = importlib.util.spec_from_file_location("distribution_installer", ROOT / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(installer.SOURCE, source)
            for name in (".env", ".credentials.json", "runtime.log", "scripts/session.json", "assets/auth.json"):
                path = source / name
                path.write_text("synthetic excluded content", encoding="utf-8")
            destination = Path(directory) / "installed"
            with patch.object(installer, "SOURCE", source):
                installer.install(destination)
            for name in (".env", ".credentials.json", "runtime.log", "scripts/session.json", "assets/auth.json"):
                self.assertFalse((destination / name).exists())

    def test_cross_provider_model_does_not_change_canonical_task(self):
        bridge = grok_tests.bridge
        task = grok_tests.GrokBridgeTests._task(Path.cwd(), "analyze", ())
        raw = dict(task.raw, model="claude-subscription-model")
        canonical = replace(task, model=raw["model"], raw=raw)
        selected = bridge._runtime_task(canonical)
        self.assertEqual(selected.model, bridge.DEFAULT_MODEL)
        self.assertEqual(selected.raw["model"], "claude-subscription-model")
        self.assertEqual(canonical.model, "claude-subscription-model")
        resumed = bridge._runtime_task(canonical, prior={"requested_model": "grok-preserved-model"})
        self.assertEqual(resumed.model, "grok-preserved-model")
        with self.assertRaises(bridge.GrokBridgeError):
            bridge._runtime_task(canonical, prior={})

    def test_grok_rejects_billing_overrides_without_echoing_values(self):
        bridge = grok_tests.bridge
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for content in ('[model."grok-4.7"]\nbase_url="https://example.invalid/private"',
                            'auth="malformed"', 'model="malformed"',
                            '[model]\n"grok-4.7"="malformed"',
                            '[auth]\nauth_provider_command="private-helper"'):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(bridge.GrokBridgeError) as caught:
                    bridge._check_routing_file(path, "grok-4.7")
                self.assertNotIn("example.invalid", str(caught.exception))
                self.assertNotIn("private-helper", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
