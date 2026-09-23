from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from codex_claude_bridge.auth import Preflight
from codex_claude_bridge.usage import UsageError, fetch_usage, parse_usage_payload
from codex_claude_bridge.launcher import ClaudeBridge, _usage_gate
from codex_claude_bridge.process import ProcessResult


PAYLOAD = {
    "five_hour": {"utilization": 58.0, "resets_at": "2026-09-21T13:00:00-06:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2026-09-22T05:00:00-06:00"},
    "limits": [
        {
            "kind": "weekly_scoped",
            "percent": 14,
            "resets_at": "2026-09-22T05:00:00-06:00",
            "scope": {"model": {"id": "claude-fable-5", "display_name": "Fable"}},
        }
    ],
}


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(PAYLOAD).encode("utf-8")


class UsageTests(unittest.TestCase):
    def test_parses_five_hour_weekly_and_scoped_remaining_usage(self) -> None:
        usage = parse_usage_payload(PAYLOAD)
        self.assertEqual(usage["five_hour"]["remaining_percent"], 42.0)
        self.assertEqual(usage["weekly"]["remaining_percent"], 91.0)
        self.assertEqual(usage["weekly_scoped"][0]["model"], "Fable")
        self.assertEqual(usage["weekly_scoped"][0]["model_id"], "claude-fable-5")
        self.assertEqual(usage["weekly_scoped"][0]["remaining_percent"], 86.0)

    def test_missing_required_window_is_an_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "weekly window"):
            parse_usage_payload({"five_hour": PAYLOAD["five_hour"]})

    def test_weekly_and_scoped_windows_are_advisory(self) -> None:
        usage = parse_usage_payload(PAYLOAD)
        self.assertTrue(_usage_gate(usage, "claude-fable-5")["allowed"])
        usage["weekly"]["remaining_percent"] = 0.0
        usage["weekly_scoped"][0]["remaining_percent"] = 0.0
        gate = _usage_gate(usage, "claude-fable-5")
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["blockers"], [])
        self.assertEqual({window["name"] for window in gate["advisory_windows"]}, {"weekly", "weekly_fable"})
        self.assertTrue(_usage_gate(usage, "claude-sonnet-5")["allowed"])

    def test_usage_gate_normalizes_display_name_and_model_identifier(self) -> None:
        usage = parse_usage_payload(PAYLOAD)
        usage["weekly_scoped"][0].update(
            {
                "model": "Claude Opus 4.5",
                "model_id": None,
                "remaining_percent": 5.0,
            }
        )
        gate = _usage_gate(usage, "claude-opus-4-5-20251101")
        self.assertTrue(gate["allowed"])
        self.assertIn("weekly_claudeopus45", {window["name"] for window in gate["advisory_windows"]})

    def test_usage_gate_applies_all_scoped_windows_when_model_is_unknown(self) -> None:
        usage = parse_usage_payload(PAYLOAD)
        usage["weekly_scoped"][0]["remaining_percent"] = 5.0
        gate = _usage_gate(usage, None)
        self.assertTrue(gate["allowed"])
        self.assertIn("weekly_fable", {window["name"] for window in gate["advisory_windows"]})

    def test_usage_gate_stops_only_when_five_hour_window_is_exhausted(self) -> None:
        for remaining in (0.0, 0, -1.0):
            with self.subTest(remaining=remaining):
                usage = parse_usage_payload(PAYLOAD)
                usage["five_hour"]["remaining_percent"] = remaining
                gate = _usage_gate(usage, "claude-fable-5")
                self.assertFalse(gate["allowed"])
                self.assertEqual(gate["reason"], "five_hour_exhausted")
                self.assertTrue(gate["blockers"])

    def test_usage_gate_allows_any_positive_five_hour_remaining(self) -> None:
        for remaining in (0.5, 5.0, 10.0):
            with self.subTest(remaining=remaining):
                usage = parse_usage_payload(PAYLOAD)
                usage["five_hour"]["remaining_percent"] = remaining
                gate = _usage_gate(usage, "claude-fable-5")
                self.assertTrue(gate["allowed"])
                self.assertIsNone(gate["reason"])
                self.assertEqual(gate["blockers"], [])

    def test_unknown_or_malformed_usage_blocks_distinctly(self) -> None:
        cases = {
            "nan": lambda usage: usage["five_hour"].update(remaining_percent=float("nan")),
            "infinite": lambda usage: usage["five_hour"].update(remaining_percent=float("inf")),
            "over_100": lambda usage: usage["five_hour"].update(remaining_percent=150.0),
            "boolean": lambda usage: usage["five_hour"].update(remaining_percent=True),
            "string": lambda usage: usage["five_hour"].update(remaining_percent="50"),
            "missing_five_hour": lambda usage: usage.pop("five_hour"),
            "missing_weekly": lambda usage: usage.pop("weekly"),
            "not_available": lambda usage: usage.update(status="unavailable"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                usage = parse_usage_payload(PAYLOAD)
                mutate(usage)
                gate = _usage_gate(usage, "claude-fable-5")
                self.assertFalse(gate["allowed"])
                self.assertEqual(gate["reason"], "usage_unavailable")
        for value in (None, [], "available"):
            with self.subTest(snapshot=value):
                self.assertEqual(_usage_gate(value, None)["reason"], "usage_unavailable")

    def test_fetch_uses_token_without_returning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            (config / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "secret-token"}}),
                encoding="utf-8",
            )
            captured = {}

            def opener(request, *, timeout):
                captured["authorization"] = request.headers["Authorization"]
                captured["timeout"] = timeout
                return FakeResponse()

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                usage = fetch_usage(claude_version="2.1.268 (Claude Code)", opener=opener)
        self.assertEqual(captured["authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", json.dumps(usage))
        self.assertEqual(usage["status"], "available")

    def test_fetch_retries_http_429_before_succeeding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            (config / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "secret-token"}}),
                encoding="utf-8",
            )
            attempts = 0
            delays: list[float] = []

            def opener(_request, *, timeout):
                nonlocal attempts
                self.assertEqual(timeout, 15)
                attempts += 1
                if attempts < 3:
                    raise urllib.error.HTTPError(
                        "https://api.anthropic.com/api/oauth/usage",
                        429,
                        "Too Many Requests",
                        {"Retry-After": "0"},
                        None,
                    )
                return FakeResponse()

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                usage = fetch_usage(
                    claude_version="2.1.268 (Claude Code)",
                    opener=opener,
                    sleeper=delays.append,
                )
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.0, 0.0])
        self.assertEqual(usage["request_attempts"], 3)

    def test_bridge_reuses_recent_usage_instead_of_requerying(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "artifacts"
            bridge = ClaudeBridge(root)
            executable = Path(temp) / "claude.exe"
            executable.write_bytes(b"")
            preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())
            live = parse_usage_payload(PAYLOAD)
            with (
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher.fetch_usage", return_value=live) as fetch,
            ):
                first = bridge.usage()
                second = bridge.usage()
        fetch.assert_called_once()
        self.assertEqual(first["retrieval"]["mode"], "live")
        self.assertEqual(second["retrieval"]["mode"], "cache")

    def test_transient_refresh_uses_recent_high_headroom_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "artifacts"
            bridge = ClaudeBridge(root)
            executable = Path(temp) / "claude.exe"
            executable.write_bytes(b"")
            preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())
            live = parse_usage_payload(PAYLOAD)
            error = UsageError("Claude usage check failed with HTTP 429", transient=True, status_code=429)
            with (
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher.fetch_usage", side_effect=[live, error]),
            ):
                bridge.usage()
                fallback = bridge.usage(force_refresh=True)
        self.assertEqual(fallback["retrieval"]["mode"], "stale_fallback")
        self.assertIn("HTTP 429", fallback["retrieval"]["warning"])
        self.assertEqual(fallback["five_hour"]["remaining_percent"], 42.0)

    def test_transient_refresh_does_not_reuse_low_headroom_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "artifacts"
            bridge = ClaudeBridge(root)
            executable = Path(temp) / "claude.exe"
            executable.write_bytes(b"")
            preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())
            live = parse_usage_payload(PAYLOAD)
            live["five_hour"]["remaining_percent"] = 10.0
            error = UsageError("Claude usage check failed with HTTP 429", transient=True, status_code=429)
            with (
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher.fetch_usage", side_effect=[live, error]),
            ):
                bridge.usage()
                with self.assertRaises(UsageError):
                    bridge.usage(force_refresh=True)

    def test_expired_oauth_is_refreshed_through_official_claude_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "artifacts"
            bridge = ClaudeBridge(root)
            executable = Path(temp) / "claude.exe"
            executable.write_bytes(b"")
            preflight = Preflight(executable, "2.1.268", True, "claude.ai", "firstParty", "max", ())
            expired = UsageError("Claude usage check failed with HTTP 401", status_code=401)
            refreshed = parse_usage_payload(PAYLOAD)
            refresh_process = ProcessResult(0, "{}", "", 0.5, False, False, False)
            with (
                patch("codex_claude_bridge.launcher.inspect_preflight", return_value=preflight),
                patch("codex_claude_bridge.launcher.fetch_usage", side_effect=[expired, refreshed]),
                patch("codex_claude_bridge.launcher.run_process", return_value=refresh_process) as runner,
            ):
                usage = bridge.usage()
        self.assertEqual(usage["retrieval"]["mode"], "live")
        self.assertEqual(usage["oauth_refresh"]["method"], "official_claude_cli_no_tools")
        args = runner.call_args.args[1]
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(args[args.index("--max-turns") + 1], "1")


if __name__ == "__main__":
    unittest.main()
