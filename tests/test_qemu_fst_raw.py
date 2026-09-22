from __future__ import annotations

import gzip
import struct
import tempfile
import unittest
from pathlib import Path

from tools.qemu_fst.lower import _map_shards_to_cores


RAW_HEADER = struct.Struct("<QIIII24x")
RAW_MAGIC = 0x3257415244535446


class QemuFstRawTest(unittest.TestCase):
    def test_maps_canonical_shards_by_header_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = {}
            for core in (1, 0):
                path = root / f"qemu-fst.{core}.1234.raw.gz"
                with gzip.open(path, "wb") as output:
                    output.write(
                        RAW_HEADER.pack(
                            RAW_MAGIC, 2, RAW_HEADER.size, core, 125
                        )
                    )
                expected[core] = path

            self.assertEqual(_map_shards_to_cores(root, 2), expected)


if __name__ == "__main__":
    unittest.main()
