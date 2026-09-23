from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_claude_bridge.auth import _find_claude_executable


class AuthTests(unittest.TestCase):
    def test_native_install_is_preferred_over_stale_path_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            native = home / ".local" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"")
            with (
                patch("codex_claude_bridge.auth.os.name", "nt"),
                patch("codex_claude_bridge.auth.Path.home", return_value=home),
                patch("codex_claude_bridge.auth.shutil.which", return_value="C:/old/claude.exe") as which,
            ):
                executable = _find_claude_executable()
        self.assertEqual(executable, str(native))
        which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
