"""Unit tests for the Terminal Trove index builder.

The fixtures are small synthetic pages.  They mirror the markup the real
collector depends on so a site change fails here before it creates a broken
index.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_index.py"
FIXTURES = ROOT / "tests" / "fixtures"


def load_module():
    loader = importlib.util.spec_from_file_location("build_index", SCRIPT)
    assert loader is not None and loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    sys.modules["build_index"] = module
    loader.loader.exec_module(module)
    return module


class IndexBuilder(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tool_html = (FIXTURES / "tool.html").read_text(encoding="utf-8")
        self.list_html = (FIXTURES / "list.html").read_text(encoding="utf-8")

    def test_apt_package_from_command(self):
        self.assertEqual(self.mod.apt_package("apt install tmux"), "tmux")
        self.assertEqual(self.mod.apt_package("sudo apt-get install -y bat"), "bat")
        self.assertEqual(self.mod.apt_package("sudo -E apt install bat"), "bat")
        self.assertEqual(
            self.mod.apt_package("DEBIAN_FRONTEND=noninteractive apt install bat"),
            "bat",
        )
        self.assertEqual(self.mod.apt_package("apt install -y --no-install-recommends htop"), "htop")
        self.assertEqual(self.mod.apt_package("apt install rust:arm64"), "rust")
        self.assertEqual(self.mod.apt_package("apt install ./local.deb"), "")
        self.assertEqual(self.mod.apt_package("snap install foo"), "")
        self.assertEqual(self.mod.apt_package("apt install -t trixie foo"), "foo")

    def test_list_parsing(self):
        entries = self.mod.parse_list(self.list_html)
        self.assertEqual([entry["slug"] for entry in entries], ["tmux", "lazygit"])
        self.assertEqual(entries[0]["name"], "tmux")
        self.assertEqual(entries[1]["summary"], "Simple terminal UI for git commands.")

    def test_tool_parsing(self):
        tool = self.mod.parse_tool(
            "tmux", self.tool_html, {"slug": "tmux", "name": "tmux", "summary": ""}
        )
        self.assertEqual(tool["name"], "tmux")
        self.assertEqual(tool["summary"], "An open-source terminal multiplexer.")
        self.assertEqual(tool["language"], "c")
        self.assertEqual(tool["language_label"], "C")
        self.assertEqual(tool["license"], "isc")
        self.assertIn("cli", tool["categories"])
        self.assertIn("tui", tool["categories"])
        self.assertNotIn("linux", tool["categories"])
        self.assertIn("linux", tool["platforms"])
        self.assertEqual(tool["apt"], {"command": "apt install tmux", "package": "tmux"})
        self.assertEqual(tool["stars"], 50378)
        self.assertEqual(tool["release"], "3.8")
        self.assertIn("scriptable", tool["description"])

    def test_install_without_apt_has_no_apt_entry(self):
        page = self.tool_html.replace(
            "&quot;ubuntu / debian&quot;:&quot;apt install tmux&quot;,",
            "",
        )
        tool = self.mod.parse_tool("tmux", page, {"name": "tmux", "summary": ""})
        self.assertEqual(tool["apt"], {})

    def test_install_apt_accepts_top_level_debian(self):
        self.assertEqual(
            self.mod.install_apt({"debian": "apt install foo"}),
            {"command": "apt install foo", "package": "foo"},
        )

    def test_labels(self):
        self.assertEqual(self.mod.category_label("tui"), "TUI")
        self.assertEqual(self.mod.category_label("text-editors"), "Text Editors")
        self.assertEqual(self.mod.language_label("cpp"), "C++")
        self.assertEqual(self.mod.language_label("javascript"), "JavaScript")


if __name__ == "__main__":
    unittest.main()
