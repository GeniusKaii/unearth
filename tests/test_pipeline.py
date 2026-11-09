import json
import os
import unittest

from unearth import fixtures
from unearth.album import build_album
from unearth.catalog import Catalog
from unearth.device import FileSource
from unearth.export import export_selection, load_picks
from unearth.report import write_report
from unearth.scanner import ScanOptions, scan_source
from unearth.triage import dup_primaries, run_triage

from . import support

MIB = 1024 * 1024


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = support.make_workdir("pipe-")
        cls.image = os.path.join(cls.dir, "demo.img")
        with open(cls.image, "wb") as fh:
            fh.write(fixtures.build_demo_image(8 * MIB))
        cls.out = os.path.join(cls.dir, "dig")
        cls.cat = Catalog(os.path.join(cls.out, "catalog.db"))
        src = FileSource(cls.image)
        source_id = cls.cat.add_source(src.spec, "image", src.label, src.size)
        cls.scan_id = cls.cat.begin_scan(source_id, src.spec, "all", 0, src.size, 4 * MIB)
        cls.stats = scan_source(src, cls.cat, cls.scan_id,
                                ScanOptions(profile="all", chunk=4 * MIB, out_dir=cls.out))
        cls.cat.finish_scan(cls.scan_id, "done")
        src.close()

    @classmethod
    def tearDownClass(cls):
        cls.cat.close()
        support.clean(cls.dir)

    def test_found_several_formats(self):
        self.assertGreaterEqual(self.stats["files"], 8)
        for fmt in ("jpeg", "isobmff", "zip", "pdf", "ole"):
            self.assertGreater(self.stats["by_fmt"].get(fmt, 0), 0, fmt)

    def test_files_written_and_hashed(self):
        row = self.cat.conn.execute("SELECT * FROM file LIMIT 1").fetchone()
        path = os.path.join(self.out, row["path"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.getsize(path), row["size"])
        self.assertEqual(len(row["sha256"]), 64)

    def test_triage_scores_and_dedups(self):
        res = run_triage(self.out, self.cat)
        self.assertGreater(res["scored"], 0)
        self.assertGreater(res["dupes"], 0)      # the demo repeats the same photos
        self.assertTrue(os.path.exists(res["album"]))
        scored = self.cat.conn.execute("SELECT COUNT(*) FROM file WHERE score>0").fetchone()[0]
        self.assertEqual(scored, res["scored"])
        primaries = dup_primaries(self.cat.conn)
        self.assertTrue(primaries)

    def test_album_payload(self):
        build_album(self.out, self.cat)
        with open(os.path.join(self.out, "album", "index.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(len(payload["items"]), self.stats["files"])
        item = payload["items"][0]
        for key in ("id", "k", "p", "sz"):
            self.assertIn(key, item)
        html = open(os.path.join(self.out, "album", "index.html"), encoding="utf-8").read()
        self.assertIn("拾遗", html)
        self.assertNotIn("/*__DATA__*/", html)
        self.assertIn('"items"', html)

    def test_report(self):
        path = write_report(self.out, self.cat)
        text = open(path, encoding="utf-8").read()
        self.assertIn("# 拾遗 unearth 报告", text)
        self.assertIn("按类型", text)

    def test_export_dedupes(self):
        dest = os.path.join(self.dir, "exported")
        stats = export_selection(self.out, self.cat, dest)
        self.assertGreater(stats["copied"], 0)
        self.assertLess(stats["copied"], self.stats["files"])
        self.assertGreater(stats["skipped_dup"], 0)
        files = [f for _, _, fs in os.walk(dest) for f in fs]
        self.assertEqual(len(files), stats["copied"])

    def test_export_picks_subset(self):
        ids = [r["id"] for r in self.cat.conn.execute("SELECT id FROM file LIMIT 2")]
        picks = os.path.join(self.dir, "picked.json")
        with open(picks, "w", encoding="utf-8") as fh:
            json.dump({"ids": ids, "count": len(ids)}, fh)
        self.assertEqual(load_picks(picks), ids)
        dest = os.path.join(self.dir, "picked")
        stats = export_selection(self.out, self.cat, dest, ids=ids)
        self.assertEqual(stats["copied"], len(ids))

    def test_provenance_survives(self):
        """Every recovered file must point back at a disk offset."""
        rows = list(self.cat.conn.execute("SELECT off, size, path FROM file"))
        for row in rows:
            self.assertIsNotNone(row["off"])
            self.assertGreater(row["size"], 0)


if __name__ == "__main__":
    unittest.main()
