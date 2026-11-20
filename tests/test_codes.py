"""Part numbers as customers type them: spaced, with an index letter, or with Cyrillic letters
that look like Latin ones (automotive/codes.py, assistant_tools.typed_tokens and the catalogue's
FMSI stems). Only exact catalogue matches are ever used, so these are lookups, not guesses."""

from __future__ import annotations

import time
import unittest

from automotive.assistant_catalog import _FMSI_PLATE
from automotive.assistant_tools import typed_tokens
from automotive.codes import codes_in_text, latin, normalize_code, parse_codes


class CodeTests(unittest.TestCase):
    def test_a_long_run_of_spaces_is_split_in_linear_time(self):
        # A customer can send one (CodeQL py/polynomial-redos): it took 7 s for 100,000.
        for text in ("A1" + " " * 100_000 + "B2", "A1" + " " * 100_000 + "/x B2",
                     "A1" + " \n" * 50_000 + " / B2"):
            started = time.perf_counter()
            codes = parse_codes(text)
            self.assertLess(time.perf_counter() - started, 0.5)
            self.assertEqual((codes[0].normalized, codes[-1].normalized), ("A1", "B2"))
        # Spaced slashes still separate codes, and a compact one stays inside its code.
        self.assertEqual([code.original for code in parse_codes("AP104/2  /  W712, 1K0 / 2")],
                         ["AP104/2", "W712", "1K0", "2"])
        # A run with a line break in it before the slash is one separator, as the regex was.
        self.assertEqual([code.original for code in parse_codes("03 115 \r / 562")],
                         ["03 115", "562"])

    def test_cyrillic_look_alikes_are_the_latin_letters(self):
        self.assertEqual(latin("1К0698151А"), "1K0698151A")
        self.assertEqual(normalize_code("1к0 698 151 а"), "1K0698151A")
        self.assertEqual(normalize_code("НU 719/7Х"), "HU719/7X")
        self.assertEqual(normalize_code("AP104/2"), "AP104/2")  # Latin stays as it was
        self.assertEqual(latin("ზეთის ფილტრი"), "ზეთის ფილტრი")  # Georgian is untouched

    def test_spaced_numbers_are_tried_joined(self):
        self.assertEqual(codes_in_text("W 712/75 გაქვთ?"), ["W712/75", "712/75"])
        self.assertEqual(codes_in_text("1K0 698 151 A"), ["1K0698151", "1K0698151A"])
        self.assertEqual(codes_in_text("ფილტრი 1К0698151А"), ["1K0698151A"])
        self.assertEqual(codes_in_text("A 000 420 30 20"), ["A0004203020"])
        self.assertEqual(codes_in_text("0 986 494 520"), ["0986494520"])
        self.assertEqual(codes_in_text("P 50 069"), ["P50069"])
        self.assertEqual(codes_in_text("GDB1916 და 03L 115 562"), ["GDB1916", "03L115562"])

    def test_years_and_short_words_are_not_numbers(self):
        self.assertEqual(codes_in_text("Camry 2015-2017"), [])
        self.assertEqual(codes_in_text("my car is from 2014"), [])
        self.assertEqual(codes_in_text("Golf ბენზინი 1.6"), [])

    def test_typed_tokens_read_cyrillic_look_alikes_as_latin(self):
        self.assertEqual(typed_tokens("ფილტრი 1К0698151А და W712/75"), ["1K0698151A", "W712/75"])

    def test_an_fmsi_plate_stem_is_read_whatever_dash_the_cell_uses(self):
        for written in ("D1107-8212", "D1107–8212", "D1107−8212", "d1107-8212a"):
            self.assertEqual(_FMSI_PLATE.fullmatch(written).group(1).upper(), "D1107", written)
        self.assertIsNone(_FMSI_PLATE.fullmatch("D1107 8212"))  # two cells, each its own code


if __name__ == "__main__":
    unittest.main()
