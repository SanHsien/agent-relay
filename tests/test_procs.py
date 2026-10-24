"""Console programs relay starts must not open windows (the scheduled watch has no console)."""

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Never touch the real ~/.agent-relay: every test run gets its own home.
os.environ["AGENT_RELAY_HOME"] = tempfile.mkdtemp(prefix="agent-relay-test-home-")

from agent_relay import procs  # noqa: E402


class NoWindowTests(unittest.TestCase):
    def test_run_adds_create_no_window_on_windows(self):
        with mock.patch.object(procs, "CREATE_NO_WINDOW", 0x08000000), \
                mock.patch.object(procs.subprocess, "run") as run:
            procs.run(["git", "status"], capture_output=True)
            procs.run(["x"], creationflags=0x00000200)
        self.assertEqual(run.call_args_list[0].kwargs["creationflags"], 0x08000000)
        self.assertEqual(run.call_args_list[1].kwargs["creationflags"], 0x08000200)  # kept the caller's flag
        with mock.patch.object(procs, "CREATE_NO_WINDOW", 0), mock.patch.object(procs.subprocess, "run") as run:
            procs.run(["git"])
        self.assertNotIn("creationflags", run.call_args.kwargs)

    def test_no_module_calls_subprocess_run_directly(self):
        offenders = []
        for path in (ROOT / "agent_relay").glob("*.py"):
            if path.name == "procs.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr == "run" and \
                        isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], "use procs.run so no console window opens")


if __name__ == "__main__":
    unittest.main()
