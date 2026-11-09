import unittest

from warehouse_sync.integrity import verify_version_integrity_tag, version_integrity_tag


class VersionIntegrityTests(unittest.TestCase):
    def test_tag_is_deterministic_and_detects_changed_source_data(self) -> None:
        fields = {
            "source_product_id": "42",
            "observed_run_id": 7,
            "source_page": 1,
            "source_columns": {"column_01": "A"},
            "row_hash": "a" * 64,
        }
        tag = version_integrity_tag(b"k" * 32, **fields)
        self.assertTrue(verify_version_integrity_tag(b"k" * 32, tag, **fields))
        self.assertFalse(
            verify_version_integrity_tag(
                b"k" * 32,
                tag,
                **{**fields, "source_columns": {"column_01": "changed"}},
            )
        )
