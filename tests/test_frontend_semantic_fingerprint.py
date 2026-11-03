"""Functional semantic identity must never invoke SIMD disassembly formatting."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]


class FrontendSemanticFingerprintTest(unittest.TestCase):
    def test_simd_register_identity_without_disassembly(self):
        text = (ROOT / 'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        method = text[text.index('TaoTrace::observeFstFrontend('):]
        block = method[method.index('    auto& m=fst_frontend_pending_;', method.index('    auto& m=fst_frontend_pending_;') + 1):]
        block = block[:block.index('    if (si->isControl() && !si->isCondCtrl())')]
        source = r'''
#include "include/fastsim/retired_frontend_facts.hpp"
#include <cassert>
#include <cstdlib>
#include <string>
#include <vector>
using namespace fastsim::fst;
struct Reg {
    uint64_t cls, idx;
    uint64_t classValue() const {return cls;}
    uint64_t index() const {return idx;}
};
struct SIMD {
    std::vector<Reg> src{{3,32},{3,33}}, dst{{3,34}};
    std::string getName() const {return "ldfp";}
    std::string disassemble(uint64_t) const {std::abort();} // Original wide-register assertion path.
    uint64_t opClass() const {return 42;}
    uint64_t numSrcRegs() const {return src.size();}
    uint64_t numDestRegs() const {return dst.size();}
    const Reg& srcRegIdx(unsigned i) const {return src.at(i);}
    const Reg& destRegIdx(unsigned i) const {return dst.at(i);}
    bool isMicroFusedMemory() const {return false;}
    bool isStackRegAllocation() const {return false;}
    bool isControl() const {return false;}
    bool isCondCtrl() const {return false;}
    bool isIndirectCtrl() const {return false;}
    bool isCall() const {return false;}
    bool isReturn() const {return false;}
    bool isLoad() const {return true;}
    bool isStore() const {return false;}
    bool isAtomic() const {return false;}
    bool isSerializing() const {return false;}
    bool isMicroop() const {return true;}
    bool isFirstMicroop() const {return false;}
    bool isLastMicroop() const {return true;}
};
struct PC {uint64_t microPC() const {return 2;}};
struct Inst {PC pcState() const {return {};}};
uint64_t fingerprint(const SIMD& op,uint64_t code=100) {
    const auto si=&op;
    Inst value; const auto inst=&value;
    RetiredFrontendMacro fst_frontend_pending_;
    fst_frontend_pending_.pc=0x1000;
    fst_frontend_pending_.code_identity=code;
    fst_frontend_pending_.semantic_identity=kFrontendHashSeed;
FINGERPRINT_BLOCK
    return m.semantic_identity;
}
int main() {
    SIMD wide;
    const auto first=fingerprint(wide); // Must not abort on 16/32/64-byte SIMD formatting.
    assert(first==fingerprint(wide));
    auto src=wide; src.src[0].idx++; assert(first!=fingerprint(src));
    auto dst=wide; dst.dst[0].idx++; assert(first!=fingerprint(dst));
    auto cls=wide; cls.src[0].cls++; assert(first!=fingerprint(cls));
    auto order=wide; std::swap(order.src[0],order.src[1]); assert(first!=fingerprint(order));
    assert(first!=fingerprint(wide,101)); // Exact code bytes/context remain part of semantics.
}
'''.replace('FINGERPRINT_BLOCK', block)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            work = Path(directory)
            (work / 'test.cc').write_text(source)
            build = subprocess.run(
                ['/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                 '-I', str(ROOT), str(work / 'test.cc'), '-lz', '-o', str(work / 'test')],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(work / 'test')], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
