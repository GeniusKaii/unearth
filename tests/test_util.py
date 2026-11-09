import unittest

from unearth import util


class TestUtil(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(util.human_bytes(0), "0 B")
        self.assertEqual(util.human_bytes(1024), "1.00 KiB")
        self.assertEqual(util.human_bytes(1536), "1.50 KiB")
        self.assertEqual(util.human_bytes(None), "-")

    def test_parse_size(self):
        self.assertEqual(util.parse_size("16M"), 16 * 1024 * 1024)
        self.assertEqual(util.parse_size("512k"), 512 * 1024)
        self.assertEqual(util.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(util.parse_size(4096), 4096)
        with self.assertRaises(ValueError):
            util.parse_size("lots")

    def test_safe_filename(self):
        self.assertEqual(util.safe_filename("a/b\\c.txt"), "c.txt")
        self.assertEqual(util.safe_filename('bad:name?.jpg'), "bad_name_.jpg")
        self.assertEqual(util.safe_filename("con.txt"), "con_.txt")
        self.assertEqual(util.safe_filename(""), "recovered")
        self.assertLessEqual(len(util.safe_filename("x" * 500 + ".jpg")), 96)

    def test_day_key(self):
        import datetime as dt
        self.assertEqual(util.day_key(dt.datetime(2013, 8, 12, 19, 22)), "2013-08-12")
        self.assertEqual(util.day_key(None), "")

    def test_unique_path(self):
        import os
        from . import support
        d = support.make_workdir("uniq-")
        try:
            first = util.unique_path(d, "a.jpg")
            open(first, "wb").close()
            second = util.unique_path(d, "a.jpg")
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("a~1.jpg"))
        finally:
            support.clean(d)


if __name__ == "__main__":
    unittest.main()
