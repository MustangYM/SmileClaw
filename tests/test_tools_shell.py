import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.shell import run


class ShellToolTests(unittest.TestCase):

    def test_run_handles_non_utf8_bytes_output(self):
        class _Result:
            returncode = 0
            stdout = b"ok\xff\xfe"
            stderr = b"warn\xff"

        with patch("tools.shell.subprocess.run", return_value=_Result()):
            out = run("echo test")

        self.assertTrue(out["ok"])
        self.assertIn("ok", out["stdout"])
        self.assertIn("warn", out["stderr"])


if __name__ == "__main__":
    unittest.main()
