"""Check modeled per-op-class FU rotation, including failed searches."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SprFuRoundRobinTest(unittest.TestCase):
    def test_rotation_skips_busy_and_keeps_per_class_state(self):
        source = r'''
#include "fastsim/spr_fu_round_robin.hpp"
#include <cassert>
int main() {
 fastsim::SprFuRoundRobin rr;
 const unsigned all=(1u<<0)|(1u<<2)|(1u<<5);
 assert(rr.select(3,all,all)==0);
 assert(rr.select(3,all,all)==2);
 assert(rr.select(4,all,all)==0);
 assert(rr.select(3,all,all & ~(1u<<5))==0);
 assert(rr.select(3,all,0)==-1);
 assert(rr.select(3,all,all)==5);
 auto copy=rr;
 assert(rr.select(3,all,all)==0);
 assert(copy.select(3,all,all)==0);
 assert(rr.select(3,0,0)==-1);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            path, exe = Path(d) / 'test.cpp', Path(d) / 'test'
            path.write_text(source)
            build = subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17', '-O2',
                '-static-libstdc++', '-static-libgcc', '-I', str(ROOT / 'include'),
                str(path), '-o', str(exe)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
