import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import check_dependency_freshness as fresh  # noqa: E402


class DependencyFreshnessTests(unittest.TestCase):
    def test_build_requirements_are_read(self):
        text = '[project]\nname = "x"\n\n[build-system]\nrequires = ["setuptools>=68", "wheel"]\n'
        rows = fresh.load_build_requirements(text, "pyproject.toml")
        self.assertEqual([(r["name"], r["minimum"]) for r in rows], [("setuptools", "68"), ("wheel", "")])

    def test_hold_comment_is_kept(self):
        text = '[build-system]\nrequires = ["setuptools>=68"]  # freshness-hold: backend only\n'
        (row,) = fresh.load_build_requirements(text, "pyproject.toml")
        self.assertEqual(row["hold"], "backend only")

    def test_other_sections_are_ignored(self):
        text = '[project]\nrequires-python = ">=3.10"\n[tool.x]\nrequires = ["nope>=1"]\n'
        self.assertEqual(fresh.load_build_requirements(text, "pyproject.toml"), [])

    def test_action_pin_and_hold(self):
        text = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        (row,) = fresh.parse_workflow_actions(text, "ci.yml")
        self.assertEqual((row["name"], row["minimum"]), ("actions/checkout", "7.0.1"))

    def test_aged_floor_needs_review_unless_held(self):
        rows = fresh.collect_status(
            [{"name": "p", "minimum": "1", "requirement": "p>=1", "source": "s", "hold": "", "kind": "pypi"},
             {"name": "q", "minimum": "1", "requirement": "q>=1", "source": "s", "hold": "why", "kind": "pypi"}],
            lambda name: "2.0", {})
        self.assertTrue(fresh.needs_review(rows[0]))
        self.assertFalse(fresh.needs_review(rows[1]))

    def test_repo_declarations_parse(self):
        self.assertTrue(fresh.load_direct_dependencies())
        self.assertTrue(fresh.load_workflow_actions())


if __name__ == "__main__":
    unittest.main()
