from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path


class ProjectConfigTests(unittest.TestCase):
    def test_console_script_entrypoints_are_importable(self):
        data = tomllib.loads(Path("pyproject.toml").read_text())
        scripts = data["project"]["scripts"]

        for target in scripts.values():
            module_name, attr = target.split(":", 1)
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, attr)))


if __name__ == "__main__":
    unittest.main()
