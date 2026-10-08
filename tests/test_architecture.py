"""Module không được gọi chéo nhau: chỉ orchestrator được import module khác (qua contract + tiêm phụ thuộc)."""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "contentfactory"
SHARED = {"contracts", "fsutil", "media"}                   # media: tiện ích file thuần (Image Pool), chỉ phụ thuộc contracts
ISOLATED = {"source", "story", "tts", "audio", "render", "publish", "output", "adapters", "jobs", "universe", "story_remix"}


def imported_packages(source: str, file_pkg: str) -> set[str]:
    """Tập package con của contentfactory mà file import (rỗng nếu chỉ import stdlib/third-party)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                out.add(file_pkg)
            elif node.level == 2:
                out.add((node.module or "").split(".")[0] or "?")
            elif node.level == 0 and (node.module or "").startswith("contentfactory."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("contentfactory."):
                    out.add(a.name.split(".")[1])
    return out


class ArchitectureTest(unittest.TestCase):
    def test_isolated_packages_import_only_contracts_and_themselves(self):
        bad = []
        for pkg in ISOLATED:
            for f in (SRC / pkg).rglob("*.py"):
                extra = imported_packages(f.read_text(encoding="utf-8"), pkg) - SHARED - {pkg}
                if extra:
                    bad.append(f"{f.relative_to(SRC)} -> {sorted(extra)}")
        self.assertEqual(bad, [], "module gọi chéo nhau trực tiếp")

    def test_checker_detects_cross_import(self):
        self.assertEqual(imported_packages("from ..audio import stage", "tts"), {"audio"})
        self.assertEqual(imported_packages("import contentfactory.render.x", "tts"), {"render"})
        self.assertEqual(imported_packages("from .x import y\nimport os", "tts"), {"tts"})


if __name__ == "__main__":
    unittest.main()
