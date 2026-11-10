"""The ext4 image browser, tested against a hand-built image."""

import os
import unittest

from unearth import fixtures
from unearth.fs.ext4 import Ext4, Ext4Error

from . import support


class TestExt4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = support.make_workdir("ext4-")
        cls.path = os.path.join(cls.dir, "data.img")
        with open(cls.path, "wb") as fh:
            fh.write(fixtures.make_ext4_image())

    @classmethod
    def tearDownClass(cls):
        support.clean(cls.dir)

    def setUp(self):
        self.fs = Ext4(self.path)

    def tearDown(self):
        self.fs.close()

    def test_geometry(self):
        self.assertEqual(self.fs.bs, 1024)
        self.assertEqual(self.fs.inode_size, 128)
        self.assertEqual(self.fs.inodes_per_group, 32)
        self.assertTrue(self.fs.extents)
        self.assertTrue(self.fs.is64)

    def test_root_listing(self):
        names = [n for n, _i, _t in self.fs.listdir(2)]
        self.assertIn("data", names)

    def test_lookup_and_read(self):
        ino = self.fs.lookup("/data/hello.txt")
        self.assertEqual(ino, 12)
        self.assertEqual(self.fs.read_file(ino), b"unearth ext4 fixture\n")

    def test_missing_path(self):
        self.assertIsNone(self.fs.lookup("/data/nope.txt"))
        self.assertIsNone(self.fs.lookup("/nothing/here"))

    def test_tree_walk(self):
        found = [p for p, _i, _t in self.fs.walk("/", maxdepth=3)]
        self.assertIn("/data", found)
        self.assertIn("/data/hello.txt", found)

    def test_export_file(self):
        out = os.path.join(self.dir, "out.txt")
        ino = self.fs.lookup("/data/hello.txt")
        written, complete = self.fs.export_file(ino, out)
        self.assertTrue(complete)
        self.assertEqual(written, 21)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), b"unearth ext4 fixture\n")

    def test_not_ext4(self):
        bad = os.path.join(self.dir, "bad.img")
        with open(bad, "wb") as fh:
            fh.write(b"\x00" * 4096)
        with self.assertRaises(Ext4Error):
            Ext4(bad)


if __name__ == "__main__":
    unittest.main()
