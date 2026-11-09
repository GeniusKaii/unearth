import io
import os
import unittest

from unearth import fixtures
from unearth.device import Source
from unearth.readers import Reader
from unearth.formats import docs, images, video
from unearth.formats.exif import extract_jpeg_thumbnail, parse_tiff
from unearth.formats.registry import SIGNATURES, signatures_for


class MemSource(Source):
    """A Source over an in-memory blob."""

    def __init__(self, data: bytes):
        self._data = data
        self.spec = "mem"
        self.label = "mem"
        self.size = len(data)
        self.sector_size = 512
        self.bytes_read = 0
        self.read_calls = 0
        self.readonly = True

    def read_at(self, off: int, length: int) -> bytes:
        return self._data[off: off + length]


def carve(blob: bytes, fmt: str):
    src = MemSource(blob)
    reader = Reader(src)
    for sig in SIGNATURES:
        if sig.fmt != fmt:
            continue
        res = sig.carve(reader, 0)
        if res is not None:
            return res
    return None


class TestImages(unittest.TestCase):
    def test_jpeg_exact_size(self):
        blob = fixtures.sample("sample.jpg")
        res = carve(blob, "jpeg")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertTrue(res.complete)
        self.assertEqual((res.meta["width"], res.meta["height"]), (480, 320))

    def test_jpeg_with_exif_metadata(self):
        res = carve(fixtures.jpeg_with_exif(), "jpeg")
        self.assertEqual(res.meta["make"], "Canon")
        self.assertEqual(res.meta["model"], "Canon EOS 5D Mark III")
        self.assertTrue(res.meta["captured"].startswith("2013-08-12T19:22:33"))
        self.assertAlmostEqual(res.meta["gps_lat"], 31.1958, places=3)
        self.assertAlmostEqual(res.meta["gps_lon"], 121.4367, places=3)

    def test_jpeg_truncated_is_labelled(self):
        blob = fixtures.sample("sample.jpg")
        cut = blob[:-2]                       # drop the EOI marker
        data = cut + fixtures.jpeg_with_exif()
        res = carve(data, "jpeg")
        self.assertIsNotNone(res)
        self.assertFalse(res.complete)
        self.assertEqual(res.size, len(cut))   # stops at the next SOI
        self.assertIn("truncated", " ".join(res.warnings))

    def test_png(self):
        blob = fixtures.make_png(40, 30)
        res = carve(blob, "png")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertEqual((res.meta["width"], res.meta["height"]), (40, 30))

    def test_gif(self):
        blob = fixtures.make_gif()
        res = carve(blob, "gif")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertEqual(res.meta["width"], 2)

    def test_bmp(self):
        blob = fixtures.make_bmp()
        res = carve(blob, "bmp")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))

    def test_tiff_uses_strip_geometry(self):
        blob = fixtures.make_tiff(32, 32)
        res = carve(blob, "tiff")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertEqual(res.meta["raw_ext"], "tif")

    def test_random_noise_has_no_jpeg(self):
        import random
        rng = random.Random(3)
        noise = bytes(rng.getrandbits(8) for _ in range(4096))
        res = carve(b"\xff\xd8\xff" + noise, "jpeg")
        self.assertIsNone(res)


class TestVideo(unittest.TestCase):
    def test_mp4(self):
        blob = fixtures.sample("sample.mp4")
        res = carve(blob, "isobmff")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertTrue(res.complete)
        self.assertEqual((res.meta["width"], res.meta["height"]), (320, 240))
        self.assertAlmostEqual(res.meta["duration"], 1.0, places=1)
        self.assertEqual(res.meta["ext"], "mp4")

    def test_mp4_without_index_is_partial(self):
        import struct
        blob = fixtures.sample("sample.mp4")
        ftyp_size = struct.unpack_from(">I", blob, 0)[0]
        ftyp = blob[:ftyp_size]
        payload = b"\x00" * 4096
        mdat = struct.pack(">I", 8 + len(payload)) + b"mdat" + payload
        res = carve(ftyp + mdat, "isobmff")
        self.assertIsNotNone(res)
        self.assertFalse(res.complete)
        self.assertTrue(any("moov" in w for w in res.warnings))


class TestDocs(unittest.TestCase):
    def test_docx(self):
        blob = fixtures.make_docx()
        res = carve(blob, "zip")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertEqual(res.meta["ext"], "docx")
        self.assertEqual(res.meta["kind"], "doc")

    def test_pdf(self):
        blob = fixtures.make_pdf()
        res = carve(blob, "pdf")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))

    def test_pdf_incremental_save_uses_last_revision(self):
        """A save-updated PDF has two %%EOF markers; we must measure to the last."""
        blob = fixtures.make_pdf_updated()
        res = carve(blob, "pdf")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        # sanity: the fixture really is an incremental save
        self.assertEqual(blob.count(b"%%EOF"), 2)
        self.assertGreater(res.size, blob.find(b"%%EOF") + 5)

    def test_pdf_stops_before_next_document(self):
        first = fixtures.make_pdf()
        second = fixtures.make_pdf("another doc")
        res = carve(first + second, "pdf")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(first))

    def test_ole(self):
        blob = fixtures.make_ole()
        res = carve(blob, "ole")
        self.assertIsNotNone(res)
        self.assertEqual(res.size, len(blob))
        self.assertEqual(res.meta["ext"], "doc")

    def test_plain_zip_is_zip(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.txt", "hello")
        res = carve(buf.getvalue(), "zip")
        self.assertIsNotNone(res)
        self.assertEqual(res.meta["ext"], "zip")


class TestExif(unittest.TestCase):
    def test_parse_and_roundtrip(self):
        tiff = fixtures.build_exif(width=1234, height=567)
        meta, end = parse_tiff(tiff)
        self.assertEqual(meta["width"], 1234)
        self.assertEqual(meta["height"], 567)
        self.assertEqual(meta["make"], "Canon")

    def test_garbage_is_safe(self):
        self.assertEqual(parse_tiff(b"not a tiff at all"), ({}, 0))
        self.assertEqual(parse_tiff(b""), ({}, 0))

    def test_thumbnail_missing_is_none(self):
        self.assertIsNone(extract_jpeg_thumbnail(fixtures.sample("sample.jpg")))


class TestRegistry(unittest.TestCase):
    def test_profiles(self):
        self.assertTrue(signatures_for("media"))
        self.assertTrue(signatures_for("docs"))
        self.assertGreater(len(signatures_for("all")), len(signatures_for("docs")))
        with self.assertRaises(ValueError):
            signatures_for("nope")

    def test_magic_lengths(self):
        for sig in SIGNATURES:
            self.assertGreaterEqual(sig.magic_at, 0)
            self.assertLessEqual(len(sig.magic), 8)


if __name__ == "__main__":
    unittest.main()
