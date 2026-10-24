"""Tests for bilingual documentation parity and Markdown relative links."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent


def _load_tool(name: str):
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DocTests(unittest.TestCase):
    def test_bilingual_doc_parity(self):
        parity = _load_tool("check_doc_parity")
        errors = parity.check_all(ROOT)
        self.assertEqual(errors, [])

    def test_markdown_relative_links(self):
        links = _load_tool("check_links")
        documents = links.iter_documents(ROOT)
        self.assertGreaterEqual(len(documents), 8)
        broken: list[str] = []
        for doc in documents:
            for problem in links.check_document(doc, ROOT):
                broken.append(f"{doc.relative_to(ROOT)}: {problem}")
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
