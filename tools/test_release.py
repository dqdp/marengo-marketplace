import json
import unittest
from release import NAME, ROOT, PAYLOAD, verify


class ReleaseTests(unittest.TestCase):
    def test_both_clients_get_same_router(self):
        lock = verify()
        copies = []
        for prefix, manifest, catalog in (("",".claude-plugin",".claude-plugin/marketplace.json"),
                                         ("chatgpt",".codex-plugin",".agents/plugins/marketplace.json")):
            base = ROOT/prefix
            entry, = json.loads((base/catalog).read_text(encoding="utf-8"))["plugins"]
            source = entry["source"] if prefix == "" else entry["source"]["path"]
            plugin = base/source
            self.assertEqual(plugin.name, NAME)
            self.assertEqual(json.loads((plugin/manifest/"plugin.json").read_text(encoding="utf-8"))["version"],lock["version"])
            skill = plugin/"skills/market-data"
            copies.append((skill/"SKILL.md").read_bytes())
            self.assertEqual({p.relative_to(skill).as_posix() for p in skill.rglob("*") if p.is_file()}, set(PAYLOAD))
            self.assertTrue((skill/"scripts/chart.py").is_file())
            self.assertIn("version: "+lock["version"]+"\n",copies[-1].decode())
        self.assertEqual(*copies)

    def test_chart_renders_in_each_package(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from xml.etree import ElementTree

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            candles = folder / "bars.csv"
            candles.write_text("date,open,high,low,close,volume\n2024-01-02,100,105,99,103,12\n")
            for prefix in ("", "chatgpt"):
                script = ROOT/prefix/"plugins"/NAME/"skills/market-data/scripts/chart.py"
                chart = folder/"price.svg"
                subprocess.run([sys.executable, str(script), "TEST", "--from", str(candles),
                                "--source-label", "Fixture UTC", "--out", str(chart)], check=True)
                labels = " ".join(ElementTree.parse(chart).getroot().itertext())
                self.assertIn("Fixture UTC", labels)
                self.assertIn("TEST", labels)
                chart.unlink()
