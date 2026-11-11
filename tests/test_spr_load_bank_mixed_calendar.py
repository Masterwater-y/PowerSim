"""Mixed qualification and cross-checkpoint SPR port calendar boundaries.

These component tests do not exercise Simulator's eligibility plumbing.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SprLoadBankMixedCalendarTest(unittest.TestCase):
    def test_mixed_qualification_and_cross_chunk_state(self):
        code = r'''
#include "fastsim/spr_load_bank.hpp"
#include <cassert>
#include <cstdint>
using fastsim::SprLoadBank;
int main() {
 constexpr std::uint32_t ports=3;
 SprLoadBank committed;
 // First checkpoint: two certified same-bank packets, both at cycle 10.
 committed.accept(0x1000,10,ports,true);
 committed.accept(0x1040,10,ports,true);
 auto snapshot=committed;
 auto second=committed; // next checkpoint receives the committed state
 // Known packet, unknown bank (e.g. software prefetch) consumes total port.
 assert(second.first_available(0,10,ports,false)==10);
 second.accept(0,10,ports,false);
 assert(!second.port_available(10,ports));
 assert(second.first_available(0x1004,10,ports,true)==11);
 assert(second.first_available(0x1080,10,ports,true)==11);
 // Rollback keeps the prior two slots and discards the third.
 assert(snapshot.port_available(10,ports));
 assert(snapshot.first_available(0x1004,10,ports,true)==10);
 // Prune strictly older than the dispatch lower bound, retaining the bound.
 second.prune(10);
 assert(!second.port_available(10,ports));
 second.prune(11);
 assert(second.port_available(10,ports));

 // Same accepted packet stream is independent of checkpoint placement.
 SprLoadBank flat;
 auto flat_a=flat.first_available(0x1000,10,ports,true);flat.accept(0x1000,flat_a,ports,true);
 auto flat_b=flat.first_available(0x1040,10,ports,true);flat.accept(0x1040,flat_b,ports,true);
 auto flat_c=flat.first_available(0,10,ports,false);flat.accept(0,flat_c,ports,false);
 auto flat_d=flat.first_available(0x1004,10,ports,true);
 auto chunk_d=committed;
 auto chunk_c=chunk_d.first_available(0,10,ports,false);chunk_d.accept(0,chunk_c,ports,false);
 assert(flat_a==10 && flat_b==10 && flat_c==chunk_c && flat_d==chunk_d.first_available(0x1004,10,ports,true));

 // Intentional coverage gap: an unqualified split/unknown physical request
 // is not reserved. If a real packet also occupies cycle 20, three other
 // certified reservations can coexist there in the model. This demonstrates
 // possible total-port undercount, not an assertion of target equivalence.
 SprLoadBank mixed;
 mixed.accept(0x1000,20,ports,true);
 mixed.accept(0x1004,20,ports,true);
 mixed.accept(0x1008,20,ports,true);
 const unsigned modeled=3, fallback_packets_if_one=1;
 assert(!mixed.port_available(20,ports));
 assert(modeled+fallback_packets_if_one>ports);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            src=Path(d)/'test.cpp';exe=Path(d)/'test';src.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)


if __name__ == '__main__':
    unittest.main()
