import importlib.util
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "examples", "test.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "examples_test_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComputeOnePlusOneTests(unittest.TestCase):
    def test_main_returns_two(self):
        module = _load_module()
        self.assertEqual(module.main(), 2)

    def test_main_returns_int(self):
        module = _load_module()
        self.assertIsInstance(module.main(), int)

    def test_import_has_no_side_effect_output(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _load_module()
        self.assertEqual(buffer.getvalue(), "")

    def test_running_as_script_prints_two(self):
        result = subprocess.run(
            [sys.executable, MODULE_PATH],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "2")


if __name__ == "__main__":
    unittest.main()
