"""Upload sniffing: the image type and size come from the bytes, never from a name."""

from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from automotive import site_media
from automotive.site_media import MAX_BYTES, MAX_SIDE, describe, packaged_gallery, sniff

SVG = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
       b"<script>alert(1)</script></svg>")
GIF = b"GIF89a" + struct.pack("<HH", 10, 10) + b"\x80\x00\x00" + b"\x00" * 32


def png(width: int = 1, height: int = 1, *, pixels: bool = True) -> bytes:
    """A real PNG: signature, IHDR, one zlib stream of RGB rows, IEND, with valid CRCs.
    ``pixels=False`` keeps only the header, for claimed sizes too large to build."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    body = b""
    if pixels:
        rows = b"".join(b"\x00" + b"\xd1\x1f\x2a" * width for _ in range(height))
        body = chunk(b"IDAT", zlib.compress(rows))
    return b"\x89PNG\r\n\x1a\n" + header + body + chunk(b"IEND", b"")


def jpeg(width: int = 1, height: int = 1, *, frame: int = 0xC0, tables_first: bool = True,
         ) -> bytes:
    """A JPEG container: SOI, JFIF, quantisation and Huffman tables, frame, scan, EOI."""
    def segment(marker: int, payload: bytes) -> bytes:
        return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload

    tables = segment(0xDB, b"\x00" + bytes([1] * 64)) + segment(0xC4, b"\x00" + bytes(17))
    start_of_frame = segment(frame, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + (tables + start_of_frame if tables_first else start_of_frame + tables)
            + segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x7f\xa0" + b"\xff\xd9")


def _riff(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossy(width: int = 1, height: int = 1) -> bytes:
    """Simple lossy WebP: a VP8 key frame header carrying 14-bit dimensions."""
    frame = b"\x10\x02\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return _riff(b"VP8 ", frame + b"\x00" * 8)


def webp_lossless(width: int = 1, height: int = 1) -> bytes:
    """Lossless WebP: signature 0x2f, then width-1 and height-1 packed in 14 bits each."""
    bits = (width - 1) | ((height - 1) << 14)
    return _riff(b"VP8L", b"\x2f" + struct.pack("<I", bits) + b"\x00" * 8)


def webp_extended(width: int = 1, height: int = 1) -> bytes:
    """Extended WebP: flags, reserved bytes, then 24-bit canvas width-1 and height-1."""
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X", b"\x10\x00\x00\x00" + canvas) + b"\x00" * 8


def padded(image: bytes, size: int) -> bytes:
    return image + b"\x00" * (size - len(image))


class SniffTests(unittest.TestCase):
    def test_png_jpeg_and_the_three_webp_layouts_are_recognised_with_their_size(self):
        images = {
            "png": (png(3, 2), "image/png"),
            "jpeg baseline": (jpeg(3, 2), "image/jpeg"),
            "jpeg progressive": (jpeg(3, 2, frame=0xC2), "image/jpeg"),
            "jpeg, frame before tables": (jpeg(3, 2, tables_first=False), "image/jpeg"),
            "webp lossy": (webp_lossy(3, 2), "image/webp"),
            "webp lossless": (webp_lossless(3, 2), "image/webp"),
            "webp extended": (webp_extended(3, 2), "image/webp"),
        }
        for name, (data, content_type) in images.items():
            with self.subTest(name):
                self.assertEqual(sniff(data), (content_type, 3, 2))
                self.assertEqual(sniff(bytearray(data)), (content_type, 3, 2))

    def test_width_and_height_are_not_swapped(self):
        for build in (png, jpeg, webp_lossy, webp_lossless, webp_extended):
            data = build(640, 480) if build is not png else png(640, 480, pixels=False)
            self.assertEqual(sniff(data)[1:], (640, 480), build.__name__)

    def test_a_jpeg_table_segment_is_not_mistaken_for_the_frame(self):
        # The Huffman table marker 0xC4 sits in the same range as the frame markers.
        self.assertEqual(sniff(jpeg(300, 200)), ("image/jpeg", 300, 200))

    def test_svg_gif_and_other_content_are_refused(self):
        refused = {
            "svg": SVG, "gif": GIF, "empty": b"", "html": b"<!doctype html><script>x</script>",
            "text": b"just some text that is long enough to look at", "pdf": b"%PDF-1.7\n" * 8,
            "riff that is not webp": b"RIFF" + struct.pack("<I", 40) + b"WAVEfmt " + bytes(40),
            "webp with an unknown chunk": _riff(b"ALPH", bytes(24)),
            "webp lossy without its start code": _riff(b"VP8 ", bytes(24)),
            "webp lossless without its signature": _riff(b"VP8L", bytes(24)),
            "png signature inside other bytes": b"x" + png(),
        }
        for name, data in refused.items():
            with self.subTest(name):
                self.assertIsNone(sniff(data))
                self.assertIsNone(describe(data))

    def test_files_cut_off_before_their_dimensions_are_refused(self):
        whole = {"png": png(), "jpeg": jpeg(), "webp lossy": webp_lossy(),
                 "webp lossless": webp_lossless(), "webp extended": webp_extended()}
        cut_at = {"png": (4, 8, 16, 23), "jpeg": (2, 3, 4, 20, 90),
                  "webp lossy": (8, 12, 20, 29), "webp lossless": (12, 24, 29),
                  "webp extended": (16, 29)}
        for name, data in whole.items():
            for length in cut_at[name]:
                with self.subTest(name, length=length):
                    self.assertIsNone(sniff(data[:length]))

    def test_size_limits_in_bytes(self):
        self.assertEqual(sniff(padded(png(), MAX_BYTES)), ("image/png", 1, 1))
        self.assertIsNone(sniff(padded(png(), MAX_BYTES + 1)))
        self.assertIsNone(describe(padded(jpeg(), MAX_BYTES + 1)))
        # The limit is the CHECK on garage.media.data.
        self.assertEqual(MAX_BYTES, 1_572_864)

    def test_size_limits_in_pixels(self):
        self.assertEqual(MAX_SIDE, 4096)
        for build in (lambda w, h: png(w, h, pixels=False), jpeg, webp_lossy, webp_lossless,
                      webp_extended):
            self.assertIsNotNone(sniff(build(MAX_SIDE, MAX_SIDE)))
            self.assertIsNone(sniff(build(MAX_SIDE + 1, 10)))
            self.assertIsNone(sniff(build(10, MAX_SIDE + 1)))
        self.assertIsNone(sniff(png(0, 10, pixels=False)))
        self.assertIsNone(sniff(jpeg(10, 0)))
        self.assertIsNone(sniff(webp_lossy(0, 10)))

    def test_only_bytes_are_examined(self):
        for value in (None, "\x89PNG\r\n\x1a\n", 5, [137, 80, 78, 71], memoryview(png())):
            self.assertIsNone(sniff(value))

    def test_describe_returns_the_stored_fields(self):
        data = webp_lossless(960, 1280)
        self.assertEqual(describe(data), {
            "sha256": hashlib.sha256(data).hexdigest(), "content_type": "image/webp",
            "data": data, "width": 960, "height": 1280})
        described = describe(bytearray(png(2, 2)))
        self.assertIsInstance(described["data"], bytes)
        self.assertEqual(described["sha256"], hashlib.sha256(png(2, 2)).hexdigest())


class PackagedGalleryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.gallery = self.root / "gallery"
        self.gallery.mkdir()

    def manifest(self, entries) -> None:
        (self.gallery / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")

    def test_no_manifest_means_no_photos(self):
        (self.gallery / "photo.png").write_bytes(png())
        self.assertEqual(packaged_gallery(self.gallery), [])
        self.assertEqual(packaged_gallery(self.root / "missing"), [])

    def test_manifest_entries_become_rows_ready_to_store(self):
        first, second = png(4, 3), webp_lossy(8, 6)
        (self.gallery / "wall.png").write_bytes(first)
        (self.gallery / "lift.webp").write_bytes(second)
        self.manifest([
            {"file": "wall.png", "alt_ka": "კედელი", "alt_en": "The wall", "published": True},
            {"file": "lift.webp", "alt_en": "x" * 300}])
        photos = packaged_gallery(self.gallery)
        self.assertEqual(photos[0], {
            "sha256": hashlib.sha256(first).hexdigest(), "content_type": "image/png",
            "data": first, "width": 4, "height": 3, "alt_ka": "კედელი", "alt_en": "The wall",
            "published": True})
        self.assertEqual((photos[1]["content_type"], photos[1]["width"], photos[1]["height"]),
                         ("image/webp", 8, 6))
        # Unlisted values are safe defaults: unpublished, no alt text, bounded length.
        self.assertEqual((photos[1]["published"], photos[1]["alt_ka"], len(photos[1]["alt_en"])),
                         (False, "", 160))
        # The rows are exactly what SiteRepository.add_media accepts.
        accepted = {"sha256", "content_type", "data", "width", "height", "alt_ka", "alt_en",
                    "published"}
        self.assertTrue(all(set(photo) == accepted for photo in photos))

    def test_files_that_are_missing_or_not_images_are_left_out(self):
        (self.gallery / "real.png").write_bytes(png())
        (self.gallery / "drawing.png").write_bytes(SVG)  # an SVG under an image name
        (self.gallery / "cut.jpg").write_bytes(jpeg()[:12])
        (self.gallery / "huge.png").write_bytes(padded(png(), MAX_BYTES + 1))
        self.manifest([{"file": "missing.png"}, {"file": "drawing.png"}, {"file": "cut.jpg"},
                       {"file": "huge.png"}, {"file": ""}, {}, {"file": "real.png"}])
        self.assertEqual([photo["sha256"] for photo in packaged_gallery(self.gallery)],
                         [hashlib.sha256(png()).hexdigest()])

    def test_a_manifest_cannot_point_outside_its_directory(self):
        (self.root / "outside.png").write_bytes(png(9, 9))
        (self.gallery / "inside.png").write_bytes(png(2, 2))
        self.manifest([{"file": "../outside.png"}, {"file": str(self.root / "outside.png")},
                       {"file": "nested/../../outside.png"}, {"file": "sub/inside.png"}])
        photos = packaged_gallery(self.gallery)
        # Only the file name is used, so the last entry resolves inside the directory.
        self.assertEqual([(photo["width"], photo["height"]) for photo in photos], [(2, 2)])

    def test_the_packaged_photos_are_all_acceptable_images(self):
        manifest = site_media.GALLERY / "manifest.json"
        if not manifest.is_file():
            self.skipTest("no packaged gallery in this checkout")
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        photos = packaged_gallery()
        # Nothing listed is silently dropped, and nothing is stored twice.
        self.assertEqual(len(photos), len(entries))
        self.assertEqual(len({photo["sha256"] for photo in photos}), len(photos))
        for photo in photos:
            self.assertIn(photo["content_type"], {"image/jpeg", "image/png", "image/webp"})
        # Every photo is described in both languages, in full: a longer text would be cut
        # to fit the column without anyone noticing.
        for entry in entries:
            for key in ("alt_ka", "alt_en"):
                self.assertTrue(0 < len(entry.get(key, "")) <= 160, f"{entry['file']}: {key}")


if __name__ == "__main__":
    unittest.main()
