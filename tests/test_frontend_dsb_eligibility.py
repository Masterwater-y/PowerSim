"""Decoded internal control flow is a functional template eligibility fact."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]


class FrontendDsbEligibilityTest(unittest.TestCase):
    def test_native_internal_control_clears_eligibility_without_clearing_code(self):
        text = (ROOT / 'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        method = text[text.index('TaoTrace::observeFstFrontend('):]
        update = re.search(
            r'    if \(si->isControl\(\) && si->isMicroop\(\) && !si->isLastMicroop\(\)\)\s*'
            r'm.flags&=~kFrontendDsbEligible;', method,
        )
        self.assertIsNotNone(update)
        source = r'''
#include "include/fastsim/retired_frontend_facts.hpp"
#include <cassert>
using namespace fastsim::fst;
struct Op {
    bool control, micro, last;
    bool isControl() const {return control;}
    bool isMicroop() const {return micro;}
    bool isLastMicroop() const {return last;}
};
void actualUpdate(RetiredFrontendMacro& m,const Op* si) {
NATIVE_UPDATE
}
int main() {
    RetiredFrontendMacro m;
    m.known_flags=kFrontendRequiredKnown;
    m.flags=kFrontendComplete|kFrontendStartsMacro|kFrontendDsbEligible;
    m.instruction_size=2; m.bytes[0]=0xeb; m.bytes[1]=0;
    m.code_identity=123; m.semantic_identity=456;
    const Op internal{true,true,false};
    actualUpdate(m,&internal);
    assert(!(m.flags&kFrontendDsbEligible));
    assert(m.flags&kFrontendComplete);
    assert(m.instruction_size==2 && m.code_identity==123 && m.semantic_identity==456);
    const Op ordinary_last{false,true,true};
    actualUpdate(m,&ordinary_last); assert(!(m.flags&kFrontendDsbEligible));
    const Op final_branch{true,true,true}, nonmicro_branch{true,false,false};
    m.flags|=kFrontendDsbEligible;
    actualUpdate(m,&final_branch); assert(m.flags&kFrontendDsbEligible);
    actualUpdate(m,&nonmicro_branch); assert(m.flags&kFrontendDsbEligible);
}
'''.replace('NATIVE_UPDATE', update.group(0))
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
