"""Split-store pairing uses functional macro identity, not the OoO SQ."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'gem5-spr/src/cpu/o3/probe'


class StorePairTest(unittest.TestCase):
    def test_macro_boundaries_and_faults(self):
        code = r'''
#include "functional_store_pair.hh"
using namespace gem5::functional_fst;
int main() {
  StorePairer p;
  auto a = p.observe(0, 7, 9, 0x100, 0, StoreRole::Address, true, false);
  auto d = p.observe(1, 7, 9, 0x100, 1, StoreRole::Data, true, false);
  if (!a || a != d) return 1;
  auto b = p.observe(2, 7, 9, 0x100, 0, StoreRole::Address, true, false);
  if (!b || b == a) return 2;
  if (p.observe(3, 8, 9, 0x100, 1, StoreRole::Data, true, false)) return 3;
  p.observe(4, 7, 9, 0x100, 0, StoreRole::Address, true, false);
  if (p.observe(5, 7, 9, 0x100, 1, StoreRole::Data, true, true)) return 4;
  p.observe(6, 7, 9, 0x100, 0, StoreRole::Address, true, false);
  p.observe(7, 7, 9, 0x200, 0, StoreRole::Other, false, false);
  if (p.observe(8, 7, 9, 0x100, 1, StoreRole::Data, true, false)) return 5;
  if (p.observe(9, 7, 9, 0x100, 0, StoreRole::Address, false, false)) return 6;
}
'''
        with tempfile.TemporaryDirectory() as d:
            cpp, binary = Path(d)/'check.cpp', Path(d)/'check'
            cpp.write_text(code)
            subprocess.run(['g++', '-std=c++17', '-I', str(PROBE), str(cpp), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_writer_does_not_read_sq_for_pairing(self):
        source = (PROBE / 'tao_trace.cc').read_text()
        start = source.index('if (fst_static_uops_) {', source.index('TaoTrace::accumulateMicro'))
        end = source.index('fst_static_uops_->observe(&rec,facts,operands);', start)
        body = source[start:end]
        self.assertNotIn('sqIt', body)
        self.assertIn('fst_store_pairer_.observe', body)


if __name__ == '__main__':
    unittest.main()
