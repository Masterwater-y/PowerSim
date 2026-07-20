from __future__ import annotations

import unittest

from data.build_static_dict import parse_objdump


class StaticDictParserTest(unittest.TestCase):
    def test_wide_bytes_and_bad_opcode_keep_real_sizes(self):
        text = """
Disassembly of section .text:
0000000000401000 <f>:
  401000: 48 b8 ec 20 39 58 55 0f 96 d0  movabs rax,0xd0960f55583920ec
  40100a: c4                             (bad)
  40100b: c3                             ret
"""
        rows = list(parse_objdump(text))
        self.assertEqual([row.size_bytes for row in rows], [10, 1, 1])
        self.assertEqual(rows[0].bytes_hex, "48b8ec203958550f96d0")
        self.assertEqual(rows[1].mnemonic, ".byte")
        self.assertTrue(all(1 <= row.size_bytes <= 15 for row in rows))


if __name__ == "__main__":
    unittest.main()
