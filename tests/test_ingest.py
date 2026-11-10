"""Ingest a directory, classify, and browse - the photo-digger scenario."""

import os
import unittest

from unearth import fixtures
from unearth.catalog import Catalog
from unearth.ingest import ingest_dir
from unearth.report import write_report
from unearth.triage import run_triage
from unearth.triage.classify import (CANDIDATE_CLASSES, SCREEN_RES,
                                     classify_photo, is_candidate)

from . import support


class TestClassify(unittest.TestCase):
    def test_camera_photo_needs_model_and_time(self):
        self.assertEqual(classify_photo("photo", 4000, 3000, "Canon",
                                        "EOS 5D", "2013-08-12T19:22:33", 3_000_000),
                         "camera_photo")

    def test_camera_model_alone_is_likely(self):
        self.assertEqual(classify_photo("photo", 4000, 3000, "Canon", "EOS 5D",
                                        None, 3_000_000), "likely_camera_photo")

    def test_time_alone_is_maybe(self):
        self.assertEqual(classify_photo("photo", 4000, 3000, None, None,
                                        "2013-08-12T19:22:33", 3_000_000),
                         "maybe_photo")

    def test_screen_resolution_without_exif_is_internet(self):
        self.assertIn((1920, 1080), SCREEN_RES)
        self.assertEqual(classify_photo("photo", 1920, 1080, None, None, None,
                                        2_000_000), "internet_screendim")

    def test_wallpaper_ratio(self):
        self.assertEqual(classify_photo("photo", 3440, 1000, None, None, None,
                                        2_000_000), "internet_wallpaper")

    def test_tiny_image(self):
        self.assertEqual(classify_photo("photo", 200, 150, None, None, None,
                                        90_000), "internet_small")

    def test_small_file(self):
        self.assertEqual(classify_photo("photo", 1000, 800, None, None, None,
                                        30_000), "internet_smallsize")

    def test_no_fingerprint_but_normal_size(self):
        self.assertEqual(classify_photo("photo", 1000, 800, None, None, None,
                                        900_000), "no_exif_unknown")

    def test_docs_are_not_photos(self):
        self.assertEqual(classify_photo("doc", 0, 0, None, None, None, 900_000),
                         "not_photo")

    def test_videos_are_never_junk(self):
        self.assertEqual(classify_photo("video", 1920, 1080, None, None, None,
                                        50_000_000), "maybe_photo")
        self.assertEqual(classify_photo("video", 1920, 1080, "Apple", "iPhone",
                                        "2016-11-17T14:23:45", 50_000_000),
                         "camera_photo")

    def test_exposure_params_count_as_fingerprint(self):
        self.assertEqual(classify_photo("photo", 4000, 3000, "NIKON", "D7000",
                                        "2014-01-01T00:00:00", 4_000_000,
                                        has_exposure_params=True), "camera_photo")

    def test_candidates(self):
        self.assertTrue(is_candidate("camera_photo"))
        self.assertFalse(is_candidate("internet_small"))


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.dir = support.make_workdir("ingest-")
        self.photos = os.path.join(self.dir, "recovered")
        os.makedirs(self.photos, exist_ok=True)
        # a camera photo
        with open(os.path.join(self.photos, "00001.jpg"), "wb") as fh:
            fh.write(fixtures.jpeg_with_exif())
        # a plain photo with no EXIF
        with open(os.path.join(self.photos, "00002.jpg"), "wb") as fh:
            fh.write(fixtures.sample("sample.jpg"))
        # a video
        with open(os.path.join(self.photos, "00003.mp4"), "wb") as fh:
            fh.write(fixtures.sample("sample.mp4"))
        # something we do not handle
        with open(os.path.join(self.photos, "00004.txt"), "w") as fh:
            fh.write("nope")
        # a subdirectory, to prove the walk is recursive
        sub = os.path.join(self.photos, "DCIM")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "00005.jpg"), "wb") as fh:
            fh.write(fixtures.jpeg_with_exif())

    def tearDown(self):
        support.clean(self.dir)

    def test_ingest_walks_and_parses(self):
        out = os.path.join(self.dir, "dig")
        cat = Catalog(os.path.join(out, "catalog.db"))
        try:
            src = cat.add_source(self.photos, "directory", "recovered")
            sid = cat.begin_scan(src, self.photos, "ingest", 0, 0, 0)
            stats = ingest_dir(self.photos, cat, sid)
            self.assertEqual(stats["ingested"], 4)          # 3 jpg + 1 mp4
            self.assertGreaterEqual(stats["skipped_ext"], 1)
            rows = list(cat.iter_files())
            self.assertEqual(len(rows), 4)
            # in-place: no copy was made, path points at the real file
            for row in rows:
                real = os.path.join(self.photos, row["path"])
                self.assertTrue(os.path.exists(real), real)
            jpeg = [r for r in rows if r["ext"] == "jpg" and r["captured"]][0]
            self.assertEqual(jpeg["make"], "Canon")
            self.assertEqual(jpeg["width"], 480)
            self.assertEqual(jpeg["exif_params"], 1)
            video = [r for r in rows if r["ext"] == "mp4"][0]
            self.assertEqual(video["kind"], "video")
            self.assertAlmostEqual(video["duration"], 1.0, places=1)
        finally:
            cat.close()

    def test_ingest_then_triage(self):
        out = os.path.join(self.dir, "dig2")
        cat = Catalog(os.path.join(out, "catalog.db"))
        try:
            src = cat.add_source(self.photos, "directory", "recovered")
            sid = cat.begin_scan(src, self.photos, "ingest", 0, 0, 0)
            ingest_dir(self.photos, cat, sid)
            cat.finish_scan(sid, "done")
            res = run_triage(out, cat)
            self.assertGreater(res["scored"], 0)
            counts = res["by_class"]
            self.assertGreaterEqual(counts.get("camera_photo", 0), 2)
            # a recovered clip is evidence of real life, not junk: it must not
            # be classified as not_photo just because EXIF is absent
            self.assertEqual(counts.get("not_photo", 0), 0)
            self.assertGreaterEqual(counts.get("maybe_photo", 0), 1)
            # the class column is populated, not stuffed into a note
            rows = list(cat.conn.execute("SELECT cls, note FROM file"))
            self.assertTrue(all(r["cls"] for r in rows))
            self.assertFalse(any("cls:" in (r["note"] or "") for r in rows))
            self.assertTrue(os.path.exists(res["album"]))
            report = write_report(out, cat)
            self.assertTrue(os.path.exists(report))
        finally:
            cat.close()


class TestMigration(unittest.TestCase):
    def test_old_catalogue_gains_new_columns(self):
        """A v0.1 catalogue (no cls column) must keep working."""
        import sqlite3
        d = support.make_workdir("mig-")
        try:
            path = os.path.join(d, "catalog.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE file (id INTEGER PRIMARY KEY, scan_id INTEGER,"
                " src TEXT, kind TEXT, fmt TEXT, ext TEXT, off INTEGER,"
                " size INTEGER, complete INTEGER DEFAULT 1, name TEXT,"
                " path TEXT, sha256 TEXT, width INTEGER, height INTEGER,"
                " duration REAL, captured TEXT, day TEXT, make TEXT,"
                " model TEXT, gps_lat REAL, gps_lon REAL, orientation INTEGER,"
                " score INTEGER DEFAULT 0, dup_group INTEGER DEFAULT 0,"
                " thumb TEXT, note TEXT, created TEXT);"
                "INSERT INTO file(name, path, kind) VALUES('a.jpg','a.jpg','photo');")
            conn.commit()
            conn.close()
            cat = Catalog(path)
            try:
                cols = {r[1] for r in cat.conn.execute("PRAGMA table_info(file)")}
                self.assertIn("cls", cols)
                self.assertIn("exif_params", cols)
                self.assertEqual(cat.count_files(), 1)   # data survived
            finally:
                cat.close()
        finally:
            support.clean(d)


if __name__ == "__main__":
    unittest.main()
