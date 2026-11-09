import os
import unittest

from unearth import fixtures
from unearth.device import FileSource
from unearth.fs.ntfs import Ntfs, NotNtfs, extract

from . import support


class TestNtfs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = support.make_workdir("ntfs-")
        cls.path = os.path.join(cls.dir, "ntfs.img")
        with open(cls.path, "wb") as fh:
            fh.write(fixtures.make_ntfs_image())
        cls.src = FileSource(cls.path)
        cls.ntfs = Ntfs(cls.src)

    @classmethod
    def tearDownClass(cls):
        cls.src.close()
        support.clean(cls.dir)

    def test_geometry(self):
        self.assertEqual(self.ntfs.cluster_size, 4096)
        self.assertEqual(self.ntfs.record_size, 1024)
        self.assertEqual(self.ntfs.mft_offset, 4 * 4096)

    def test_record_count_sane(self):
        self.assertGreater(self.ntfs.record_count, 0)
        self.assertLess(self.ntfs.record_count, 10000)

    def test_deleted_names_and_dates(self):
        recs = list(self.ntfs.deleted_files())
        names = [r.name for r in recs]
        self.assertIn("IMG_1234.JPG", names)
        self.assertIn("游记草稿.txt", names)
        photo = [r for r in recs if r.name == "IMG_1234.JPG"][0]
        self.assertFalse(photo.in_use)
        self.assertEqual(photo.created.year, 2013)
        self.assertEqual(photo.created.month, 8)
        self.assertEqual(photo.size, len(fixtures.jpeg_with_exif()))

    def test_parent_path(self):
        photo = [r for r in self.ntfs.deleted_files() if r.name == "IMG_1234.JPG"][0]
        self.assertEqual(self.ntfs.rel_path(photo), "DCIM")

    def test_extract_bytes_identical(self):
        out = os.path.join(self.dir, "out")
        os.makedirs(out, exist_ok=True)
        photo = [r for r in self.ntfs.deleted_files() if r.name == "IMG_1234.JPG"][0]
        path, written, complete = extract(self.src, self.ntfs, photo, out)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(complete)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), fixtures.jpeg_with_exif())

    def test_live_extents(self):
        extents = self.ntfs.live_extents()
        self.assertTrue(extents)
        mft_start = self.ntfs.mft_offset
        self.assertTrue(any(a <= mft_start < b for a, b in extents))

    def test_not_ntfs(self):
        bad = os.path.join(self.dir, "bad.img")
        with open(bad, "wb") as fh:
            fh.write(b"\x00" * 4096)
        src = FileSource(bad)
        try:
            with self.assertRaises(NotNtfs):
                Ntfs(src)
        finally:
            src.close()


if __name__ == "__main__":
    unittest.main()
