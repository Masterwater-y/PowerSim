"""The functional FST serialization boundary must never export OoO time."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'gem5-spr/src/cpu/o3/probe'


class FunctionalSyscallIsolation(unittest.TestCase):
    def test_timestamp_scrub_preserves_other_fields(self):
        code = r'''
#include <cstdint>
#include "functional_syscall_isolation.hh"
struct Metadata {
    std::uint64_t pre_timestamp_us = 123, post_timestamp_us = 456;
    std::uint16_t valid_fields = 0xffff;
    std::uint64_t number = 42;
};
int main() {
    Metadata x;
    gem5::functional_fst::clearNativeSyscallTimes(x);
    return x.pre_timestamp_us || x.post_timestamp_us ||
        x.valid_fields != 0xffcf || x.number != 42;
}
'''
        with tempfile.TemporaryDirectory() as d:
            cpp, binary = Path(d)/'check.cpp', Path(d)/'check'
            cpp.write_text(code)
            subprocess.run(['g++', '-std=c++17', '-I', str(PROBE), str(cpp), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_fst_finalizer_uses_scrub_on_wire_row(self):
        source = (PROBE / 'tao_trace.cc').read_text()
        start = source.index('TaoTrace::finalizeFst()')
        end = source.index('TaoTrace::', start + len('TaoTrace::finalizeFst()'))
        body = source[start:end]
        self.assertIn('functional_fst::clearNativeSyscallTimes(row)', body)
        self.assertLess(body.index('functional_fst::clearNativeSyscallTimes(row)'),
                        body.index('std::fwrite(&row'))


if __name__ == '__main__':
    unittest.main()
