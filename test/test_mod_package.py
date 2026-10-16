import json
import unittest
from pathlib import Path


class ModPackageTests(unittest.TestCase):
    def setUp(self):
        self.mod_root = Path(__file__).resolve().parents[1] / "mod"
        packages = [path for path in self.mod_root.glob("factorio_ai_mcp_*") if path.is_dir()]
        self.assertEqual(len(packages), 1)
        self.package = packages[0]
        self.info = json.loads((self.package / "info.json").read_text(encoding="utf-8"))

    def test_package_directory_matches_metadata(self):
        self.assertEqual(self.package.name, f"{self.info['name']}_{self.info['version']}")

    def test_info_metadata(self):
        self.assertEqual(self.info["name"], "factorio_ai_mcp")
        self.assertEqual(self.info["factorio_version"], "2.0")
        self.assertRegex(self.info["version"], r"^\d+\.\d+\.\d+$")

    def test_mod_is_enabled(self):
        mod_list = json.loads((self.mod_root / "mod-list.json").read_text(encoding="utf-8"))
        self.assertIn({"name": "factorio_ai_mcp", "enabled": True}, mod_list["mods"])

    def test_control_lua_exists_and_is_nonempty(self):
        control = self.package / "control.lua"
        self.assertTrue(control.is_file())
        self.assertTrue(control.read_bytes())


if __name__ == "__main__":
    unittest.main()
