import ast
import importlib.util
import os
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "examples", "test.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "examples_test_module_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExamplesTestModuleSyntaxTests(unittest.TestCase):
    def test_source_file_exists(self):
        self.assertTrue(
            os.path.isfile(MODULE_PATH),
            "expected examples/test.py to exist at %s" % MODULE_PATH,
        )

    def test_source_compiles(self):
        # Regression: bare `import`, missing `def` colon, and a `try`
        # without `except`/`finally` all raised SyntaxError at compile time.
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        try:
            compile(source, MODULE_PATH, "exec")
        except SyntaxError as exc:
            self.fail("examples/test.py failed to compile: %s" % exc)

    def test_source_parses_as_ast(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        try:
            tree = ast.parse(source, filename=MODULE_PATH)
        except SyntaxError as exc:
            self.fail("examples/test.py failed to parse: %s" % exc)
        self.assertIsInstance(tree, ast.Module)

    def test_no_bare_import_statement(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=MODULE_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(
                    node.names,
                    "found an import statement with no module names",
                )

    def test_try_blocks_have_handlers_or_finalbody(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=MODULE_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                self.assertTrue(
                    node.handlers or node.finalbody,
                    "try block at line %s has no except/finally" % node.lineno,
                )


class ExamplesTestModuleBehaviorTests(unittest.TestCase):
    def test_module_imports_and_exposes_main(self):
        module = _load_module()
        self.assertTrue(
            hasattr(module, "main"),
            "expected module to define a `main` function",
        )
        self.assertTrue(callable(module.main))

    def test_main_returns_one(self):
        module = _load_module()
        self.assertEqual(module.main(), 1)


if __name__ == "__main__":
    unittest.main()
