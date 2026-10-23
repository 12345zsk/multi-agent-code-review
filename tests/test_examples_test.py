import importlib.util
import os
import unittest


MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "test.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("examples_test_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExamplesTestModuleTests(unittest.TestCase):
    def test_module_parses_and_imports(self):
        # Regression: a bare `import` line previously caused a SyntaxError,
        # making the module impossible to import.
        module = _load_module()
        self.assertTrue(hasattr(module, "main"))
        self.assertTrue(callable(module.main))

    def test_main_does_not_raise_zero_division(self):
        # Regression: main() previously did `return 1/0` unconditionally,
        # always raising ZeroDivisionError.
        module = _load_module()
        try:
            result = module.main()
        except ZeroDivisionError:
            self.fail("main() raised ZeroDivisionError; expected a defined fallback")
        self.assertEqual(result, 0)

    def test_main_returns_defined_value(self):
        # Normal case: main() returns a concrete value rather than raising.
        module = _load_module()
        result = module.main()
        self.assertIsNotNone(result)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
