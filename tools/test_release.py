import json
import unittest
from release import NAME, ROOT, runtime_member, verify


class ReleaseTests(unittest.TestCase):
    def test_both_clients_get_same_router(self):
        lock = verify()
        copies = []
        for prefix, manifest, catalog in (("",".claude-plugin",".claude-plugin/marketplace.json"),
                                         ("chatgpt",".codex-plugin",".agents/plugins/marketplace.json")):
            base = ROOT/prefix
            entry, = json.loads((base/catalog).read_text())["plugins"]
            source = entry["source"] if prefix == "" else entry["source"]["path"]
            plugin = base/source
            self.assertEqual(plugin.name, NAME)
            self.assertEqual(json.loads((plugin/manifest/"plugin.json").read_text())["version"],lock["version"])
            skill = plugin/"skills/market-data"
            copies.append((skill/"SKILL.md").read_bytes())
            self.assertEqual((skill/"market-data-host.json").exists(),prefix=="chatgpt")
            self.assertTrue((skill/"scripts/chart.py").is_file())
            self.assertIn("version: "+lock["version"]+"\n",copies[-1].decode())
        self.assertEqual(*copies)

    def test_only_runtime_package_members_are_selected(self):
        for file in ("market-data/.env","market-data/store.tgz","market-data/scripts/test_chart.py",
                     "market-data/store/options.sqlite","market-data/private/notes.md"):
            self.assertFalse(runtime_member(file))
        for invalid in ("../SKILL.md","market-data/../../.git/config","/etc/passwd"):
            with self.assertRaises(ValueError):
                runtime_member(invalid)
