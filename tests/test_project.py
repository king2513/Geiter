import re
import unittest
from pathlib import Path

import geiter


class ProjectContractTests(unittest.TestCase):
    root = Path(__file__).parent.parent

    def test_version_is_declared_consistently(self):
        pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        gateway = (self.root / "geiter" / "gateway.py").read_text(encoding="utf-8")
        self.assertIn(f'version = "{geiter.__version__}"', pyproject)
        self.assertIn("from . import __version__", gateway)
        self.assertIn('"version": __version__', gateway)

    def test_readme_is_utf8_and_agent_positioned(self):
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("built for agents first", readme)
        self.assertNotRegex(readme, r"[\u0080-\u009f]")
        self.assertIn("actions/workflows/ci.yml", readme)
        self.assertIn("python -m pip install .", readme)
        self.assertIn('"name":"geiter_resume"', readme)
        self.assertIn("python -m geiter connect", readme)
        self.assertIn("python -m geiter regression", readme)
        self.assertIn("geiter://capabilities", readme)
        self.assertIn("action list", readme)
        self.assertIn("action skip", readme)
        self.assertIn("action reclaim", readme)

    def test_release_workflow_is_tagged(self):
        workflow = (self.root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("tags:", workflow)
        self.assertIn('"v*"', workflow)

    def test_ci_verifies_built_artifact(self):
        workflow = (self.root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip wheel", workflow)
        self.assertIn("pip install", workflow)
        self.assertIn("find-links", workflow)
        self.assertIn("import geiter", workflow)
