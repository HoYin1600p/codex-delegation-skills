from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_claude_bridge.launcher import (
    EMPTY_MCP_CONFIG,
    actual_model,
    classify_error,
    observed_metrics,
    parse_envelope,
)
from codex_claude_bridge.process import run_process


PYTHON = Path(sys.executable).resolve()


class ProcessTests(unittest.TestCase):
    def test_empty_mcp_config_matches_cli_schema_shape(self) -> None:
        self.assertEqual(json.loads(EMPTY_MCP_CONFIG), {"mcpServers": {}})

    def test_captures_failure_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = run_process(
                PYTHON,
                ["-c", "import sys; print('partial'); print('failure detail', file=sys.stderr); raise SystemExit(7)"],
                cwd=temp,
                timeout_seconds=10,
            )
        self.assertEqual(result.exit_code, 7)
        self.assertIn("partial", result.stdout)
        self.assertIn("failure detail", result.stderr)
        self.assertFalse(result.timed_out)

    def test_timeout_kills_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / "child-survived.txt"
            child_code = "import pathlib,time; time.sleep(2); pathlib.Path(r'%s').write_text('alive')" % sentinel
            parent_code = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(30)"
            )
            result = run_process(PYTHON, ["-c", parent_code], cwd=root, timeout_seconds=0.4)
            time.sleep(2.5)
            self.assertTrue(result.timed_out)
            self.assertFalse(sentinel.exists(), "timed-out child process survived its parent")

    def test_cancellation_kills_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / "cancelled-child-survived.txt"
            child_code = "import pathlib,time; time.sleep(2); pathlib.Path(r'%s').write_text('alive')" % sentinel
            parent_code = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(30)"
            )
            cancel = threading.Event()
            timer = threading.Timer(0.4, cancel.set)
            timer.start()
            try:
                result = run_process(
                    PYTHON,
                    ["-c", parent_code],
                    cwd=root,
                    timeout_seconds=10,
                    cancel_event=cancel,
                )
            finally:
                timer.cancel()
            time.sleep(2.5)
            self.assertTrue(result.cancelled)
            self.assertTrue(result.interrupted)
            self.assertFalse(result.timed_out)
            self.assertFalse(sentinel.exists(), "cancelled child process survived its parent")

    def test_valid_terminal_json_and_metrics(self) -> None:
        raw = json.dumps(
            {
                "result": "READY",
                "session_id": "session",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "total_cost_usd": 0.01,
            }
        )
        envelope, error = parse_envelope(raw)
        self.assertIsNone(error)
        metrics = observed_metrics(envelope)
        self.assertEqual(metrics["input_tokens"]["value"], 10)
        self.assertEqual(metrics["actual_incremental_charge_usd"]["provenance"], "unavailable")

    def test_malformed_and_missing_terminal_results(self) -> None:
        self.assertIn("missing", parse_envelope("")[1])
        self.assertIn("malformed", parse_envelope("not-json")[1])

    def test_failed_run_accounting_is_partial_not_free(self) -> None:
        metrics = observed_metrics(
            {
                "is_error": True,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "total_cost_usd": 0,
                "modelUsage": {},
            }
        )
        self.assertEqual(metrics["accounting_completeness"], "partial")
        self.assertEqual(metrics["input_tokens"]["provenance"], "partial")
        self.assertEqual(metrics["estimated_api_equivalent_usd"]["provenance"], "partial")

    def test_fake_rate_limit_is_classified_without_model_usage(self) -> None:
        payload = {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "Rate limit reached. Try again later.",
        }
        code = f"import json; print(json.dumps({payload!r}))"
        with tempfile.TemporaryDirectory() as temp:
            process = run_process(PYTHON, ["-c", code], cwd=temp, timeout_seconds=10)
        envelope, error = parse_envelope(process.stdout)
        self.assertIsNone(error)
        self.assertEqual(classify_error(envelope), "rate_limit")

    def test_actual_model_falls_back_to_model_usage(self) -> None:
        envelope = {"modelUsage": {"alias": {"canonicalModel": "claude-sonnet-example"}}}
        self.assertEqual(actual_model(envelope), "claude-sonnet-example")


if __name__ == "__main__":
    unittest.main()
