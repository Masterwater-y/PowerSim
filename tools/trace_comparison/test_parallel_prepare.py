"""Bounded preparation must actually overlap work and join on failures."""

import pathlib
import os
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class ParallelPrepareTest(unittest.TestCase):
    def test_worker_overlap_and_exception_propagation(self):
        root = pathlib.Path(
            tempfile.mkdtemp(prefix="parallel-prepare-", dir=ROOT / "tmp")
        )
        source = root / "test.cpp"
        source.write_text(
            r"""
#include "parallel_prepare.hpp"
#include <atomic>
#include <chrono>
#include <stdexcept>
#include <thread>
int main() {
    std::atomic<unsigned> active(0), peak(0), completed(0);
    tracebench::parallel_prepare(8, 4, [&](size_t) {
        unsigned value=++active, old=peak.load();
        while(old<value && !peak.compare_exchange_weak(old,value)) {}
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        --active; ++completed;
    });
    if(completed!=8 || active!=0 || peak<2 || peak>4) return 1;
    bool caught=false;
    try {
        tracebench::parallel_prepare(8, 3, [&](size_t index) {
            if(index==0) throw std::runtime_error("fixture failure");
            ++active; std::this_thread::sleep_for(std::chrono::milliseconds(2)); --active;
        });
    } catch(const std::runtime_error&) {caught=true;}
    if(!caught || active!=0) return 2;
    completed=0;
    tracebench::parallel_prepare(4,1,[&](size_t i) {if(i!=completed++)throw std::runtime_error("order changed");});
    if(completed!=4) return 3;
}
"""
        )
        binary = root / "test"
        build = subprocess.run(
            [
                "g++",
                "-std=c++17",
                "-O2",
                "-pthread",
                "-I" + str(ROOT / "tools/trace_comparison"),
                str(source),
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(build.returncode, 0, build.stderr)
        p = subprocess.run(
            [str(binary)],
            env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            timeout=10,
        )
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
